"""Storage admin routes — disk usage, recording cache retention, job health.

Extracted pattern: registered via register(web_bp).

Exists because the scheduled jobs that keep this server from filling up all
failed silently for months (see CLAUDE.md gotcha 35). Nothing in the product
showed disk usage, whether the purge had ever run, or whether recordings were
safely archived. This page makes all three visible.
"""

import logging
from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash

from rinq.database.db import get_db
from rinq.services.auth import admin_required, get_current_user
from rinq.services.recording_service import (
    DEFAULT_RETENTION_DAYS,
    RETENTION_SETTING_KEY,
    get_retention_days,
    recording_service,
)
from rinq.web.util import flash_error

logger = logging.getLogger(__name__)

# Retention bounds. Below a day the purge would evict calls staff are still
# reviewing; beyond a year it stops being a cache and the disk fills again.
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365

# The scheduled jobs, their cron cadence, and how long after their last run
# we should start calling them stale. Generous multiples of the interval so a
# single missed tick doesn't cry wolf — these flag jobs that have *stopped*.
SCHEDULED_JOBS = [
    {
        'key': 'recordings_purged',
        'name': 'Recordings purge',
        'schedule': 'Daily, 3am',
        'purpose': 'Frees disk by dropping cached recordings past the retention window',
        'stale_after_hours': 48,
    },
    {
        'key': 'stats_aggregated',
        'name': 'Stats aggregation',
        'schedule': 'Every 15 minutes',
        'purpose': 'Builds the daily and hourly figures behind Reports',
        'stale_after_hours': 24,
    },
    {
        'key': 'queue_cleanup',
        'name': 'Queue cleanup',
        'schedule': 'Every 5 minutes',
        'purpose': 'Clears finished queue calls and stale ring attempts',
        'stale_after_hours': 6,
    },
    {
        'key': 'address_book_synced',
        'name': 'Address book sync',
        'schedule': 'Daily, 2am',
        'purpose': 'Pulls contacts from Peter into the directory',
        'stale_after_hours': 48,
    },
]


def _hours_since(iso: str):
    """Hours between an ISO timestamp and now, or None if unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _build_job_rows(db):
    """Pair each scheduled job with its last logged run and a health verdict."""
    last_runs = db.get_last_activity([job['key'] for job in SCHEDULED_JOBS])

    rows = []
    for job in SCHEDULED_JOBS:
        entry = last_runs.get(job['key'])
        hours = _hours_since(entry['performed_at']) if entry else None

        if entry is None:
            status = 'never'
        elif hours is not None and hours > job['stale_after_hours']:
            status = 'stale'
        else:
            status = 'ok'

        rows.append({
            **job,
            'last_run': entry['performed_at'] if entry else None,
            'details': entry['details'] if entry else None,
            'hours_since': hours,
            'status': status,
        })
    return rows


def register(bp):
    """Register storage admin routes on the given blueprint."""

    @bp.route('/admin/storage')
    @admin_required
    def admin_storage():
        """Disk usage, recording cache health, and scheduled job status."""
        db = get_db()

        overview = recording_service.get_storage_overview()
        counts = db.get_recording_storage_stats()

        archived_percent = None
        if counts['with_local']:
            archived_percent = round(
                (counts['with_local'] - counts['local_only']) / counts['with_local'] * 100
            )

        return render_template(
            'admin_storage.html',
            current_user=get_current_user(),
            active_nav='admin',
            overview=overview,
            counts=counts,
            archived_percent=archived_percent,
            retention_days=get_retention_days(db),
            default_retention_days=DEFAULT_RETENTION_DAYS,
            min_retention_days=MIN_RETENTION_DAYS,
            max_retention_days=MAX_RETENTION_DAYS,
            jobs=_build_job_rows(db),
        )

    @bp.route('/admin/storage/retention', methods=['POST'])
    @admin_required
    def admin_storage_set_retention():
        """Update how long recordings stay cached on local disk."""
        raw = (request.form.get('retention_days') or '').strip()
        try:
            days = int(raw)
        except ValueError:
            flash_error(f"Retention must be a whole number of days, got {raw!r}")
            return redirect(url_for('web.admin_storage'))

        if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
            flash_error(
                f"Retention must be between {MIN_RETENTION_DAYS} and "
                f"{MAX_RETENTION_DAYS} days, got {days}"
            )
            return redirect(url_for('web.admin_storage'))

        user = get_current_user()
        db = get_db()
        db.set_bot_setting(RETENTION_SETTING_KEY, str(days), user.email)
        db.log_activity('retention_changed', f'{days}d',
                        f"Recording cache retention set to {days} days", user.email)
        flash(f'Recordings now stay on disk for {days} days.', 'success')
        return redirect(url_for('web.admin_storage'))
