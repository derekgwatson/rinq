"""
Reporting service for Rinq.

Provides call statistics, agent performance metrics, and queue analytics.
Handles both real-time data (today) and historical aggregated data.
"""

import logging
from datetime import datetime, timedelta

import pytz

from rinq.database.db import get_db

logger = logging.getLogger(__name__)

# Default timezone for date calculations (Australian Eastern)
LOCAL_TZ = pytz.timezone('Australia/Sydney')


class ReportingService:
    """Service for generating call reports and statistics."""

    @property
    def db(self):
        return get_db()

    def get_report_data(self, period: str = 'today', queue_name: str = None, queue_names: list = None,
                        agent_emails: list = None, team_emails: list = None) -> dict:
        """Get complete report data for a time period.

        Args:
            period: 'today', 'yesterday', 'this_week', 'last_week',
                   'this_month', or 'YYYY-MM-DD:YYYY-MM-DD' for custom range
            queue_name: Optional single queue name to filter by
            queue_names: Optional list of queue names (for "all queues" filter)
            agent_emails: Optional list of agent emails to include outbound calls for
            team_emails: Optional list of team member emails (from Peter's manager
                        reportees). Used for agent stats independently of queue filtering.

        Returns:
            Dict with summary, agent_stats, queue_stats, hourly_distribution
        """
        parsed = self._parse_period(period)
        start_utc = parsed['start_utc']
        end_utc = parsed['end_utc']
        start_date = parsed['start_date']
        end_date = parsed['end_date']
        period_label = parsed['label']

        # Determine if we're looking at "today" (for queue stats source selection)
        is_today = period == 'today'

        # Calculate timezone offset for hourly distribution (hours ahead of UTC)
        now_local = datetime.now(LOCAL_TZ)
        tz_offset_hours = int(now_local.utcoffset().total_seconds() / 3600)

        # Get call statistics from call_log
        summary = self.db.get_call_log_stats(start_utc, end_utc, queue_name=queue_name, queue_names=queue_names,
                                             agent_emails=agent_emails, team_emails=team_emails)
        agent_stats = self.db.get_call_log_by_agent(start_utc, end_utc, queue_name=queue_name, queue_names=queue_names,
                                                     agent_emails=agent_emails, team_emails=team_emails)
        hourly = self.db.get_call_log_hourly(start_utc, end_utc, tz_offset_hours, queue_name=queue_name, queue_names=queue_names,
                                             agent_emails=agent_emails, team_emails=team_emails)

        # Queue stats still come from queued_calls for queue-specific metrics
        if is_today:
            queue_stats = self.db.get_realtime_queue_stats_today()
        else:
            queue_stats = self.db.get_queue_stats_report(start_date, end_date)

        # Enrich agent stats with display names
        agent_stats = self._enrich_agent_names(agent_stats)

        # Calculate max values for visual scaling
        max_agent_calls = max((a['answered_calls'] for a in agent_stats), default=0)
        max_agent_duration = max((a.get('total_duration_seconds', 0) or 0 for a in agent_stats), default=0)
        max_queue_calls = max((q['total_calls'] for q in queue_stats), default=0)
        max_hourly_calls = max((h['total_calls'] for h in hourly), default=0)

        return {
            'period': period,
            'period_label': period_label,
            'start_date': start_date,
            'end_date': end_date,
            'is_today': is_today,
            'summary': summary,
            'agent_stats': agent_stats,
            'queue_stats': queue_stats,
            'hourly': hourly,
            'max_agent_calls': max_agent_calls,
            'max_agent_duration': max_agent_duration,
            'max_queue_calls': max_queue_calls,
            'max_hourly_calls': max_hourly_calls,
        }

    def _parse_period(self, period: str) -> dict:
        """Parse period string into date boundaries.

        Args:
            period: Period identifier or date range

        Returns:
            Dict with:
                - start_utc, end_utc: UTC timestamp strings for call_log queries
                - start_date, end_date: Local date strings (YYYY-MM-DD) for display/aggregated tables
                - label: Human-readable period label
        """
        # Use local timezone for date calculations so "today" matches user's day
        today = datetime.now(LOCAL_TZ).date()

        if period == 'today':
            start_local = today
            end_local = today
            label = 'Today'

        elif period == 'yesterday':
            start_local = today - timedelta(days=1)
            end_local = start_local
            label = 'Yesterday'

        elif period == 'this_week':
            # Monday to today
            start_local = today - timedelta(days=today.weekday())
            end_local = today
            label = 'This Week'

        elif period == 'last_week':
            # Previous Monday to Sunday
            end_local = today - timedelta(days=today.weekday() + 1)
            start_local = end_local - timedelta(days=6)
            label = 'Last Week'

        elif period == 'this_month':
            start_local = today.replace(day=1)
            end_local = today
            label = 'This Month'

        elif period == 'last_month':
            # First day of last month to last day of last month
            first_of_this_month = today.replace(day=1)
            last_of_last_month = first_of_this_month - timedelta(days=1)
            first_of_last_month = last_of_last_month.replace(day=1)
            start_local = first_of_last_month
            end_local = last_of_last_month
            label = 'Last Month'

        elif ':' in period:
            # Custom range: YYYY-MM-DD:YYYY-MM-DD
            try:
                start_str, end_str = period.split(':')
                start_local = datetime.strptime(start_str, '%Y-%m-%d').date()
                end_local = datetime.strptime(end_str, '%Y-%m-%d').date()
                label = f"{start_str} to {end_str}"
            except (ValueError, AttributeError):
                logger.warning(f"Invalid date range: {period}, defaulting to today")
                start_local = today
                end_local = today
                label = 'Today'

        else:
            # Assume single date
            try:
                start_local = datetime.strptime(period, '%Y-%m-%d').date()
                end_local = start_local
                label = period
            except ValueError:
                logger.warning(f"Invalid period: {period}, defaulting to today")
                start_local = today
                end_local = today
                label = 'Today'

        # Convert local date boundaries to UTC timestamps
        # Start of day in local time -> UTC
        start_dt = LOCAL_TZ.localize(datetime.combine(start_local, datetime.min.time()))
        start_utc = start_dt.astimezone(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S')

        # End of day in local time -> UTC (23:59:59)
        end_dt = LOCAL_TZ.localize(datetime.combine(end_local, datetime.max.time().replace(microsecond=0)))
        end_utc = end_dt.astimezone(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S')

        return {
            'start_utc': start_utc,
            'end_utc': end_utc,
            'start_date': start_local.strftime('%Y-%m-%d'),
            'end_date': end_local.strftime('%Y-%m-%d'),
            'label': label,
        }

    def _enrich_agent_names(self, agent_stats: list[dict]) -> list[dict]:
        """Add display names to agent stats.

        Extracts the name portion from email addresses and looks up
        staff names if available.
        """
        for agent in agent_stats:
            email = agent.get('agent_email', '')
            if email:
                # Try to get a friendly name from the email
                name_part = email.split('@')[0] if '@' in email else email
                # Convert to title case and replace dots/underscores
                name_part = name_part.replace('.', ' ').replace('_', ' ').title()
                agent['display_name'] = name_part
            else:
                agent['display_name'] = 'Unknown'
        return agent_stats

    def aggregate_stats_for_date(self, target_date: str = None) -> dict:
        """Aggregate statistics for a specific date.

        This should be called daily (via Skye) before queue cleanup.

        Args:
            target_date: Date to aggregate (YYYY-MM-DD). Defaults to yesterday.

        Returns:
            Dict with aggregation results
        """
        daily_result = self.db.aggregate_daily_stats(target_date)
        hourly_result = self.db.aggregate_hourly_stats(target_date)

        logger.info(
            f"Aggregated stats for {daily_result['date']}: "
            f"{daily_result['records_created']} daily records, "
            f"{hourly_result['records_created']} hourly records"
        )

        return {
            'date': daily_result['date'],
            'daily_records': daily_result['records_created'],
            'hourly_records': hourly_result['records_created'],
        }

    def format_duration(self, seconds: int) -> str:
        """Format seconds as HH:MM:SS or MM:SS."""
        if seconds is None or seconds == 0:
            return '00:00'

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"

    def format_wait_time(self, seconds: int) -> str:
        """Format wait time in a human-readable way."""
        if seconds is None or seconds == 0:
            return '0s'

        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            secs = seconds % 60
            if secs > 0:
                return f"{minutes}m {secs}s"
            return f"{minutes}m"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}m"


# Singleton instance
_service = None


def get_reporting_service() -> ReportingService:
    """Get reporting service singleton."""
    global _service
    if _service is None:
        _service = ReportingService()
    return _service
