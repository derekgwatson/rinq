"""CSV data exports — row-level call data for offline analysis.

Extracted from routes.py. Registered via register(api_bp) at import time.

/reports answers fixed questions with pre-aggregated numbers. These endpoints
do the opposite: one row per call, streamed, so the analysis can happen
wherever the file lands. Two datasets, because the metrics live in two tables
— call_log has no wait time, queued_calls has no outbound calls.

Scoping comes from resolve_visible_emails(), shared with /reports, so the
dashboard and the download can never disagree about who may see what.
"""

import csv
import hashlib
import io
import logging
from datetime import datetime

from flask import Response, request

from rinq.database.db import get_db, _parse_dt
from rinq.services.auth import login_required, manager_required, get_current_user
from rinq.services.reporting_service import (
    LOCAL_TZ,
    get_reporting_service,
    resolve_visible_emails,
)
from rinq.tenant.context import get_current_tenant

logger = logging.getLogger(__name__)

# A range longer than this is almost certainly a mistake, and the resulting
# file is too big to be useful anyway. Refuse loudly rather than stream a
# multi-hundred-megabyte download nobody can open.
MAX_EXPORT_DAYS = 366


def _local(ts: str) -> str:
    """Convert a stored UTC timestamp to local wall-clock time.

    call_log and queued_calls store naive UTC. Exporting only UTC puts a 5pm
    call spike at 6am in the analysis, so every timestamp ships in both forms.
    """
    if not ts:
        return ''
    try:
        return _parse_dt(ts).astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError) as e:
        logger.warning(f"Could not parse export timestamp {ts!r}: {e}")
        return ''


def _core_digits(number: str) -> str:
    """Reduce a phone number (or SIP URI) to the digits that identify a caller.

    Same rule the rest of Rinq matches numbers by: the last 9 digits, so
    +61412345678, 0412345678 and 412345678 all resolve to one caller. Without
    this, one person split across two number formats counts as two people.
    """
    if not number:
        return ''
    digits = ''.join(c for c in str(number) if c.isdigit())
    return digits[-9:] if len(digits) >= 9 else digits


def _customer_ref(number: str, salt: str) -> str:
    """Stable non-identifying reference for the other party on a call.

    Lets an analyst count repeat callers without the export carrying anyone's
    phone number. Salted per tenant so refs can't be compared across tenants
    or reversed with a precomputed table of Australian numbers.
    """
    digits = _core_digits(number)
    if not digits:
        return ''
    return hashlib.sha256(f"{salt}:{digits}".encode()).hexdigest()[:12]


def _tenant_salt() -> str:
    tenant = get_current_tenant()
    return str(tenant['id']) if tenant else 'no-tenant'


def _resolve_range(period: str):
    """Parse a /reports-style period into UTC bounds.

    Returns (parsed, error_message). Accepts the same strings the dashboard
    uses, including 'YYYY-MM-DD:YYYY-MM-DD'.
    """
    parsed = get_reporting_service().parse_period(period)
    start = datetime.strptime(parsed['start_date'], '%Y-%m-%d').date()
    end = datetime.strptime(parsed['end_date'], '%Y-%m-%d').date()
    if end < start:
        return None, "End date is before start date."
    span = (end - start).days + 1
    if span > MAX_EXPORT_DAYS:
        return None, f"Range is {span} days; the maximum is {MAX_EXPORT_DAYS}. Export it in chunks."
    return parsed, None


def _stream_csv(header: list, rows_iter, filename: str) -> Response:
    """Stream rows as a CSV download without buffering the whole file."""
    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator='\n')
        writer.writerow(header)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in rows_iter:
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return Response(
        generate(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'no-store',
        },
    )


def _wants_customer_data(user) -> bool:
    """Customer identity columns are opt-in and admin-only.

    Nothing in agent performance, queue wait or hour-of-day analysis needs a
    customer's name or number, so the default export carries none of it.
    """
    if request.args.get('include_customer') not in ('1', 'true', 'yes'):
        return False
    if not user.is_admin:
        logger.warning(
            f"{user.email} requested customer columns in a CSV export "
            "without an admin role — columns withheld"
        )
        return False
    return True


def _audit(db, target: str, detail: str, email: str) -> None:
    """Record who exported what. Never block the download on the audit trail."""
    try:
        db.log_activity('export', target, detail, email)
    except Exception as e:
        logger.warning(f"Could not log CSV export activity for {email}: {e}", exc_info=True)


def register(bp):
    """Register all export routes on the given blueprint."""

    @bp.route('/export/calls.csv', methods=['GET'])
    @login_required
    def export_calls_csv():
        """Row-level call_log export, scoped to what the user may see."""
        user = get_current_user()
        period = request.args.get('period', 'today')

        parsed, error = _resolve_range(period)
        if error:
            return Response(error, status=400, mimetype='text/plain')

        team_emails, team_label = resolve_visible_emails(user)
        include_customer = _wants_customer_data(user)
        salt = _tenant_salt()
        db = get_db()

        header = (
            list(db.CALL_LOG_EXPORT_COLUMNS)
            + ['started_at_local', 'answered_at_local', 'ended_at_local', 'customer_ref']
        )
        if include_customer:
            header += list(db.CALL_LOG_CUSTOMER_COLUMNS)

        def rows():
            for row in db.iter_call_log_export(parsed['start_utc'], parsed['end_utc'], team_emails):
                # The customer is whichever end of the call isn't us.
                counterparty = (
                    row['from_number'] if row['direction'] == 'inbound' else row['to_number']
                )
                out = [row[c] for c in db.CALL_LOG_EXPORT_COLUMNS]
                out += [
                    _local(row['started_at']),
                    _local(row['answered_at']),
                    _local(row['ended_at']),
                    _customer_ref(counterparty, salt),
                ]
                if include_customer:
                    out += [row[c] for c in db.CALL_LOG_CUSTOMER_COLUMNS]
                yield out

        filename = f"rinq-calls-{parsed['start_date']}-to-{parsed['end_date']}.csv"
        logger.info(
            f"CSV export: calls {parsed['start_date']}..{parsed['end_date']} "
            f"scope={team_label} customer_columns={include_customer} by {user.email}"
        )
        _audit(
            db, 'calls.csv',
            f"{parsed['start_date']} to {parsed['end_date']}, scope={team_label}, "
            f"customer_columns={include_customer}",
            user.email,
        )
        return _stream_csv(header, rows(), filename)

    @bp.route('/export/queue-calls.csv', methods=['GET'])
    @manager_required
    def export_queue_calls_csv():
        """Row-level queued_calls export — wait times, abandons, priority.

        Manager-only: this dataset is deliberately unscoped by agent because
        abandoned calls have no agent, and a per-agent slice of it would
        report a zero abandon rate rather than an honest one.
        """
        user = get_current_user()
        period = request.args.get('period', 'today')

        parsed, error = _resolve_range(period)
        if error:
            return Response(error, status=400, mimetype='text/plain')

        include_customer = _wants_customer_data(user)
        salt = _tenant_salt()
        db = get_db()

        header = (
            list(db.QUEUED_CALLS_EXPORT_COLUMNS)
            + ['enqueued_at_local', 'answered_at_local', 'ended_at_local', 'customer_ref']
        )
        if include_customer:
            header += list(db.QUEUED_CALLS_CUSTOMER_COLUMNS)

        def rows():
            for row in db.iter_queued_calls_export(parsed['start_utc'], parsed['end_utc']):
                out = [row[c] for c in db.QUEUED_CALLS_EXPORT_COLUMNS]
                out += [
                    _local(row['enqueued_at']),
                    _local(row['answered_at']),
                    _local(row['ended_at']),
                    _customer_ref(row['caller_number'], salt),
                ]
                if include_customer:
                    out += [row[c] for c in db.QUEUED_CALLS_CUSTOMER_COLUMNS]
                yield out

        filename = f"rinq-queue-calls-{parsed['start_date']}-to-{parsed['end_date']}.csv"
        logger.info(
            f"CSV export: queue calls {parsed['start_date']}..{parsed['end_date']} "
            f"customer_columns={include_customer} by {user.email}"
        )
        _audit(
            db, 'queue-calls.csv',
            f"{parsed['start_date']} to {parsed['end_date']}, "
            f"customer_columns={include_customer}",
            user.email,
        )
        return _stream_csv(header, rows(), filename)

    @bp.route('/export/data-dictionary.md', methods=['GET'])
    @login_required
    def export_data_dictionary():
        """Column meanings and the counting rules that go with them.

        Download this alongside the CSV. Without it the transfer rows get
        double-counted and the numbers come out wrong but plausible.
        """
        return Response(
            DATA_DICTIONARY,
            mimetype='text/markdown',
            headers={'Content-Disposition': 'attachment; filename="rinq-data-dictionary.md"'},
        )


DATA_DICTIONARY = """# Rinq call data — data dictionary

Read this before analysing `rinq-calls-*.csv` or `rinq-queue-calls-*.csv`.
The three rules at the top are the ones that produce wrong-but-plausible
numbers if they are missed.

## Rules that change the answer

**1. Do not count every row as a call.**
`rinq-calls.csv` has one primary row per call, plus an extra row for every
additional agent who joined it — warm transfers, blind transfers, 3-way. Those
extra rows have `call_type = 'transfer'` and a `parent_call_sid` pointing at
the primary row. They exist so each agent gets credit for their actual talk
time; they are not separate calls.

- Call volume: filter to `parent_call_sid` being empty
- Agent talk time: use all rows, that is what they are for

**2. Timestamps come in two flavours.**
`started_at`, `answered_at` and `ended_at` are UTC. The `*_local` columns are
the same instants in Australia/Sydney, which is the business day. Use the local
columns for anything by hour, day or shift, or a 5pm spike lands at 6am.

**3. `talk_seconds` is empty on some rows.**
Calls that were never answered have no talk time, and a small number of older
rows are empty from a since-fixed bug. SQL `AVG()` ignores nulls; averaging in
a spreadsheet or in pandas may not. Filter explicitly.

## rinq-calls.csv — one row per call leg

| Column | Meaning |
|---|---|
| `call_sid` | Twilio call identifier, unique per row |
| `parent_call_sid` | Set on transfer rows, pointing at the primary row for that call. Empty on primary rows |
| `direction` | `inbound` or `outbound` |
| `call_type` | `direct`, `queue`, `transfer`, `voicemail`, `forwarded` |
| `status` | `answered`, `completed`, `transferred`, `abandoned`, `missed`, `voicemail`, `busy`, `failed`, `no-answer` |
| `agent_email` | Who handled it. Desk phone calls may appear as a `sip:` identity |
| `queue_name` | Queue the call came through, empty if it did not |
| `started_at` / `answered_at` / `ended_at` | UTC timestamps, empty if the call never reached that state |
| `started_at_local` / `answered_at_local` / `ended_at_local` | The same three in Australia/Sydney |
| `ring_seconds` | Time before the call was answered or abandoned |
| `talk_seconds` | Actual conversation time, empty if never answered |
| `total_seconds` | Whole call duration |
| `transfer_status` | `pending`, `consulting`, `completed`, `failed`, `cancelled` |
| `transfer_type` | `blind`, `warm`, `three_way` |
| `transfer_target` / `transferred_by` / `transferred_at` | Transfer participants and timing |
| `transfer_failure_reason` | Why a transfer failed, when it did |
| `is_recorded` | 1 if a recording exists |
| `customer_ref` | Stable anonymous id for the other party. The same person always gives the same ref, so repeat callers are countable without any phone number |

Answered calls are `status` in `answered`, `completed` or `transferred`. That is
the definition /reports uses, so use it too if you want the numbers to match.

## rinq-queue-calls.csv — one row per queued caller

This is where wait time lives; `rinq-calls.csv` has no wait metric. Queue calls
only, so no outbound calls appear here.

| Column | Meaning |
|---|---|
| `call_sid` | Joins to `call_sid` in rinq-calls.csv |
| `queue_name` | Queue the caller waited in |
| `status` | `waiting`, `answered`, `abandoned`, `timeout` |
| `priority` / `priority_reason` | Assigned at enqueue time from the customer lookup |
| `enqueued_at` / `answered_at` / `ended_at` | UTC, with `*_local` equivalents |
| `wait_seconds` | Time held in the queue |
| `answered_by` | Agent email, empty for abandoned and timed-out calls |
| `transfer_status` / `transfer_type` / `transfer_target` / `transferred_by` / `transferred_at` / `transfer_failure_reason` | As above |
| `customer_ref` | The same anonymous id scheme as rinq-calls.csv, so the two files join on it |

`timeout` means the caller was sent to voicemail after ringing out, which is a
per-queue setting. It is not the same as the caller abandoning.

## Scope

The rows in a calls export are limited to what the person downloading it can
see in /reports: their own calls, their team's if people report to them, or all
staff if they are a manager or admin. Queue exports are manager-only.

Customer names, emails and phone numbers are not included by default. Use
`customer_ref` for per-caller analysis.
"""
