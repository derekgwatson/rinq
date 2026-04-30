"""Statistics and reporting database methods.

Mixin class — mixed into Database via multiple inheritance.
All methods access self._get_conn() from the Database base class.
"""

from datetime import datetime, timezone, timedelta

import pytz

_LOCAL_TZ = pytz.timezone('Australia/Sydney')


class StatsMixin:
    """Aggregation, reporting, and hourly distribution queries."""

    def aggregate_daily_stats(self, target_date: str = None) -> dict:
        """Aggregate call statistics for a given date.

        This should be called before cleanup_old_queued_calls to preserve
        statistics that would otherwise be lost.

        Args:
            target_date: Date to aggregate (YYYY-MM-DD). Defaults to yesterday.

        Returns:
            Dict with counts of records aggregated
        """
        if not target_date:
            target_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            # Aggregate queue stats by queue and agent
            queue_data = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    answered_by as agent_email,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(CASE WHEN status = 'transferred' THEN 1 ELSE 0 END) as transferred_calls,
                    SUM(COALESCE(wait_seconds, 0)) as total_wait_seconds,
                    MAX(wait_seconds) as max_wait_seconds,
                    -- Answer speed buckets (for answered calls only)
                    SUM(CASE WHEN status = 'answered' AND wait_seconds <= 15 THEN 1 ELSE 0 END) as within_15s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 15 AND wait_seconds <= 30 THEN 1 ELSE 0 END) as within_30s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 30 AND wait_seconds <= 60 THEN 1 ELSE 0 END) as within_60s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 60 AND wait_seconds <= 90 THEN 1 ELSE 0 END) as within_90s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 90 THEN 1 ELSE 0 END) as over_90s,
                    -- Max wait for answered vs abandoned
                    MAX(CASE WHEN status = 'answered' THEN wait_seconds ELSE 0 END) as max_answered_wait,
                    MAX(CASE WHEN status = 'abandoned' THEN wait_seconds ELSE 0 END) as max_abandoned_wait,
                    -- Total wait for abandoned (for average calculation)
                    SUM(CASE WHEN status = 'abandoned' THEN COALESCE(wait_seconds, 0) ELSE 0 END) as abandoned_wait
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                GROUP BY queue_id, queue_name, answered_by
            """, (target_date,)).fetchall()

            # Get duration stats from recording_log for the same date
            recording_data = conn.execute("""
                SELECT
                    staff_email as agent_email,
                    SUM(COALESCE(duration_seconds, 0)) as total_duration,
                    SUM(CASE WHEN call_type = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN call_type = 'outbound' THEN 1 ELSE 0 END) as outbound_calls
                FROM recording_log
                WHERE DATE(created_at) = ?
                  AND call_type IN ('inbound', 'outbound')
                GROUP BY staff_email
            """, (target_date,)).fetchall()

            # Create lookup for recording duration by agent
            duration_by_agent = {
                row['agent_email']: {
                    'duration': row['total_duration'],
                    'inbound': row['inbound_calls'],
                    'outbound': row['outbound_calls']
                }
                for row in recording_data if row['agent_email']
            }

            now = datetime.now(timezone.utc).isoformat()
            records_created = 0

            for row in queue_data:
                agent = row['agent_email'] or '__all__'
                agent_durations = duration_by_agent.get(agent, {'duration': 0, 'inbound': 0, 'outbound': 0})

                conn.execute("""
                    INSERT INTO daily_call_stats (
                        stat_date, queue_id, queue_name, agent_email,
                        total_calls, answered_calls, abandoned_calls, timeout_calls, transferred_calls,
                        total_duration_seconds, total_wait_seconds,
                        answered_within_15s, answered_within_30s, answered_within_60s,
                        answered_within_90s, answered_over_90s,
                        abandoned_total_wait_seconds,
                        max_wait_seconds, max_answered_wait_seconds, max_abandoned_wait_seconds,
                        inbound_calls, outbound_calls,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stat_date, queue_id, agent_email) DO UPDATE SET
                        total_calls = excluded.total_calls,
                        answered_calls = excluded.answered_calls,
                        abandoned_calls = excluded.abandoned_calls,
                        timeout_calls = excluded.timeout_calls,
                        transferred_calls = excluded.transferred_calls,
                        total_duration_seconds = excluded.total_duration_seconds,
                        total_wait_seconds = excluded.total_wait_seconds,
                        answered_within_15s = excluded.answered_within_15s,
                        answered_within_30s = excluded.answered_within_30s,
                        answered_within_60s = excluded.answered_within_60s,
                        answered_within_90s = excluded.answered_within_90s,
                        answered_over_90s = excluded.answered_over_90s,
                        abandoned_total_wait_seconds = excluded.abandoned_total_wait_seconds,
                        max_wait_seconds = excluded.max_wait_seconds,
                        max_answered_wait_seconds = excluded.max_answered_wait_seconds,
                        max_abandoned_wait_seconds = excluded.max_abandoned_wait_seconds,
                        inbound_calls = excluded.inbound_calls,
                        outbound_calls = excluded.outbound_calls,
                        updated_at = excluded.updated_at
                """, (
                    target_date, row['queue_id'], row['queue_name'],
                    row['agent_email'],
                    row['total_calls'], row['answered_calls'], row['abandoned_calls'],
                    row['timeout_calls'], row['transferred_calls'],
                    agent_durations['duration'], row['total_wait_seconds'],
                    row['within_15s'], row['within_30s'], row['within_60s'],
                    row['within_90s'], row['over_90s'],
                    row['abandoned_wait'],
                    row['max_wait_seconds'], row['max_answered_wait'], row['max_abandoned_wait'],
                    agent_durations['inbound'], agent_durations['outbound'],
                    now, now
                ))
                records_created += 1

            conn.commit()
            return {'date': target_date, 'records_created': records_created}

    def aggregate_hourly_stats(self, target_date: str = None) -> dict:
        """Aggregate hourly call distribution for a given date.

        Args:
            target_date: Date to aggregate (YYYY-MM-DD). Defaults to yesterday.

        Returns:
            Dict with counts of records aggregated
        """
        if not target_date:
            target_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            hourly_data = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    CAST(strftime('%H', enqueued_at) AS INTEGER) as hour,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                GROUP BY queue_id, queue_name, hour
            """, (target_date,)).fetchall()

            now = datetime.now(timezone.utc).isoformat()
            records_created = 0

            for row in hourly_data:
                conn.execute("""
                    INSERT INTO hourly_call_stats (
                        stat_date, stat_hour, queue_id, queue_name,
                        total_calls, answered_calls, abandoned_calls, timeout_calls,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stat_date, stat_hour, queue_id) DO UPDATE SET
                        total_calls = excluded.total_calls,
                        answered_calls = excluded.answered_calls,
                        abandoned_calls = excluded.abandoned_calls,
                        timeout_calls = excluded.timeout_calls
                """, (
                    target_date, row['hour'], row['queue_id'], row['queue_name'],
                    row['total_calls'], row['answered_calls'],
                    row['abandoned_calls'], row['timeout_calls'],
                    now
                ))
                records_created += 1

            conn.commit()
            return {'date': target_date, 'records_created': records_created}

    def get_daily_stats_summary(self, start_date: str, end_date: str) -> dict:
        """Get aggregated daily statistics summary for a date range.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            Dict with summary statistics
        """
        with self._get_conn() as conn:
            # Get aggregated totals
            row = conn.execute("""
                SELECT
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls,
                    SUM(transferred_calls) as transferred_calls,
                    SUM(total_duration_seconds) as total_duration,
                    SUM(total_wait_seconds) as total_wait,
                    SUM(answered_within_15s) as within_15s,
                    SUM(answered_within_30s) as within_30s,
                    SUM(answered_within_60s) as within_60s,
                    SUM(answered_within_90s) as within_90s,
                    SUM(answered_over_90s) as over_90s,
                    SUM(abandoned_total_wait_seconds) as abandoned_wait,
                    MAX(max_wait_seconds) as max_wait,
                    MAX(max_answered_wait_seconds) as max_answered_wait,
                    MAX(max_abandoned_wait_seconds) as max_abandoned_wait,
                    SUM(inbound_calls) as inbound_calls,
                    SUM(outbound_calls) as outbound_calls
                FROM daily_call_stats
                WHERE stat_date BETWEEN ? AND ?
            """, (start_date, end_date)).fetchone()

            if not row or not row['total_calls']:
                return self._empty_stats_summary()

            total = row['total_calls'] or 0
            answered = row['answered_calls'] or 0
            abandoned = row['abandoned_calls'] or 0
            timeout = row['timeout_calls'] or 0

            return {
                'total_calls': total,
                'answered_calls': answered,
                'abandoned_calls': abandoned,
                'timeout_calls': timeout,
                'transferred_calls': row['transferred_calls'] or 0,
                'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                'abandoned_rate': round(abandoned / total * 100, 1) if total > 0 else 0,
                'timeout_rate': round(timeout / total * 100, 1) if total > 0 else 0,
                'total_duration_seconds': row['total_duration'] or 0,
                'avg_duration_seconds': round((row['total_duration'] or 0) / answered) if answered > 0 else 0,
                'total_wait_seconds': row['total_wait'] or 0,
                'avg_wait_seconds': round((row['total_wait'] or 0) / total) if total > 0 else 0,
                'avg_answered_wait_seconds': round((row['total_wait'] or 0) / answered) if answered > 0 else 0,
                'avg_abandoned_wait_seconds': round((row['abandoned_wait'] or 0) / abandoned) if abandoned > 0 else 0,
                'max_wait_seconds': row['max_wait'] or 0,
                'max_answered_wait_seconds': row['max_answered_wait'] or 0,
                'max_abandoned_wait_seconds': row['max_abandoned_wait'] or 0,
                'answer_speed': {
                    'within_15s': row['within_15s'] or 0,
                    'within_30s': row['within_30s'] or 0,
                    'within_60s': row['within_60s'] or 0,
                    'within_90s': row['within_90s'] or 0,
                    'over_90s': row['over_90s'] or 0,
                },
                'inbound_calls': row['inbound_calls'] or 0,
                'outbound_calls': row['outbound_calls'] or 0,
            }

    def _empty_stats_summary(self) -> dict:
        """Return an empty stats summary structure."""
        return {
            'total_calls': 0,
            'answered_calls': 0,
            'abandoned_calls': 0,
            'timeout_calls': 0,
            'transferred_calls': 0,
            'answer_rate': 0,
            'abandoned_rate': 0,
            'timeout_rate': 0,
            'total_duration_seconds': 0,
            'avg_duration_seconds': 0,
            'total_wait_seconds': 0,
            'avg_wait_seconds': 0,
            'avg_answered_wait_seconds': 0,
            'avg_abandoned_wait_seconds': 0,
            'max_wait_seconds': 0,
            'max_answered_wait_seconds': 0,
            'max_abandoned_wait_seconds': 0,
            'answer_speed': {
                'within_15s': 0,
                'within_30s': 0,
                'within_60s': 0,
                'within_90s': 0,
                'over_90s': 0,
            },
            'inbound_calls': 0,
            'outbound_calls': 0,
        }

    def get_agent_stats(self, start_date: str, end_date: str) -> list[dict]:
        """Get per-agent statistics for a date range.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            List of dicts with per-agent stats, sorted by total calls desc
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    agent_email,
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls,
                    SUM(total_duration_seconds) as total_duration,
                    SUM(total_wait_seconds) as total_wait
                FROM daily_call_stats
                WHERE stat_date BETWEEN ? AND ?
                  AND agent_email IS NOT NULL
                  AND agent_email != '__all__'
                GROUP BY agent_email
                ORDER BY SUM(answered_calls) DESC
            """, (start_date, end_date)).fetchall()

            result = []
            for row in rows:
                answered = row['answered_calls'] or 0
                total = row['total_calls'] or 0
                missed = (row['abandoned_calls'] or 0) + (row['timeout_calls'] or 0)

                result.append({
                    'agent_email': row['agent_email'],
                    'total_calls': total,
                    'answered_calls': answered,
                    'missed_calls': missed,
                    'total_duration_seconds': row['total_duration'] or 0,
                    'avg_duration_seconds': round((row['total_duration'] or 0) / answered) if answered > 0 else 0,
                })

            return result

    def get_queue_stats_report(self, start_date: str, end_date: str) -> list[dict]:
        """Get per-queue statistics for a date range.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            List of dicts with per-queue stats, sorted by total calls desc
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls,
                    SUM(total_wait_seconds) as total_wait,
                    MAX(max_wait_seconds) as max_wait,
                    SUM(answered_within_15s) as within_15s,
                    SUM(answered_within_30s) as within_30s,
                    SUM(answered_within_60s) as within_60s,
                    SUM(answered_within_90s) as within_90s,
                    SUM(answered_over_90s) as over_90s
                FROM daily_call_stats
                WHERE stat_date BETWEEN ? AND ?
                  AND queue_id IS NOT NULL
                GROUP BY queue_id, queue_name
                ORDER BY SUM(total_calls) DESC
            """, (start_date, end_date)).fetchall()

            result = []
            for row in rows:
                total = row['total_calls'] or 0
                answered = row['answered_calls'] or 0

                result.append({
                    'queue_id': row['queue_id'],
                    'queue_name': row['queue_name'] or f"Queue {row['queue_id']}",
                    'total_calls': total,
                    'answered_calls': answered,
                    'abandoned_calls': row['abandoned_calls'] or 0,
                    'timeout_calls': row['timeout_calls'] or 0,
                    'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                    'avg_wait_seconds': round((row['total_wait'] or 0) / total) if total > 0 else 0,
                    'max_wait_seconds': row['max_wait'] or 0,
                })

            return result

    def get_hourly_distribution(self, start_date: str, end_date: str,
                                queue_id: int = None) -> list[dict]:
        """Get hourly call distribution for charting.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            queue_id: Optional queue filter

        Returns:
            List of 24 dicts (one per hour) with call counts
        """
        with self._get_conn() as conn:
            query = """
                SELECT
                    stat_hour,
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls
                FROM hourly_call_stats
                WHERE stat_date BETWEEN ? AND ?
            """
            params = [start_date, end_date]

            if queue_id:
                query += " AND queue_id = ?"
                params.append(queue_id)

            query += " GROUP BY stat_hour ORDER BY stat_hour"

            rows = conn.execute(query, params).fetchall()

            return self._fill_hourly(rows, hour_key='stat_hour')

    def get_realtime_stats_today(self) -> dict:
        """Get real-time statistics for today from live data.

        This pulls directly from queued_calls and recording_log for
        today's data that hasn't been aggregated yet.

        Returns:
            Dict with today's statistics
        """
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            # Queue stats from queued_calls
            queue_row = conn.execute("""
                SELECT
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(COALESCE(wait_seconds, 0)) as total_wait,
                    MAX(wait_seconds) as max_wait,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds <= 15 THEN 1 ELSE 0 END) as within_15s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 15 AND wait_seconds <= 30 THEN 1 ELSE 0 END) as within_30s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 30 AND wait_seconds <= 60 THEN 1 ELSE 0 END) as within_60s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 60 AND wait_seconds <= 90 THEN 1 ELSE 0 END) as within_90s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 90 THEN 1 ELSE 0 END) as over_90s,
                    SUM(CASE WHEN status = 'abandoned' THEN COALESCE(wait_seconds, 0) ELSE 0 END) as abandoned_wait,
                    MAX(CASE WHEN status = 'answered' THEN wait_seconds ELSE 0 END) as max_answered_wait,
                    MAX(CASE WHEN status = 'abandoned' THEN wait_seconds ELSE 0 END) as max_abandoned_wait
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
            """, (today,)).fetchone()

            # Duration from recording_log
            recording_row = conn.execute("""
                SELECT
                    SUM(COALESCE(duration_seconds, 0)) as total_duration,
                    SUM(CASE WHEN call_type = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN call_type = 'outbound' THEN 1 ELSE 0 END) as outbound_calls
                FROM recording_log
                WHERE DATE(created_at) = ?
                  AND call_type IN ('inbound', 'outbound')
            """, (today,)).fetchone()

            total = queue_row['total_calls'] or 0
            answered = queue_row['answered_calls'] or 0
            abandoned = queue_row['abandoned_calls'] or 0
            timeout = queue_row['timeout_calls'] or 0

            return {
                'date': today,
                'is_realtime': True,
                'total_calls': total,
                'answered_calls': answered,
                'abandoned_calls': abandoned,
                'timeout_calls': timeout,
                'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                'abandoned_rate': round(abandoned / total * 100, 1) if total > 0 else 0,
                'timeout_rate': round(timeout / total * 100, 1) if total > 0 else 0,
                'total_duration_seconds': recording_row['total_duration'] or 0,
                'avg_duration_seconds': round((recording_row['total_duration'] or 0) / answered) if answered > 0 else 0,
                'total_wait_seconds': queue_row['total_wait'] or 0,
                'avg_wait_seconds': round((queue_row['total_wait'] or 0) / total) if total > 0 else 0,
                'avg_answered_wait_seconds': round((queue_row['total_wait'] or 0) / answered) if answered > 0 else 0,
                'avg_abandoned_wait_seconds': round((queue_row['abandoned_wait'] or 0) / abandoned) if abandoned > 0 else 0,
                'max_wait_seconds': queue_row['max_wait'] or 0,
                'max_answered_wait_seconds': queue_row['max_answered_wait'] or 0,
                'max_abandoned_wait_seconds': queue_row['max_abandoned_wait'] or 0,
                'answer_speed': {
                    'within_15s': queue_row['within_15s'] or 0,
                    'within_30s': queue_row['within_30s'] or 0,
                    'within_60s': queue_row['within_60s'] or 0,
                    'within_90s': queue_row['within_90s'] or 0,
                    'over_90s': queue_row['over_90s'] or 0,
                },
                'inbound_calls': recording_row['inbound_calls'] or 0,
                'outbound_calls': recording_row['outbound_calls'] or 0,
            }

    def get_realtime_agent_stats_today(self) -> list[dict]:
        """Get real-time per-agent statistics for today.

        Returns:
            List of dicts with per-agent stats for today
        """
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            # Get agent stats from queued_calls
            queue_rows = conn.execute("""
                SELECT
                    answered_by as agent_email,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status IN ('abandoned', 'timeout') THEN 1 ELSE 0 END) as missed_calls
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                  AND answered_by IS NOT NULL
                GROUP BY answered_by
            """, (today,)).fetchall()

            # Get duration from recording_log
            recording_rows = conn.execute("""
                SELECT
                    staff_email,
                    SUM(COALESCE(duration_seconds, 0)) as total_duration
                FROM recording_log
                WHERE DATE(created_at) = ?
                  AND staff_email IS NOT NULL
                  AND call_type IN ('inbound', 'outbound')
                GROUP BY staff_email
            """, (today,)).fetchall()

            duration_by_agent = {row['staff_email']: row['total_duration'] for row in recording_rows}

            result = []
            for row in queue_rows:
                agent = row['agent_email']
                answered = row['answered_calls'] or 0
                duration = duration_by_agent.get(agent, 0)

                result.append({
                    'agent_email': agent,
                    'total_calls': row['total_calls'] or 0,
                    'answered_calls': answered,
                    'missed_calls': row['missed_calls'] or 0,
                    'total_duration_seconds': duration,
                    'avg_duration_seconds': round(duration / answered) if answered > 0 else 0,
                })

            # Sort by answered calls descending
            result.sort(key=lambda x: x['answered_calls'], reverse=True)
            return result

    def _today_utc_range(self):
        """Return (start_utc, end_utc) strings covering today in local time."""
        today_local = datetime.now(_LOCAL_TZ).date()
        start = _LOCAL_TZ.localize(datetime.combine(today_local, datetime.min.time()))
        end = _LOCAL_TZ.localize(datetime.combine(today_local, datetime.max.time().replace(microsecond=0)))
        return (
            start.astimezone(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S'),
            end.astimezone(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S'),
        )

    def get_realtime_queue_stats_today(self) -> list[dict]:
        """Get real-time per-queue statistics for today.

        Returns:
            List of dicts with per-queue stats for today
        """
        start_utc, end_utc = self._today_utc_range()

        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(COALESCE(wait_seconds, 0)) as total_wait,
                    MAX(wait_seconds) as max_wait
                FROM queued_calls
                WHERE enqueued_at BETWEEN ? AND ?
                  AND queue_id IS NOT NULL
                GROUP BY queue_id, queue_name
                ORDER BY COUNT(*) DESC
            """, (start_utc, end_utc)).fetchall()

            result = []
            for row in rows:
                total = row['total_calls'] or 0
                answered = row['answered_calls'] or 0

                result.append({
                    'queue_id': row['queue_id'],
                    'queue_name': row['queue_name'] or f"Queue {row['queue_id']}",
                    'total_calls': total,
                    'answered_calls': answered,
                    'abandoned_calls': row['abandoned_calls'] or 0,
                    'timeout_calls': row['timeout_calls'] or 0,
                    'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                    'avg_wait_seconds': round((row['total_wait'] or 0) / total) if total > 0 else 0,
                    'max_wait_seconds': row['max_wait'] or 0,
                })

            return result

    def get_realtime_hourly_today(self) -> list[dict]:
        """Get real-time hourly distribution for today.

        Returns:
            List of 24 dicts with hourly call counts for today
        """
        start_utc, end_utc = self._today_utc_range()
        tz_offset_hours = int(datetime.now(_LOCAL_TZ).utcoffset().total_seconds() / 3600)
        offset_str = f'+{tz_offset_hours} hours' if tz_offset_hours >= 0 else f'{tz_offset_hours} hours'

        with self._get_conn() as conn:
            rows = conn.execute(f"""
                SELECT
                    CAST(strftime('%H', enqueued_at, '{offset_str}') AS INTEGER) as hour,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls
                FROM queued_calls
                WHERE enqueued_at BETWEEN ? AND ?
                GROUP BY hour
                ORDER BY hour
            """, (start_utc, end_utc)).fetchall()

            return self._fill_hourly(rows)

