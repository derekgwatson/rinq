"""Call log database methods.

Mixin class — mixed into Database via multiple inheritance.
All methods access self._get_conn() and self._fill_hourly() from the Database base class.
"""

from datetime import datetime, timezone


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
            conn.execute("""
                UPDATE call_log
                SET status = 'missed', ended_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE ended_at IS NULL
                  AND status = 'ringing'
                  AND started_at < datetime('now', '-2 minutes')
            """)
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

    def close_stale_calls(self, active_sids: set[str]) -> int:
        """Close call_log entries for calls no longer active in Twilio.

        Any call_log entry with ended_at IS NULL whose call_sid is NOT in
        active_sids is marked as completed. Returns the number of rows updated.
        """
        with self._get_conn() as conn:
            # Always clean up stale ringing calls regardless
            conn.execute("""
                UPDATE call_log
                SET status = 'missed', ended_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE ended_at IS NULL
                  AND status = 'ringing'
                  AND started_at < datetime('now', '-2 minutes')
            """)

            # Close calls that Twilio says are no longer active
            rows = conn.execute("""
                SELECT call_sid FROM call_log
                WHERE ended_at IS NULL AND status = 'answered'
            """).fetchall()

            stale_sids = [r['call_sid'] for r in rows if r['call_sid'] not in active_sids]
            if stale_sids:
                placeholders = ','.join('?' * len(stale_sids))
                conn.execute(f"""
                    UPDATE call_log
                    SET status = 'completed', ended_at = datetime('now'),
                        updated_at = datetime('now')
                    WHERE call_sid IN ({placeholders})
                """, stale_sids)

            conn.commit()
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
            where = "started_at >= ? AND started_at <= ?"
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
                    SUM(CASE WHEN status IN ('answered', 'completed') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'voicemail' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(COALESCE(talk_seconds, 0)) as total_duration_seconds,
                    SUM(COALESCE(ring_seconds, 0)) as total_wait_seconds,
                    AVG(CASE WHEN status IN ('answered', 'completed') THEN talk_seconds END) as avg_duration_seconds,
                    AVG(CASE WHEN status IN ('answered', 'completed') THEN ring_seconds END) as avg_answered_wait_seconds,
                    MAX(CASE WHEN status IN ('answered', 'completed') THEN ring_seconds END) as max_answered_wait_seconds,
                    AVG(CASE WHEN status IN ('abandoned', 'missed') THEN ring_seconds END) as avg_abandoned_wait_seconds,
                    MAX(CASE WHEN status IN ('abandoned', 'missed') THEN ring_seconds END) as max_abandoned_wait_seconds,
                    AVG(ring_seconds) as avg_wait_seconds,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds <= 15 THEN 1 ELSE 0 END) as within_15s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 15 AND ring_seconds <= 30 THEN 1 ELSE 0 END) as within_30s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 30 AND ring_seconds <= 60 THEN 1 ELSE 0 END) as within_60s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 60 AND ring_seconds <= 90 THEN 1 ELSE 0 END) as within_90s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 90 THEN 1 ELSE 0 END) as over_90s
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

            rows = conn.execute(f"""
                SELECT agent_email, COUNT(*) as total_calls,
                    SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) as outbound_calls,
                    SUM(CASE WHEN status IN ('answered', 'completed') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN direction = 'inbound' AND status IN ('answered', 'completed') THEN 1 ELSE 0 END) as inbound_answered,
                    SUM(CASE WHEN direction = 'inbound' AND status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as missed_calls,
                    SUM(COALESCE(talk_seconds, 0)) as total_duration_seconds,
                    SUM(CASE WHEN direction = 'inbound' THEN COALESCE(talk_seconds, 0) ELSE 0 END) as inbound_duration_seconds,
                    SUM(CASE WHEN direction = 'outbound' THEN COALESCE(talk_seconds, 0) ELSE 0 END) as outbound_duration_seconds,
                    AVG(CASE WHEN talk_seconds > 0 THEN talk_seconds END) as avg_duration_seconds,
                    AVG(CASE WHEN direction = 'outbound' AND talk_seconds > 0 THEN talk_seconds END) as outbound_avg_duration_seconds
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

            # Convert UTC hour to local hour by adding timezone offset
            rows = conn.execute(f"""
                SELECT CAST((strftime('%H', started_at) + {tz_offset_hours}) % 24 AS INTEGER) as hour,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status IN ('answered', 'completed') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'voicemail' THEN 1 ELSE 0 END) as timeout_calls
                FROM call_log WHERE {where} GROUP BY hour ORDER BY hour
            """, params).fetchall()
            return self._fill_hourly(rows)

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
                ORDER BY started_at DESC
                LIMIT ?
            """, (agent_email, limit)).fetchall()
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
            cursor = conn.execute("""
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
                WHERE from_number LIKE ? OR to_number LIKE ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (f'%{core_number}%', f'%{core_number}%', limit))

            calls = [dict(row) for row in cursor.fetchall()]

            # Get total count
            cursor = conn.execute("""
                SELECT COUNT(*) FROM call_log
                WHERE from_number LIKE ? OR to_number LIKE ?
            """, (f'%{core_number}%', f'%{core_number}%'))
            total = cursor.fetchone()[0]

            return {
                'total_calls': total,
                'calls': calls,
                'last_call_date': calls[0]['started_at'] if calls else None
            }
