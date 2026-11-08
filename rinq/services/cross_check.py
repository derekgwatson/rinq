"""Weekly cross-check: does Tina's call record agree with Twilio's?

Twilio keeps its own independent record of every call. Tina's Reports were
wrong for months (2026-10-13 audit: unanswered callers invisible, duplicate
tallies, impossible call lengths) and nothing noticed, because nothing ever
compared Tina's numbers with a second source. This does, call by call:

1. Incoming calls — every customer call Twilio delivered to one of our numbers
   must be in call_log, and vice versa (matched on the call SID).
2. Outgoing calls — where Twilio says the customer's phone was never answered
   (no-answer, busy, failed, cancelled), Tina must not have recorded the call
   as answered.
3. Housekeeping — signs of the old bugs coming back: impossible call lengths,
   inbound calls left unfinished, duplicate daily tallies.

The result is emailed to the tenant admins every week, pass or fail: a check
that only speaks up on failure looks exactly like a check that has stopped
running. "Couldn't check" (Twilio unreachable) is reported as its own state,
never as a pass.

Known outages: when Tina itself is down, callers never reach call_log, so the
incoming check rightly fails for that week. Once the outage has been dealt with
(lost callers found and rung back), record its window with
`python -m rinq.cli add-known-outage`. Calls inside a recorded window are still
REPORTED, on their own line, but no longer counted as a problem - so a re-run
of that week comes back clean, and a new, unexplained gap still fails.
"""

import json
import logging
import re
from datetime import date, datetime, time, timedelta

import pytz

logger = logging.getLogger(__name__)

LOCAL_TZ = pytz.timezone('Australia/Sydney')
SETTING_KEY = 'cross_check_last'
OUTAGES_KEY = 'cross_check_known_outages'

# Calls right at the window edges can land either side (Twilio's start time vs
# the moment Tina logged the webhook), so a couple of strays isn't a problem.
EDGE_TOLERANCE = 2
OUTBOUND_TOLERANCE_PCT = 2.0
MAX_EXAMPLES = 5
# Below this many incoming calls in the week the tenant is too quiet to email about
MIN_WEEKLY_CALLS_TO_EMAIL = 20
TALLY_FIX_DATE = date(2026, 10, 14)


def last_week() -> tuple[date, date]:
    """Monday..Sunday of the most recent full week, Sydney time."""
    today = datetime.now(LOCAL_TZ).date()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def _utc(d: date, end: bool = False) -> datetime:
    t = time(23, 59, 59) if end else time.min
    return LOCAL_TZ.localize(datetime.combine(d, t)).astimezone(pytz.UTC).replace(tzinfo=None)


def _fmt(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S')


# A real Twilio call SID. Anything else in call_log (e.g. 'CAclaudeprobe-b1')
# is a hand-made test row, which Twilio can never have.
_REAL_SID = re.compile(r'^CA[0-9a-f]{32}$')


def _as_utc(value) -> datetime | None:
    """Twilio's aware datetime, or call_log's naive-UTC text, as aware UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = pytz.UTC.localize(value)
    return value.astimezone(pytz.UTC)


def _local(value) -> str:
    """Sydney time the way a person reads it: 'Mon 2 Nov 9:19am'."""
    dt = _as_utc(value)
    if dt is None:
        return str(value)
    dt = dt.astimezone(LOCAL_TZ)
    return f"{dt:%a} {dt.day} {dt:%b} {dt.hour % 12 or 12}:{dt:%M}{'am' if dt.hour < 12 else 'pm'}"


def _sort_key(value) -> datetime:
    return _as_utc(value) or datetime.min.replace(tzinfo=pytz.UTC)


def get_known_outages(db) -> list[dict]:
    """[{start, end, note}] - start/end are Sydney local 'YYYY-MM-DDTHH:MM'."""
    try:
        return json.loads(db.get_bot_setting(OUTAGES_KEY) or '[]')
    except (ValueError, TypeError):
        logger.warning("Cross-check: known outages setting is not valid JSON - ignoring it")
        return []


def add_known_outage(db, start: datetime, end: datetime, note: str, by: str = 'cli') -> dict:
    """Record a window (naive Sydney local time) when Tina was known to be down."""
    if end <= start:
        raise ValueError("end must be after start")
    if not (note or '').strip():
        raise ValueError("a note is required: what happened, and how the lost callers were handled")
    outages = get_known_outages(db)
    entry = {'start': start.strftime('%Y-%m-%dT%H:%M'), 'end': end.strftime('%Y-%m-%dT%H:%M'),
             'note': note.strip()}
    outages.append(entry)
    outages.sort(key=lambda o: o['start'])
    db.set_bot_setting(OUTAGES_KEY, json.dumps(outages), by)
    return entry


def _outage_windows(db) -> list[tuple[datetime, datetime, dict]]:
    """Known outages as aware-UTC (start, end, entry)."""
    windows = []
    for o in get_known_outages(db):
        try:
            s = LOCAL_TZ.localize(datetime.fromisoformat(o['start'])).astimezone(pytz.UTC)
            e = LOCAL_TZ.localize(datetime.fromisoformat(o['end'])).astimezone(pytz.UTC)
        except (KeyError, ValueError, TypeError):
            logger.warning(f"Cross-check: skipping malformed known outage {o!r}")
            continue
        windows.append((s, e, o))
    return windows


def _outage_label(o: dict) -> str:
    s, e = datetime.fromisoformat(o['start']), datetime.fromisoformat(o['end'])
    t = lambda d: f"{d.hour % 12 or 12}:{d:%M}{'am' if d.hour < 12 else 'pm'}"
    day = lambda d: f"{d:%a} {d.day} {d:%b}"
    span = (f"{day(s)} {t(s)}-{t(e)}" if s.date() == e.date()
            else f"{day(s)} {t(s)} to {day(e)} {t(e)}")
    return f"{span}: {o['note']}"


def run(db, twilio_client, start: date, end: date) -> dict:
    """Compare Tina and Twilio for local dates start..end. Never raises."""
    from rinq.services.twilio_service import twilio_list

    s_utc, e_utc = _utc(start), _utc(end, end=True)
    result = {'start': start.isoformat(), 'end': end.isoformat(), 'checks': [],
              'status': 'ok', 'ran_at': datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}

    def add(name, ok, summary, examples=None):
        examples = examples or []
        shown = examples[:MAX_EXAMPLES]
        if len(examples) > MAX_EXAMPLES:
            shown.append(f"…and {len(examples) - MAX_EXAMPLES} more")
        result['checks'].append({'name': name, 'ok': ok, 'summary': summary,
                                 'examples': shown})
        if ok is False and result['status'] == 'ok':
            result['status'] = 'problems'

    try:
        calls = twilio_list(twilio_client.calls, raise_on_error=True,
                            start_time_after=s_utc, start_time_before=e_utc, page_size=1000)
    except Exception as e:
        logger.error(f"Cross-check: couldn't read Twilio's call records: {e}")
        result['status'] = 'could_not_check'
        result['error'] = str(e)[:300]
        return result

    ours = {p['phone_number'] for p in db.get_phone_numbers() if p.get('phone_number')}
    windows = _outage_windows(db)
    outages_hit = {}

    def in_known_outage(when) -> bool:
        t = _as_utc(when)
        for s, e, o in windows:
            if t is not None and s <= t <= e:
                outages_hit[o['start']] = o
                return True
        return False

    # ---- 1. Incoming calls, SID by SID
    tw_in = {c.sid: c for c in calls
             if c.direction == 'inbound' and (c.to or '') in ours}
    from rinq.database.call_log_mixin import ONE_ROW_PER_CALL_SQL
    with db._get_conn() as conn:
        log_in = {r['call_sid']: r for r in conn.execute(f"""
            SELECT call_sid, started_at, status FROM call_log
            WHERE direction = 'inbound' AND started_at >= ? AND started_at <= ?
              AND {ONE_ROW_PER_CALL_SQL}
        """, (_fmt(s_utc), _fmt(e_utc))) if _REAL_SID.match(r['call_sid'] or '')}
    result['incoming_calls'] = len(tw_in)
    missing_all = sorted(tw_in.keys() - log_in.keys(), key=lambda sid: (_sort_key(tw_in[sid].start_time), sid))
    missing = [sid for sid in missing_all if not in_known_outage(tw_in[sid].start_time)]
    in_outage = len(missing_all) - len(missing)
    result['incoming_in_outage'] = in_outage
    extra = sorted(log_in.keys() - tw_in.keys(), key=lambda sid: (_sort_key(log_in[sid]['started_at']), sid))
    ok = len(missing) <= EDGE_TOLERANCE and len(extra) <= EDGE_TOLERANCE
    add('Incoming calls', ok,
        f"Twilio delivered {len(tw_in)} customer calls to our numbers; Tina recorded {len(log_in)}. "
        f"{len(missing)} missing from Tina, {len(extra)} in Tina that Twilio doesn't have."
        + (f" (Not counted: {in_outage} more that came in during a known outage - see below.)"
           if in_outage else ""),
        [f"missing: {sid} at {_local(tw_in[sid].start_time)} from …{(tw_in[sid]._from or '')[-3:]}" for sid in missing]
        + [f"extra: {sid} at {_local(log_in[sid]['started_at'])} ({log_in[sid]['status']})" for sid in extra])

    # ---- 2. Outgoing calls: never "answered" when the customer didn't pick up
    tw_out = {c.sid: c for c in calls
              if c.direction == 'outbound-api' and not (c.to or '').startswith(('client:', 'sip:'))}
    with db._get_conn() as conn:
        staff_rows = {r['child_call_sid']: r for r in conn.execute("""
            SELECT child_call_sid, call_sid, agent_email, status, started_at FROM call_log
            WHERE direction = 'outbound' AND child_call_sid IS NOT NULL
              AND started_at >= ? AND started_at <= ?
        """, (_fmt(s_utc - timedelta(minutes=5)), _fmt(e_utc)))}
    matched = [sid for sid in tw_out if sid in staff_rows]
    unanswered = [sid for sid in matched if tw_out[sid].status in ('no-answer', 'busy', 'failed', 'canceled')]
    wrong_all = sorted((sid for sid in unanswered
                        if staff_rows[sid]['status'] in ('answered', 'completed', 'transferred')),
                       key=lambda sid: (_sort_key(staff_rows[sid]['started_at']), sid))
    wrong = [sid for sid in wrong_all if not in_known_outage(staff_rows[sid]['started_at'])]
    out_in_outage = len(wrong_all) - len(wrong)
    pct = round(len(wrong) / len(matched) * 100, 1) if matched else 0
    add('Outgoing calls', pct <= OUTBOUND_TOLERANCE_PCT,
        f"{len(matched)} outgoing customer calls matched to Twilio; the customer didn't pick up on "
        f"{len(unanswered)}. Tina recorded {len(wrong)} of those as answered ({pct}%)."
        + (f" (Not counted: {out_in_outage} more during a known outage.)" if out_in_outage else ""),
        [f"{staff_rows[sid]['call_sid']} by {(staff_rows[sid]['agent_email'] or '').split('@')[0]} at "
         f"{_local(staff_rows[sid]['started_at'])}: Twilio '{tw_out[sid].status}', Tina '{staff_rows[sid]['status']}'"
         for sid in wrong])

    # ---- 3. Housekeeping: have any of the old problems come back?
    with db._get_conn() as conn:
        long_calls = conn.execute(
            "SELECT count(*) FROM call_log WHERE talk_seconds > 14400 AND started_at >= ? AND started_at <= ?",
            (_fmt(s_utc), _fmt(e_utc))).fetchone()[0]
        unfinished = conn.execute(
            "SELECT count(*) FROM call_log WHERE direction = 'inbound' AND ended_at IS NULL "
            "AND status IN ('ringing', 'queued', 'answered') AND started_at >= ? AND started_at <= ?",
            (_fmt(s_utc), _fmt(e_utc))).fetchone()[0]
        # Unanswered tallies used a NULL key that never matched, so each
        # 15-minute run added a row (fixed 2026-10-13 -> '__all__'). Any NULL
        # row written after the fix means it has come back.
        dupes = conn.execute(
            "SELECT count(*) FROM daily_call_stats WHERE agent_email IS NULL "
            "AND date(created_at) >= ? AND date(created_at) <= ?",
            (max(start, TALLY_FIX_DATE).isoformat(), (end + timedelta(days=1)).isoformat())).fetchone()[0]
    add('Impossible call lengths', long_calls == 0,
        f"{long_calls} calls recorded as longer than 4 hours." if long_calls else "None.")
    add('Unfinished incoming calls', unfinished <= EDGE_TOLERANCE,
        f"{unfinished} incoming calls from the week still aren't marked as finished."
        if unfinished else "None — every incoming call has an end.")
    add('Duplicate daily tallies', dupes == 0,
        f"{dupes} daily tallies were written with the old broken key, so they pile up." if dupes else "None.")
    result['known_outages'] = [_outage_label(o) for _, o in sorted(outages_hit.items())]
    return result


def _admin_emails(tenant_id: str) -> list[str]:
    from rinq.database.master import get_master_db
    try:
        users = get_master_db().get_tenant_users(tenant_id)
    except Exception as e:
        logger.warning(f"Cross-check: could not read tenant admins for {tenant_id}: {e}")
        return []
    return [u['email'] for u in users if u.get('role') == 'admin' and u.get('email')]


def email_result(tenant: dict, result: dict, product: str = 'Tina') -> list[str]:
    """Email the admins. Returns who it went to."""
    period = f"{result['start']} to {result['end']}"
    if result['status'] == 'could_not_check':
        subject = f"{product} weekly check: COULDN'T RUN ({period})"
        body = (f"The weekly comparison of {product}'s call records with Twilio's couldn't run, because "
                f"Twilio's call records couldn't be read:\n\n  {result.get('error')}\n\n"
                "Nothing was checked this week. This is not a pass.\n")
    else:
        bad = [c for c in result['checks'] if c['ok'] is False]
        subject = (f"{product} weekly check: all good ({period})" if not bad else
                   f"{product} weekly check: {len(bad)} problem{'s' if len(bad) != 1 else ''} ({period})")
        lines = [f"Every week {product} compares its own call records with Twilio's independent record "
                 f"of the same calls. Week checked: {period}. Times are Sydney time.\n"]
        for c in result['checks']:
            lines.append(f"{'OK     ' if c['ok'] else 'PROBLEM'}  {c['name']}: {c['summary']}")
            for ex in c['examples']:
                lines.append(f"           - {ex}")
        if result.get('known_outages'):
            lines.append("\nKnown outages this week (already dealt with, so not counted above):")
            lines += [f"  - {label}" for label in result['known_outages']]
        if bad:
            lines.append("\nA problem here means Reports (and Tina Says) may be showing wrong numbers. "
                         "Worth asking Claude to look into it.")
        body = '\n'.join(lines) + '\n'

    recipients = _admin_emails(tenant['id'])
    sent = []
    try:
        from rinq.integrations import get_email_service
        email_service = get_email_service()
        if not email_service:
            logger.error(f"Cross-check: no email service — result: {subject}")
            return sent
        for address in recipients:
            try:
                if email_service.send_email(to=address, subject=subject, text_body=body):
                    sent.append(address)
            except Exception as e:
                logger.warning(f"Cross-check email to {address} failed: {e}")
    except Exception as e:
        logger.warning(f"Cross-check: email failed: {e}")
    return sent


def save(db, result: dict) -> None:
    try:
        db.set_bot_setting(SETTING_KEY, json.dumps(result), 'tina')
    except Exception as e:
        logger.warning(f"Cross-check: could not store result: {e}")
