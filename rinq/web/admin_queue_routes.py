"""Queue admin routes — CRUD for queues and members.

Extracted from web/routes.py. Registered via register(web_bp).
"""

import logging
import pytz
from datetime import datetime
from flask import request, redirect, url_for, flash, jsonify, render_template
from rinq.services.auth import admin_required, manager_required, get_current_user
from rinq.database.db import get_db
from rinq.config import config

_TZ = pytz.timezone('Australia/Sydney')


def _parse_local_dt(value: str):
    """Parse a datetime-local input (naive, treated as Australia/Sydney) to UTC ISO string."""
    dt = datetime.strptime(value, '%Y-%m-%dT%H:%M')
    dt_local = _TZ.localize(dt)
    return dt_local.astimezone(pytz.utc).isoformat()


def _pause_redirect(queue_id: int, user) -> str:
    """Redirect back to wherever the user came from — admin queues or manager queues."""
    if user.is_admin:
        return url_for('web.admin_queues') + f'#queue_{queue_id}'
    return url_for('web.queues') + f'#queue_{queue_id}'


def _utc_to_local(iso: str) -> str:
    """Convert a UTC ISO string to Australia/Sydney datetime-local input format."""
    if not iso:
        return ''
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(_TZ).strftime('%Y-%m-%dT%H:%M')

logger = logging.getLogger(__name__)

def _audit_tag(user):
    return f"session:{user.email}"

def register(bp):
    """Register queue admin routes on the given blueprint."""

    @bp.route('/admin/queue/create', methods=['POST'])
    @admin_required
    def create_queue():
        """Create a new queue."""
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        ring_strategy = request.form.get('ring_strategy', 'simultaneous')
        ring_timeout = request.form.get('ring_timeout', '30')
        ring_timeout = int(ring_timeout) if ring_timeout else 30
        ring_timeout = max(5, min(ring_timeout, 120))
        allow_self_service = request.form.get('allow_self_service') == '1'
        allow_voicemail_escape = request.form.get('allow_voicemail_escape') == '1'
        offer_callback = request.form.get('offer_callback') == '1'
        callback_threshold = request.form.get('callback_threshold', '60')
        callback_threshold = int(callback_threshold) if callback_threshold else 60
        escape_announcement_delay = request.form.get('escape_announcement_delay', '60')
        escape_announcement_delay = int(escape_announcement_delay) if escape_announcement_delay else 60
        escape_repeat_interval = request.form.get('escape_repeat_interval', '120')
        escape_repeat_interval = int(escape_repeat_interval) if escape_repeat_interval else 120
        reject_action = request.form.get('reject_action', 'continue')
        hold_music_id = request.form.get('hold_music_id')
        hold_music_id = int(hold_music_id) if hold_music_id else None
    
        if not name:
            flash("Queue name is required", "error")
            return redirect(url_for('web.admin'))
    
        user = get_current_user()
        db = get_db()
    
        try:
            queue_id = db.create_queue(
                data={
                    'name': name,
                    'description': description or None,
                    'ring_strategy': ring_strategy,
                    'ring_timeout': ring_timeout,
                    'allow_self_service': allow_self_service,
                    'allow_voicemail_escape': allow_voicemail_escape,
                    'offer_callback': offer_callback,
                    'callback_threshold': callback_threshold,
                    'escape_announcement_delay': escape_announcement_delay,
                    'escape_repeat_interval': escape_repeat_interval,
                    'reject_action': reject_action,
                    'hold_music_id': hold_music_id,
                },
                created_by=_audit_tag(user)
            )
            db.log_activity(
                action="create_queue",
                target=name,
                details=f"Created queue ID {queue_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Created queue '{name}'", "success")
        except Exception as e:
            flash(f"Failed to create queue: {e}", "error")
    
        return redirect(url_for('web.admin'))
    
    
    @bp.route('/admin/queue/<int:queue_id>/member/add', methods=['POST'])
    @admin_required
    def add_queue_member(queue_id):
        """Add a member to a queue."""
        user_email = request.form.get('user_email', '').strip().lower()
    
        if not user_email:
            flash("User email is required", "error")
            return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')
    
        user = get_current_user()
        db = get_db()
    
        try:
            db.add_queue_member(
                queue_id=queue_id,
                user_email=user_email,
                priority=0,  # Default priority
                created_by=_audit_tag(user)
            )
    
            # Note: ring_browser and ring_sip default to True in users table,
            # so no need to create device entries
    
            db.log_activity(
                action="add_queue_member",
                target=user_email,
                details=f"Added to queue {queue_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Added {user_email} to queue", "success")
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                flash(f"{user_email} is already in this queue", "error")
            else:
                flash(f"Failed to add member: {e}", "error")
    
        return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')
    
    
    @bp.route('/admin/queue/<int:queue_id>/member/<int:member_id>/remove', methods=['POST'])
    @admin_required
    def remove_queue_member(queue_id, member_id):
        """Remove a member from a queue."""
        user = get_current_user()
        db = get_db()
    
        db.remove_queue_member(member_id)
        db.log_activity(
            action="remove_queue_member",
            target=str(member_id),
            details=f"Removed from queue {queue_id}",
            performed_by=_audit_tag(user)
        )
        flash("Removed from queue", "success")
    
        return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')
    
    
    @bp.route('/admin/queue/<int:queue_id>/update', methods=['POST'])
    @admin_required
    def update_queue(queue_id):
        """Update a queue's settings."""
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        ring_strategy = request.form.get('ring_strategy', 'simultaneous')
        ring_timeout = request.form.get('ring_timeout', '30')
        ring_timeout = int(ring_timeout) if ring_timeout else 30
        ring_timeout = max(5, min(ring_timeout, 120))
        allow_self_service = request.form.get('allow_self_service') == '1'
        allow_voicemail_escape = request.form.get('allow_voicemail_escape') == '1'
        offer_callback = request.form.get('offer_callback') == '1'
        callback_threshold = request.form.get('callback_threshold', '60')
        callback_threshold = int(callback_threshold) if callback_threshold else 60
        escape_announcement_delay = request.form.get('escape_announcement_delay', '60')
        escape_announcement_delay = int(escape_announcement_delay) if escape_announcement_delay else 60
        escape_repeat_interval = request.form.get('escape_repeat_interval', '120')
        escape_repeat_interval = int(escape_repeat_interval) if escape_repeat_interval else 120
        reject_action = request.form.get('reject_action', 'continue')
        hold_music_id = request.form.get('hold_music_id')
        hold_music_id = int(hold_music_id) if hold_music_id else None
        max_wait_plays = request.form.get('max_wait_plays', '').strip()
        max_wait_time = int(max_wait_plays) if max_wait_plays else None
    
        if not name:
            flash("Queue name is required", "error")
            return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')
    
        user = get_current_user()
        db = get_db()
    
        try:
            db.update_queue(
                queue_id=queue_id,
                data={
                    'name': name,
                    'description': description or None,
                    'ring_strategy': ring_strategy,
                    'ring_timeout': ring_timeout,
                    'allow_self_service': allow_self_service,
                    'allow_voicemail_escape': allow_voicemail_escape,
                    'offer_callback': offer_callback,
                    'callback_threshold': callback_threshold,
                    'escape_announcement_delay': escape_announcement_delay,
                    'escape_repeat_interval': escape_repeat_interval,
                    'reject_action': reject_action,
                    'hold_music_id': hold_music_id,
                    'max_wait_time': max_wait_time,
                },
                updated_by=_audit_tag(user)
            )
            db.log_activity(
                action="update_queue",
                target=name,
                details=f"Updated queue {queue_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Updated queue '{name}'", "success")
        except Exception as e:
            flash(f"Failed to update queue: {e}", "error")
    
        return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')
    
    
    @bp.route('/admin/queue/<int:queue_id>/manager/add', methods=['POST'])
    @admin_required
    def add_queue_manager(queue_id):
        user_email = request.form.get('user_email', '').strip().lower()
        if not user_email:
            flash("User email is required.", "error")
            return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')

        user = get_current_user()
        db = get_db()
        try:
            db.add_queue_manager(queue_id, user_email, _audit_tag(user))
            db.log_activity(action="add_queue_manager", target=user_email,
                            details=f"Added as manager of queue {queue_id}",
                            performed_by=_audit_tag(user))
            flash(f"Added {user_email} as queue manager.", "success")
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                flash(f"{user_email} is already a manager of this queue.", "error")
            else:
                flash(f"Failed to add manager: {e}", "error")
        return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')


    @bp.route('/admin/queue/<int:queue_id>/manager/remove', methods=['POST'])
    @admin_required
    def remove_queue_manager(queue_id):
        user_email = request.form.get('user_email', '').strip().lower()
        if not user_email:
            flash("User email is required.", "error")
            return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')

        user = get_current_user()
        db = get_db()
        db.remove_queue_manager(queue_id, user_email)
        db.log_activity(action="remove_queue_manager", target=user_email,
                        details=f"Removed as manager of queue {queue_id}",
                        performed_by=_audit_tag(user))
        flash(f"Removed {user_email} as queue manager.", "success")
        return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')


    @bp.route('/admin/queue/<int:queue_id>/schedule-pause', methods=['POST'])
    @manager_required
    def schedule_queue_pause(queue_id):
        """Schedule a pause window for a queue.

        Both fields are optional:
        - paused_from: defaults to now (immediate pause)
        - paused_until: if omitted, queue is paused indefinitely
        """
        user = get_current_user()
        db = get_db()

        if not user.is_admin and not db.is_queue_manager(queue_id, user.email):
            return "Access denied", 403

        paused_from_str = request.form.get('paused_from', '').strip()
        paused_until_str = request.form.get('paused_until', '').strip()

        try:
            if paused_from_str:
                paused_from_utc = _parse_local_dt(paused_from_str)
            else:
                paused_from_utc = datetime.now(pytz.utc).isoformat()

            paused_until_utc = _parse_local_dt(paused_until_str) if paused_until_str else None
        except ValueError:
            flash("Invalid date/time format.", "error")
            return redirect(_pause_redirect(queue_id, user))

        if paused_until_utc and paused_from_utc >= paused_until_utc:
            flash("'Resume at' must be after 'Pause from'.", "error")
            return redirect(_pause_redirect(queue_id, user))

        db.schedule_queue_pause(queue_id, paused_from_utc, paused_until_utc, _audit_tag(user))
        db.log_activity(
            action="schedule_queue_pause",
            target=str(queue_id),
            details=f"Pause {paused_from_utc} → {paused_until_utc or 'indefinite'}",
            performed_by=_audit_tag(user)
        )
        flash("Pause scheduled." if paused_from_str else "Queue paused.", "success")
        return redirect(_pause_redirect(queue_id, user))


    @bp.route('/admin/queue/<int:queue_id>/clear-pause', methods=['POST'])
    @manager_required
    def clear_queue_pause(queue_id):
        """Remove the scheduled pause window from a queue."""
        user = get_current_user()
        db = get_db()

        if not user.is_admin and not db.is_queue_manager(queue_id, user.email):
            return "Access denied", 403

        db.clear_queue_pause(queue_id, _audit_tag(user))
        db.log_activity(
            action="clear_queue_pause",
            target=str(queue_id),
            details="Pause cleared",
            performed_by=_audit_tag(user)
        )
        flash("Pause cleared.", "success")
        return redirect(_pause_redirect(queue_id, user))


    @bp.route('/admin/queue/<int:queue_id>/ring-timeout', methods=['POST'])
    @manager_required
    def set_queue_ring_timeout(queue_id):
        """Update just a queue's ring timeout. Available to queue managers (not
        only admins) so they can self-serve this one setting without admin access."""
        user = get_current_user()
        db = get_db()

        if not user.is_admin and not db.is_queue_manager(queue_id, user.email):
            return "Access denied", 403

        raw = request.form.get('ring_timeout', '').strip()
        try:
            ring_timeout = int(raw)
        except (TypeError, ValueError):
            flash("Ring timeout must be a whole number of seconds.", "error")
            return redirect(_pause_redirect(queue_id, user))
        ring_timeout = max(5, min(ring_timeout, 120))

        db.set_queue_ring_timeout(queue_id, ring_timeout, _audit_tag(user))
        db.log_activity(
            action="set_queue_ring_timeout",
            target=str(queue_id),
            details=f"Ring timeout set to {ring_timeout}s",
            performed_by=_audit_tag(user)
        )
        flash(f"Calls now ring for {ring_timeout} seconds before going to voicemail.", "success")
        return redirect(_pause_redirect(queue_id, user))


    @bp.route('/admin/queue/<int:queue_id>/delete', methods=['POST'])
    @admin_required
    def delete_queue(queue_id):
        """Delete a queue."""
        user = get_current_user()
        db = get_db()
    
        try:
            db.delete_queue(queue_id)
            db.log_activity(
                action="delete_queue",
                target=str(queue_id),
                details="Deleted queue",
                performed_by=_audit_tag(user)
            )
            flash("Queue deleted", "success")
        except Exception as e:
            flash(f"Failed to delete queue: {e}", "error")
    
        return redirect(url_for('web.admin_queues'))
    
