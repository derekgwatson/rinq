"""Call log database methods.

Mixin class — mixed into Database via multiple inheritance.
All methods access self._get_conn() and self._fill_hourly() from the Database base class.
"""

import logging
from datetime import datetime, timezone

import pytz

logger = logging.getLogger(__name__)

# Reports are in Sydney time (same as ReportingService.LOCAL_TZ)
_LOCAL_TZ = pytz.timezone('Australia/Sydney')


def _local_hour(started_at: str) -> int:
    """Local hour of a stored UTC timestamp, using that call's own date.

    A fixed offset taken from 'now' puts every call on the other side of a
    daylight-saving change in the wrong hour.
    """
    dt = datetime.fromisoformat(str(started_at).replace('Z', ''))
    return pytz.UTC.localize(dt.replace(tzinfo=None)).astimezone(_LOCAL_TZ).hour

# A row still 'ringing' after 2 minutes was never touched by any later handler.
# For an INBOUND call that means the caller hung up before anything happened:
# during the welcome message or a menu, or while a direct line rang. Twilio
# sends no status callback for those (the numbers have none configured), so
# this sweep is the only place the outcome is ever written. It used to stamp
# them 'missed', which read as "nobody answered" — they never reached anyone
# who could have. 'ended_early' was introduced 2026-10-14; older swept rows
# are still 'missed'.
# A caller WAITING in a queue is also still 'ringing' in call_log (the main
# queue path never changes it), so they're skipped while queued_calls says
# they're waiting — the queue-leave handler writes their real outcome. Before
# this they were swept at 2 minutes (wrong label + ended_at mid-wait) and only
# corrected when they left the queue.
STALE_RINGING_SWEEP_SQL = """
    UPDATE call_log
    SET status = CASE WHEN direction = 'inbound' THEN 'ended_early' ELSE 'missed' END,
        ended_at = datetime('now'),
        updated_at = datetime('now')
    WHERE ended_at IS NULL
      AND status = 'ringing'
      AND started_at < datetime('now', '-2 minutes')
      AND call_sid NOT IN (SELECT call_sid FROM queued_calls WHERE status = 'waiting')
"""

# Statuses that count as "a person picked up" for a caller.
ANSWERED_STATUSES = ('answered', 'completed', 'transferred')

# Rows that are one real call each. call_log has a row per call LEG, so leave out:
# - the customer's leg of an outbound call (agent_email NULL; the staff member's
#   leg is the call) — counting both doubled every outbound call
# - transfer credit rows (call_type 'transfer') and the blind-transfer target
#   leg (conference '..._xfer') — the same customer call, already counted
ONE_ROW_PER_CALL_SQL = (
    "NOT (direction = 'outbound' AND agent_email IS NULL)"
    " AND COALESCE(call_type, '') <> 'transfer'"
    " AND COALESCE(conference_name, '') NOT LIKE '%\\_xfer' ESCAPE '\\'"
)

# Talk time for reporting. talk_seconds is uncapped: a call whose hang-up was
# never seen got closed much later with all that time counted (87 rows, the
# worst 1,733 hours, none after 2026-07-30) — anything over 4 hours counts as
# 0. On outbound calls it is the staff leg's whole duration, so it includes the
# ringing before the customer picked up (~13s a call); capping it at the time
# actually connected (answered_at -> ended_at) removes that. For inbound calls
# the two already agree.
TALK_SQL = (
    "(CASE WHEN talk_seconds IS NULL OR talk_seconds > 14400 THEN 0"
    " WHEN answered_at IS NOT NULL AND ended_at IS NOT NULL AND ended_at >= answered_at"
    " THEN MIN(talk_seconds, CAST((julianday(ended_at) - julianday(answered_at)) * 86400 AS INTEGER))"
    " ELSE talk_seconds END)"
)


class CallLogMixin:
    """Call logging, history, and call-log-based reporting queries."""

    def log_call(self, data: dict) -> int:
        """Log a call to the comprehensive call_log table."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO call_log (
                    call_sid, parent_call_sid, direction, call_type,
                    from_number, to_number, phone_number_id,
                    queue_id, queue_name, call_flow_id,
                    status, agent_email,
                    customer_id, customer_name, customer_email,
                    conference_name, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('call_sid'),
                data.get('parent_call_sid'),
                data.get('direction'),
                data.get('call_type'),
                data.get('from_number'),
                data.get('to_number'),
                data.get('phone_number_id'),
                data.get('queue_id'),
                data.get('queue_name'),
                data.get('call_flow_id'),
                data.get('status', 'ringing'),
                data.get('agent_email'),
                data.get('customer_id'),
                data.get('customer_name'),
                data.get('customer_email'),
                data.get('conference_name'),
                data.get('started_at', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')),
            ))
            conn.commit()
            return cursor.lastrowid

    def get_recent_outbound_caller(self, number: str, days: int = 14) -> str | None:
        """Email of the staff member who most recently rang this number."""
        call = self.get_recent_outbound_call(number, days)
        return call['agent_email'] if call else None

    def get_recent_outbound_call(self, number: str, days: int = 14) -> dict | None:
        """{agent_email, started_at} of the most recent staff call to this number.

        Only the staff leg of an outbound call carries an email in agent_email
        (the customer's leg has none; a few old rows hold 'sip:<user>'), so
        that filter also picks one row per call. `number` must be E.164.
        """
        if not number:
            return None
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT agent_email, started_at FROM call_log
                WHERE direction = 'outbound'
                  AND to_number = ?
                  AND agent_email LIKE '%@%'
                  AND started_at >= datetime('now', ?)
                ORDER BY started_at DESC
                LIMIT 1
            """, (number, f'-{int(days)} days')).fetchone()
            return dict(row) if row else None

    _CALL_LOG_FIELDS = {
        'status', 'direction', 'call_type', 'from_number', 'to_number',
        'customer_name', 'customer_email', 'conference_name', 'answered_by',
        'duration', 'talk_duration', 'ring_duration', 'recording_url',
    }

    def get_call_log_field(self, call_sid: str, field: str):
        """Get a single field from a call_log entry by call SID."""
        if field not in self._CALL_LOG_FIELDS:
            raise ValueError(f"Invalid call_log field: {field}")
        with self._get_conn() as conn:
            row = conn.execute(
                f"SELECT {field} FROM call_log WHERE call_sid = ?", (call_sid,)
            ).fetchone()
            return row[field] if row else None

    _CALL_LOG_UPDATE_FIELDS = {
        'status', 'call_type', 'agent_email', 'answered_at', 'ended_at',
        'customer_name', 'customer_email', 'customer_id', 'notes',
        'ring_seconds', 'ai_receptionist', 'answered_by',
    }

    def update_call_log(self, call_sid: str, updates: dict) -> None:
        """Update a call_log entry."""
        if not updates:
            return
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        set_clauses = []
        values = []
        for key, value in updates.items():
            if key not in self._CALL_LOG_UPDATE_FIELDS:
                # Don't raise (some callers pass superset dicts), but a typo'd
                # field silently no-oping has already cost debugging time —
                # make the drop visible.
                logger.warning(f"update_call_log: ignoring unknown field '{key}' for {call_sid}")
                continue
            set_clauses.append(f"{key} = ?")
            # Convert 'CURRENT_TIMESTAMP' string to actual datetime
            if value == 'CURRENT_TIMESTAMP':
                value = now
            values.append(value)
        if not set_clauses:
            return
        set_clauses.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
        values.append(call_sid)
        with self._get_conn() as conn:
            conn.execute(f"UPDATE call_log SET {', '.join(set_clauses)} WHERE call_sid = ?", values)
            conn.commit()

    def complete_call(self, call_sid: str, status: str, agent_email: str = None,
                      talk_seconds: int = None) -> None:
        """Mark a call as completed with final status and duration."""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with self._get_conn() as conn:
            row = conn.execute("SELECT started_at, answered_at FROM call_log WHERE call_sid = ?", (call_sid,)).fetchone()
            updates = {'status': status, 'ended_at': now, 'updated_at': now}
            if row and row['started_at']:
                try:
                    start_dt = datetime.fromisoformat(row['started_at'])
                    if row['answered_at']:
                        answer_dt = datetime.fromisoformat(row['answered_at'])
                        updates['ring_seconds'] = int((answer_dt - start_dt).total_seconds())
                    else:
                        updates['ring_seconds'] = int((datetime.fromisoformat(now) - start_dt).total_seconds())
                except (ValueError, TypeError):
                    pass
            if status == 'answered':
                if not row or not row['answered_at']:
                    updates['answered_at'] = now
                if agent_email:
                    updates['agent_email'] = agent_email
            if talk_seconds is not None:
                updates['talk_seconds'] = talk_seconds
                updates['total_seconds'] = (updates.get('ring_seconds', 0) or 0) + talk_seconds
            else:
                # Calculate talk_seconds as total_duration - ring_seconds
                # (answered_at is unreliable for conference-based calls — can arrive after ended_at)
                if row and row['started_at']:
                    try:
                        start_dt = datetime.fromisoformat(row['started_at'])
                        ended_dt = datetime.fromisoformat(now)
                        total_secs = max(0, int((ended_dt - start_dt).total_seconds()))
                        ring_secs = updates.get('ring_seconds', 0) or 0
                        calculated_talk = max(0, total_secs - ring_secs)
                        updates['talk_seconds'] = calculated_talk
                        updates['total_seconds'] = total_secs
                    except (ValueError, TypeError):
                        if 'ring_seconds' in updates:
                            updates['total_seconds'] = updates['ring_seconds']
                elif 'ring_seconds' in updates:
                    updates['total_seconds'] = updates['ring_seconds']
            set_clauses = [f"{k} = ?" for k in updates.keys()]
            values = list(updates.values()) + [call_sid]
            conn.execute(f"UPDATE call_log SET {', '.join(set_clauses)} WHERE call_sid = ?", values)
            conn.commit()

    def get_active_calls(self) -> list[dict]:
        """Get calls currently in progress (not yet ended).

        Filters:
        - ended_at IS NULL (not completed)
        - Must have an agent (otherwise we can't show who's busy)
        - Auto-cleans stale ringing calls (>2 min) on each query
        - Deduplicates per agent, keeping only the most recent call
        """
        with self._get_conn() as conn:
            # Clean up stale calls:
            # - Ringing > 2 min is impossible (Twilio gives up sooner)
            # - Answered calls > 2 hours old without ended_at are stale
            #   (status callback likely never fired for transferred/SIP calls)
            conn.execute(STALE_RINGING_SWEEP_SQL)
            conn.execute("""
                UPDATE call_log
                SET status = 'completed', ended_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE ended_at IS NULL
                  AND status = 'answered'
                  AND started_at < datetime('now', '-2 hours')
            """)
            conn.commit()

            rows = conn.execute("""
                SELECT cl.call_sid, cl.direction, cl.from_number, cl.to_number,
                       cl.agent_email, cl.status, cl.started_at, cl.answered_at,
                       cl.customer_name, cl.queue_name,
                       u.staff_email as resolved_email
                FROM call_log cl
                LEFT JOIN users u
                    ON cl.agent_email = 'sip:' || u.username
                WHERE cl.ended_at IS NULL
                  AND cl.agent_email IS NOT NULL
                  AND cl.status = 'answered'
                ORDER BY cl.started_at DESC
            """).fetchall()

            # Deduplicate: keep only the most recent call per agent
            seen_agents = set()
            result = []
            for row in rows:
                row_dict = dict(row)
                # Resolve SIP identities (sip:username) to staff email
                if row_dict.get('resolved_email'):
                    row_dict['agent_email'] = row_dict['resolved_email']
                del row_dict['resolved_email']

                agent = row_dict['agent_email']
                if agent in seen_agents:
                    continue
                seen_agents.add(agent)
                result.append(row_dict)
            return result

    def sweep_stale_ringing(self) -> int:
        """Close call_log rows stuck 'ringing' (see STALE_RINGING_SWEEP_SQL).

        The other two callers only run while someone has the phone page open,
        so after hours and over a weekend these rows sat open until the first
        login. The 5-minute cleanup cron calls this so they close regardless.
        """
        with self._get_conn() as conn:
            cursor = conn.execute(STALE_RINGING_SWEEP_SQL)
            conn.commit()
            return cursor.rowcount

    def close_stale_calls(self, active_sids: set[str]) -> int:
        """Close call_log entries for calls no longer active in Twilio.

        Any call_log entry with ended_at IS NULL whose call_sid is NOT in
        active_sids is marked as completed. Returns the number of rows updated.
        """
        with self._get_conn() as conn:
            # Always clean up stale ringing calls regardless
            conn.execute(STALE_RINGING_SWEEP_SQL)
            conn.commit()

            # Close calls that Twilio says are no longer active
            rows = conn.execute("""
                SELECT call_sid FROM call_log
                WHERE ended_at IS NULL AND status = 'answered'
            """).fetchall()

            stale_sids = [r['call_sid'] for r in rows if r['call_sid'] not in active_sids]

        # Route through complete_call so talk_seconds is calculated from
        # started_at / ring_seconds. Twilio's own status callback doesn't
        # fire reliably for SIP-answered customer calls, so this path is
        # how those calls get their talk time populated.
        for sid in stale_sids:
            self.complete_call(call_sid=sid, status='completed')
        return len(stale_sids)

    def get_call_log_by_sids(self, sids: set[str]) -> dict:
        """Get call_log entries for a set of call SIDs.

        Returns a dict mapping call_sid -> row dict, with SIP agent
        identities resolved to staff emails.
        """
        if not sids:
            return {}

        with self._get_conn() as conn:
            placeholders = ','.join('?' * len(sids))
            rows = conn.execute(f"""
                SELECT cl.call_sid, cl.direction, cl.from_number, cl.to_number,
                       cl.agent_email, cl.status, cl.started_at, cl.answered_at,
                       cl.customer_name, cl.queue_name,
                       u.staff_email as resolved_email
                FROM call_log cl
                LEFT JOIN users u
                    ON cl.agent_email = 'sip:' || u.username
                WHERE cl.call_sid IN ({placeholders})
            """, list(sids)).fetchall()

            result = {}
            for row in rows:
                d = dict(row)
                if d.get('resolved_email'):
                    d['agent_email'] = d['resolved_email']
                del d['resolved_email']
                result[d['call_sid']] = d
            return result

    def _build_queue_filter(self, queue_name=None, queue_names=None, agent_emails=None):
        """Build WHERE clause fragments for queue + outbound agent filtering.

        Returns (clause_str, params_list). The clause includes queue calls
        and outbound calls by the specified agents.
        """
        conditions = []
        params = []

        if queue_name:
            conditions.append("queue_name = ?")
            params.append(queue_name)
        elif queue_names:
            placeholders = ','.join('?' * len(queue_names))
            conditions.append(f"queue_name IN ({placeholders})")
            params.extend(queue_names)

        if agent_emails:
            placeholders = ','.join('?' * len(agent_emails))
            conditions.append(f"(direction = 'outbound' AND agent_email IN ({placeholders}))")
            params.extend(agent_emails)

        if conditions:
            return " AND (" + " OR ".join(conditions) + ")", params
        return "", []

    def _resolve_team_identities(self, team_emails: list) -> list:
        """Expand team emails to include SIP identity variants.

        call_log stores SIP identities (sip:username@domain) as agent_email
        for desk phone calls, so we need to include those in team filters.
        """
        with self._get_conn() as conn:
            users = conn.execute(
                "SELECT username, staff_email FROM users WHERE username IS NOT NULL"
            ).fetchall()
            sip_map = {u['staff_email']: u['username'] for u in users if u['staff_email']}

        all_identities = list(team_emails)
        for email in team_emails:
            username = sip_map.get(email)
            if username:
                all_identities.append(f"sip:{username}")
                # Twilio may append the domain
                domain = email.split('@')[1] if '@' in email else ''
                if domain:
                    all_identities.append(f"sip:{username}@{domain}")
        return all_identities

    def _build_team_filter(self, team_emails: list):
        """Build WHERE clause to filter calls by team member emails.

        Filters both inbound and outbound calls where agent_email matches
        any team member (including SIP identity variants).

        Returns (clause_str, params_list).
        """
        if not team_emails:
            return "", []

        all_identities = self._resolve_team_identities(team_emails)
        placeholders = ','.join('?' * len(all_identities))
        return f" AND agent_email IN ({placeholders})", list(all_identities)

    def get_call_log_stats(self, start_utc: str, end_utc: str, queue_name: str = None, queue_names: list = None, agent_emails: list = None, team_emails: list = None) -> dict:
        """Get call statistics from call_log for a UTC timestamp range."""
        with self._get_conn() as conn:
            where = f"started_at >= ? AND started_at <= ? AND {ONE_ROW_PER_CALL_SQL}"
            params = [start_utc, end_utc]
            if team_emails:
                filter_clause, filter_params = self._build_team_filter(team_emails)
            else:
                filter_clause, filter_params = self._build_queue_filter(queue_name, queue_names, agent_emails)
            where += filter_clause
            params.extend(filter_params)

            row = conn.execute(f"""
                SELECT
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) as outbound_calls,
                    SUM(CASE WHEN status IN ('answered', 'completed', 'transferred') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'voicemail' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM({TALK_SQL}) as total_duration_seconds,
                    SUM(COALESCE(ring_seconds, 0)) as total_wait_seconds,
                    AVG(CASE WHEN status IN ('answered', 'completed', 'transferred') THEN NULLIF({TALK_SQL}, 0) END) as avg_duration_seconds,
                    AVG(CASE WHEN status IN ('answered', 'completed', 'transferred') THEN ring_seconds END) as avg_answered_wait_seconds,
                    MAX(CASE WHEN status IN ('answered', 'completed', 'transferred') THEN ring_seconds END) as max_answered_wait_seconds,
                    AVG(CASE WHEN status IN ('abandoned', 'missed') THEN ring_seconds END) as avg_abandoned_wait_seconds,
                    MAX(CASE WHEN status IN ('abandoned', 'missed') THEN ring_seconds END) as max_abandoned_wait_seconds,
                    AVG(ring_seconds) as avg_wait_seconds,
                    SUM(CASE WHEN status IN ('answered', 'completed', 'transferred') AND direction = 'inbound' AND ring_seconds <= 15 THEN 1 ELSE 0 END) as within_15s,
                    SUM(CASE WHEN status IN ('answered', 'completed', 'transferred') AND direction = 'inbound' AND ring_seconds > 15 AND ring_seconds <= 30 THEN 1 ELSE 0 END) as within_30s,
                    SUM(CASE WHEN status IN ('answered', 'completed', 'transferred') AND direction = 'inbound' AND ring_seconds > 30 AND ring_seconds <= 60 THEN 1 ELSE 0 END) as within_60s,
                    SUM(CASE WHEN status IN ('answered', 'completed', 'transferred') AND direction = 'inbound' AND ring_seconds > 60 AND ring_seconds <= 90 THEN 1 ELSE 0 END) as within_90s,
                    SUM(CASE WHEN status IN ('answered', 'completed', 'transferred') AND direction = 'inbound' AND ring_seconds > 90 THEN 1 ELSE 0 END) as over_90s
                FROM call_log WHERE {where}
            """, params).fetchone()
            total = row['total_calls'] or 0
            answered = row['answered_calls'] or 0
            abandoned = row['abandoned_calls'] or 0
            timeout = row['timeout_calls'] or 0
            return {
                'total_calls': total,
                'inbound_calls': row['inbound_calls'] or 0,
                'outbound_calls': row['outbound_calls'] or 0,
                'answered_calls': answered,
                'abandoned_calls': abandoned,
                'timeout_calls': timeout,
                'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                'abandoned_rate': round(abandoned / total * 100, 1) if total > 0 else 0,
                'timeout_rate': round(timeout / total * 100, 1) if total > 0 else 0,
                'total_duration_seconds': row['total_duration_seconds'] or 0,
                'total_wait_seconds': row['total_wait_seconds'] or 0,
                'avg_duration_seconds': int(row['avg_duration_seconds'] or 0),
                'avg_wait_seconds': int(row['avg_wait_seconds'] or 0),
                'avg_answered_wait_seconds': int(row['avg_answered_wait_seconds'] or 0),
                'max_answered_wait_seconds': int(row['max_answered_wait_seconds'] or 0),
                'avg_abandoned_wait_seconds': int(row['avg_abandoned_wait_seconds'] or 0),
                'max_abandoned_wait_seconds': int(row['max_abandoned_wait_seconds'] or 0),
                'answer_speed': {
                    'within_15s': row['within_15s'] or 0,
                    'within_30s': row['within_30s'] or 0,
                    'within_60s': row['within_60s'] or 0,
                    'within_90s': row['within_90s'] or 0,
                    'over_90s': row['over_90s'] or 0,
                },
            }

    def _caller_rows(self, start_utc: str, end_utc: str) -> list:
        """One row per inbound caller in the range, with its outcome.

        Excludes the extra rows a transfer creates (agent credit rows and the
        blind-transfer target leg, conference '..._xfer'). Outcomes:
          answered        — a person picked up
          voicemail_msg   — sent to voicemail and a message was recorded
          voicemail_none  — sent to voicemail, no message (status 'voicemail'
                            is set when the caller is SENT there)
          callback        — asked (from the queue) to be called back
          hung_up_waiting — hung up while waiting in the queue ('abandoned')
          ended_early     — hung up before reaching the queue or anyone
          not_recorded    — 'missed' and anything else. Until 2026-10-14 the
                            stale sweep stamped early hang-ups 'missed', so
                            history can't be told apart from a genuine ring-out
          in_progress     — still ringing/queued; left out of the percentages
        """
        answered = "','".join(ANSWERED_STATUSES)
        with self._get_conn() as conn:
            return conn.execute(f"""
                SELECT c.started_at, c.queue_id, c.queue_name, c.ring_seconds,
                    CASE
                        WHEN c.status IN ('ringing', 'queued') AND c.ended_at IS NULL THEN 'in_progress'
                        WHEN c.status IN ('{answered}') THEN 'answered'
                        WHEN c.status = 'voicemail' AND vm.call_sid IS NOT NULL THEN 'voicemail_msg'
                        WHEN c.status = 'voicemail' THEN 'voicemail_none'
                        WHEN c.status = 'callback' THEN 'callback'
                        WHEN c.status = 'abandoned' THEN 'hung_up_waiting'
                        WHEN c.status = 'ended_early' THEN 'ended_early'
                        ELSE 'not_recorded'
                    END AS outcome
                FROM call_log c
                LEFT JOIN (
                    SELECT DISTINCT call_sid FROM recording_log
                    WHERE call_type = 'voicemail' AND created_at >= datetime(?, '-1 day')
                ) vm ON vm.call_sid = c.call_sid
                WHERE c.started_at >= ? AND c.started_at <= ?
                  AND c.direction = 'inbound'
                  AND COALESCE(c.call_type, '') <> 'transfer'
                  AND COALESCE(c.conference_name, '') NOT LIKE '%\\_xfer' ESCAPE '\\'
            """, [start_utc, start_utc, end_utc]).fetchall()

    @staticmethod
    def _outcome_bucket(outcome: str) -> str:
        """Three-way split used by the charts and the queue table."""
        if outcome == 'answered':
            return 'answered'
        if outcome.startswith('voicemail') or outcome == 'callback':
            return 'voicemail'
        return 'not_through'

    def get_caller_outcomes(self, start_utc: str, end_utc: str, tz_offset_hours: int = None) -> dict:
        """What happened to every person who rang in, whoever (if anyone) took it.

        The team-scoped stats above can only see calls with a staff member on
        them, so every caller nobody answered was invisible and the answer rate
        read ~99%. This counts callers instead (see _caller_rows for the
        outcomes). Unscoped — only for viewers allowed to see the whole company.

        Wait figures are TIME TO ANSWER: from when the call reached Tina until
        someone picked up (or the caller hung up in the queue), so they include
        the welcome message. tz_offset_hours is ignored — each call's hour is
        converted with its own date, so ranges across a daylight-saving change
        land in the right hour.
        """
        rows = self._caller_rows(start_utc, end_utc)

        keys = ('answered', 'voicemail_msg', 'voicemail_none', 'callback', 'hung_up_waiting',
                'ended_early', 'not_recorded', 'in_progress')
        counts = {k: 0 for k in keys}
        hourly = {h: {'answered': 0, 'voicemail': 0, 'not_through': 0} for h in range(24)}
        answered_waits, abandoned_waits = [], []
        for r in rows:
            o = r['outcome']
            counts[o] += 1
            if o == 'in_progress':
                continue
            hourly[_local_hour(r['started_at'])][self._outcome_bucket(o)] += 1
            if r['ring_seconds'] is not None:
                if o == 'answered':
                    answered_waits.append(r['ring_seconds'])
                elif o == 'hung_up_waiting':
                    abandoned_waits.append(r['ring_seconds'])

        callers = sum(v for k, v in counts.items() if k != 'in_progress')

        def pct(n):
            return round(n / callers * 100, 1) if callers else 0

        # A callback request is a message left another way — same card
        voicemail = counts['voicemail_msg'] + counts['voicemail_none'] + counts['callback']
        not_through = counts['ended_early'] + counts['not_recorded']
        waits = answered_waits + abandoned_waits
        return {
            'callers': callers,
            'in_progress': counts['in_progress'],
            'answered': counts['answered'],
            'answered_pct': pct(counts['answered']),
            'voicemail': voicemail,
            'voicemail_pct': pct(voicemail),
            'voicemail_with_message': counts['voicemail_msg'],
            'callback_requests': counts['callback'],
            'hung_up_waiting': counts['hung_up_waiting'],
            'hung_up_waiting_pct': pct(counts['hung_up_waiting']),
            'not_through': not_through,
            'not_through_pct': pct(not_through),
            'ended_early': counts['ended_early'],
            'not_recorded': counts['not_recorded'],
            'avg_answered_wait_seconds': int(sum(answered_waits) / len(answered_waits)) if answered_waits else 0,
            'max_answered_wait_seconds': max(answered_waits, default=0),
            'avg_abandoned_wait_seconds': int(sum(abandoned_waits) / len(abandoned_waits)) if abandoned_waits else 0,
            'max_abandoned_wait_seconds': max(abandoned_waits, default=0),
            'total_wait_seconds': sum(waits),
            'avg_wait_seconds': int(sum(waits) / len(waits)) if waits else 0,
            'answer_speed': {
                'within_15s': sum(1 for w in answered_waits if w <= 15),
                'within_30s': sum(1 for w in answered_waits if 15 < w <= 30),
                'within_60s': sum(1 for w in answered_waits if 30 < w <= 60),
                'within_90s': sum(1 for w in answered_waits if 60 < w <= 90),
                'over_90s': sum(1 for w in answered_waits if w > 90),
            },
            'hourly': [
                {'hour': h, 'label': f"{h:02d}:00", **hourly[h],
                 'total_calls': sum(hourly[h].values())}
                for h in range(24)
            ],
        }

    def get_queue_outcomes(self, start_utc: str, end_utc: str) -> list[dict]:
        """Per-queue caller outcomes, from call_log, for any period.

        Replaces the daily_call_stats path (duplicated rows every 15 minutes,
        UTC dates, built from queued_calls after its 24h purge) and the
        today-only queued_calls path, so every period counts the same way as
        the caller cards. Same dict shape the Queue Performance table reads:
        abandoned_calls = hung up (waiting or early), timeout_calls = voicemail.
        Avg wait = average time to answer; max wait = longest anyone waited
        before being answered or hanging up in the queue.
        """
        queues = {}
        for r in self._caller_rows(start_utc, end_utc):
            if not r['queue_name'] or r['outcome'] == 'in_progress':
                continue
            q = queues.setdefault(r['queue_name'], {
                'queue_id': r['queue_id'], 'queue_name': r['queue_name'],
                'answered': 0, 'voicemail': 0, 'not_through': 0,
                'answered_waits': [], 'all_waits': []})
            q[self._outcome_bucket(r['outcome'])] += 1
            if r['ring_seconds'] is not None:
                if r['outcome'] == 'answered':
                    q['answered_waits'].append(r['ring_seconds'])
                if r['outcome'] in ('answered', 'hung_up_waiting'):
                    q['all_waits'].append(r['ring_seconds'])

        result = []
        for q in queues.values():
            total = q['answered'] + q['voicemail'] + q['not_through']
            result.append({
                'queue_id': q['queue_id'],
                'queue_name': q['queue_name'],
                'total_calls': total,
                'answered_calls': q['answered'],
                'abandoned_calls': q['not_through'],
                'timeout_calls': q['voicemail'],
                'answer_rate': round(q['answered'] / total * 100, 1) if total else 0,
                'avg_wait_seconds': (int(sum(q['answered_waits']) / len(q['answered_waits']))
                                     if q['answered_waits'] else 0),
                'max_wait_seconds': max(q['all_waits'], default=0),
            })
        result.sort(key=lambda q: q['total_calls'], reverse=True)
        return result

    def get_call_log_by_agent(self, start_utc: str, end_utc: str, queue_name: str = None,
                              queue_names: list = None, agent_emails: list = None,
                              team_emails: list = None) -> list[dict]:
        """Get call statistics grouped by agent.

        Resolves SIP identities (sip:username@...) to staff emails so
        desk phone and browser calls are merged into one agent entry.

        Args:
            team_emails: If provided, filters agents to this list (from Peter's
                        manager reportees). Overrides queue-based agent filtering
                        for the agent stats only.
        """
        with self._get_conn() as conn:
            # Build a SIP username -> email mapping for resolution
            users = conn.execute(
                "SELECT username, staff_email FROM users WHERE username IS NOT NULL"
            ).fetchall()
            sip_map = {}
            for u in users:
                sip_map[f"sip:{u['username']}"] = u['staff_email']

            where = "started_at >= ? AND started_at <= ? AND agent_email IS NOT NULL"
            params = [start_utc, end_utc]

            if team_emails:
                # Team-based filtering: show all calls (inbound + outbound) for team members
                # Build SIP variants so desk phone calls are included
                all_identities = list(team_emails)
                for email in team_emails:
                    # Find SIP identities that map to this email
                    for sip_key, mapped_email in sip_map.items():
                        if mapped_email == email:
                            all_identities.append(sip_key)
                            # Also add with domain suffix variants
                            all_identities.append(sip_key + '@' + email.split('@')[1] if '@' in email else sip_key)

                placeholders = ','.join('?' * len(all_identities))
                where += f" AND agent_email IN ({placeholders})"
                params.extend(all_identities)
            else:
                # Queue-based filtering (legacy behavior)
                filter_clause, filter_params = self._build_queue_filter(queue_name, queue_names, agent_emails)
                where += filter_clause
                params.extend(filter_params)

            # Exclude auto-ring attempts (outbound calls with no talk time
            # that ended as missed/no-answer — these are queue ring attempts, not real calls)
            real_call_where = where + " AND NOT (direction = 'outbound' AND status IN ('missed', 'abandoned') AND (talk_seconds IS NULL OR talk_seconds = 0))"
            # Extension-to-extension calls are colleagues, not customers
            real_call_where += " AND direction <> 'internal'"

            rows = conn.execute(f"""
                SELECT agent_email, COUNT(*) as total_calls,
                    SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) as outbound_calls,
                    SUM(CASE WHEN status IN ('answered', 'completed', 'transferred') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN direction = 'inbound' AND status IN ('answered', 'completed', 'transferred') THEN 1 ELSE 0 END) as inbound_answered,
                    SUM(CASE WHEN direction = 'inbound' AND status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as missed_calls,
                    SUM({TALK_SQL}) as total_duration_seconds,
                    SUM(CASE WHEN direction = 'inbound' THEN {TALK_SQL} ELSE 0 END) as inbound_duration_seconds,
                    SUM(CASE WHEN direction = 'outbound' THEN {TALK_SQL} ELSE 0 END) as outbound_duration_seconds,
                    AVG(NULLIF({TALK_SQL}, 0)) as avg_duration_seconds,
                    AVG(CASE WHEN direction = 'inbound' THEN NULLIF({TALK_SQL}, 0) END) as inbound_avg_duration_seconds,
                    AVG(CASE WHEN direction = 'outbound' THEN NULLIF({TALK_SQL}, 0) END) as outbound_avg_duration_seconds
                FROM call_log WHERE {real_call_where}
                GROUP BY agent_email ORDER BY total_calls DESC
            """, params).fetchall()

            # Merge SIP entries with their email counterparts
            merged = {}
            for row in rows:
                d = dict(row)
                email = d['agent_email']

                # Resolve SIP URI: "sip:chris_savage@domain" -> check sip_map for "sip:chris_savage"
                if email.startswith('sip:'):
                    sip_key = email.split('@')[0] if '@' in email else email
                    email = sip_map.get(sip_key, email)
                    d['agent_email'] = email

                if email in merged:
                    # Merge with existing entry
                    m = merged[email]
                    m['total_calls'] += d['total_calls']
                    m['inbound_calls'] += d['inbound_calls']
                    m['outbound_calls'] += d['outbound_calls']
                    m['answered_calls'] += d['answered_calls']
                    m['inbound_answered'] = (m.get('inbound_answered') or 0) + (d.get('inbound_answered') or 0)
                    m['missed_calls'] += d['missed_calls']
                    m['total_duration_seconds'] += d['total_duration_seconds']
                    m['inbound_duration_seconds'] = (m.get('inbound_duration_seconds') or 0) + (d.get('inbound_duration_seconds') or 0)
                    m['outbound_duration_seconds'] = (m.get('outbound_duration_seconds') or 0) + (d.get('outbound_duration_seconds') or 0)
                    # Recalculate average
                    if m['answered_calls'] > 0:
                        m['avg_duration_seconds'] = m['total_duration_seconds'] // m['answered_calls']
                    if m['outbound_calls'] > 0 and m['outbound_duration_seconds'] > 0:
                        m['outbound_avg_duration_seconds'] = m['outbound_duration_seconds'] // m['outbound_calls']
                else:
                    d['avg_duration_seconds'] = int(d['avg_duration_seconds'] or 0)
                    d['outbound_avg_duration_seconds'] = int(d['outbound_avg_duration_seconds'] or 0)
                    d['total_duration_seconds'] = int(d['total_duration_seconds'] or 0)
                    d['inbound_duration_seconds'] = int(d.get('inbound_duration_seconds') or 0)
                    d['outbound_duration_seconds'] = int(d.get('outbound_duration_seconds') or 0)
                    d['inbound_answered'] = int(d.get('inbound_answered') or 0)
                    merged[email] = d

            result = sorted(merged.values(), key=lambda x: x['total_calls'], reverse=True)
            return result

    def get_call_log_hourly(self, start_utc: str, end_utc: str, tz_offset_hours: int = 11, queue_name: str = None, queue_names: list = None, agent_emails: list = None, team_emails: list = None) -> list[dict]:
        """Get hourly call distribution in local time."""
        with self._get_conn() as conn:
            where = "started_at >= ? AND started_at <= ?"
            params = [start_utc, end_utc]
            if team_emails:
                filter_clause, filter_params = self._build_team_filter(team_emails)
            else:
                filter_clause, filter_params = self._build_queue_filter(queue_name, queue_names, agent_emails)
            where += filter_clause
            params.extend(filter_params)

            # Local hour per call (its own date), not a fixed offset from now —
            # tz_offset_hours is kept for callers but no longer used
            calls = conn.execute(f"""
                SELECT started_at, status FROM call_log WHERE {where}
            """, params).fetchall()
            by_hour = {}
            for c in calls:
                h = by_hour.setdefault(_local_hour(c['started_at']), {
                    'hour': None, 'total_calls': 0, 'answered_calls': 0,
                    'abandoned_calls': 0, 'timeout_calls': 0})
                h['total_calls'] += 1
                if c['status'] in ANSWERED_STATUSES:
                    h['answered_calls'] += 1
                elif c['status'] in ('abandoned', 'missed', 'ended_early'):
                    h['abandoned_calls'] += 1
                elif c['status'] == 'voicemail':
                    h['timeout_calls'] += 1
            for hour, h in by_hour.items():
                h['hour'] = hour
            return self._fill_hourly(list(by_hour.values()))

    def get_my_call_history(self, agent_email: str, limit: int = 50) -> list[dict]:
        """Get recent call history for a specific agent.

        Returns calls where the agent was involved — either as the agent_email
        on inbound calls, or as the caller on outbound calls.

        Args:
            agent_email: The agent's email address
            limit: Maximum number of calls to return

        Returns:
            List of call dicts with direction, status, numbers, timing, etc.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    call_sid, direction, call_type, status,
                    from_number, to_number,
                    started_at, answered_at, ended_at,
                    ring_seconds, talk_seconds, total_seconds,
                    customer_name, queue_name, is_recorded
                FROM call_log
                WHERE agent_email = ?
                   OR (direction = 'inbound' AND agent_email IS NULL AND to_number IN (
                       SELECT pn.phone_number
                       FROM phone_numbers pn
                       JOIN phone_assignments pa ON pa.phone_number_sid = pn.sid
                       WHERE pa.staff_email = ?
                   ))
                ORDER BY started_at DESC
                LIMIT ?
            """, (agent_email, agent_email, limit)).fetchall()
            return [dict(row) for row in rows]

    def get_call_history_by_phone(self, phone_number: str, limit: int = 10) -> dict:
        """Get call history for a phone number.

        Searches both from_number and to_number to capture
        inbound and outbound calls with this customer.

        Args:
            phone_number: Phone number to search for (any format)
            limit: Maximum number of recent calls to return

        Returns:
            dict with:
                - total_calls: Total call count with this number
                - calls: List of recent calls (up to limit)
                - last_call_date: Date of most recent call
        """
        # Extract the core number (last 9 digits) to match any format
        # +61412345678, 0412345678, 412345678 all have the same last 9 digits
        cleaned = phone_number.replace(' ', '').replace('-', '').replace('+', '')
        # Get last 9 digits (Australian mobile numbers)
        core_number = cleaned[-9:] if len(cleaned) >= 9 else cleaned

        with self._get_conn() as conn:
            # Get recent calls (both directions)
            # Use LIKE with % to match partial numbers (handles format variations)
            cursor = conn.execute(f"""
                SELECT
                    call_sid,
                    direction,
                    status,
                    started_at,
                    talk_seconds,
                    agent_email,
                    customer_name,
                    queue_name
                FROM call_log
                WHERE (from_number LIKE ? OR to_number LIKE ?) AND {ONE_ROW_PER_CALL_SQL}
                ORDER BY started_at DESC
                LIMIT ?
            """, (f'%{core_number}%', f'%{core_number}%', limit))

            calls = [dict(row) for row in cursor.fetchall()]

            # Get total count
            cursor = conn.execute(f"""
                SELECT COUNT(*) FROM call_log
                WHERE (from_number LIKE ? OR to_number LIKE ?) AND {ONE_ROW_PER_CALL_SQL}
            """, (f'%{core_number}%', f'%{core_number}%'))
            total = cursor.fetchone()[0]

            return {
                'total_calls': total,
                'calls': calls,
                'last_call_date': calls[0]['started_at'] if calls else None
            }

    # ------------------------------------------------------------------
    # Row-level exports (CSV)
    #
    # These stream one row per call rather than aggregating. Callers must
    # not hold the results in memory — the point is that a year of watson
    # volume never lands in a list.
    # ------------------------------------------------------------------

    CALL_LOG_EXPORT_COLUMNS = [
        'call_sid', 'parent_call_sid', 'direction', 'call_type', 'status',
        'agent_email', 'queue_name', 'started_at', 'answered_at', 'ended_at',
        'ring_seconds', 'talk_seconds', 'total_seconds',
        'transfer_status', 'transfer_type', 'transfer_target', 'transferred_by',
        'transferred_at', 'transfer_failure_reason', 'is_recorded',
    ]

    # Anything that identifies the person on the other end of the call.
    CALL_LOG_CUSTOMER_COLUMNS = [
        'from_number', 'to_number', 'customer_id', 'customer_name', 'customer_email',
    ]

    def iter_call_log_export(self, start_utc: str, end_utc: str, team_emails: list = None):
        """Yield call_log rows for a UTC range, oldest first.

        Args:
            start_utc, end_utc: inclusive UTC timestamp bounds
            team_emails: restrict to these agents (plus their SIP identities).
                         None means no agent filter — callers must only pass
                         None for users allowed to see everyone.

        Yields:
            sqlite3.Row per call, streamed.
        """
        columns = list(self.CALL_LOG_EXPORT_COLUMNS)
        # from_number/to_number are always selected: the export derives a
        # non-identifying customer_ref from them even when they're withheld.
        select = columns + self.CALL_LOG_CUSTOMER_COLUMNS

        where = "started_at >= ? AND started_at <= ?"
        params = [start_utc, end_utc]
        if team_emails is not None:
            filter_clause, filter_params = self._build_team_filter(team_emails)
            if not filter_clause:
                # An empty team list must match nothing, never everything.
                return
            where += filter_clause
            params.extend(filter_params)

        with self._get_conn() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(select)} FROM call_log WHERE {where} ORDER BY started_at ASC",
                params,
            )
            for row in cursor:
                yield row

    QUEUED_CALLS_EXPORT_COLUMNS = [
        'call_sid', 'queue_name', 'status', 'priority', 'priority_reason',
        'enqueued_at', 'answered_at', 'answered_by', 'ended_at', 'wait_seconds',
        'transfer_status', 'transfer_type', 'transfer_target', 'transferred_by',
        'transferred_at', 'transfer_failure_reason',
    ]

    QUEUED_CALLS_CUSTOMER_COLUMNS = [
        'caller_number', 'called_number', 'customer_id', 'customer_name', 'customer_email',
    ]

    def iter_queued_calls_export(self, start_utc: str, end_utc: str):
        """Yield queued_calls rows for a UTC range, oldest first.

        Unscoped by agent on purpose: abandoned calls have no answered_by, so
        an agent filter would silently drop exactly the rows that make this
        dataset worth exporting. Callers must gate this on manager access.
        """
        select = list(self.QUEUED_CALLS_EXPORT_COLUMNS) + list(self.QUEUED_CALLS_CUSTOMER_COLUMNS)

        with self._get_conn() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(select)} FROM queued_calls "
                "WHERE enqueued_at >= ? AND enqueued_at <= ? ORDER BY enqueued_at ASC",
                [start_utc, end_utc],
            )
            for row in cursor:
                yield row
