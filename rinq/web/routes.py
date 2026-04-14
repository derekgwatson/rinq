"""
Web routes for Rinq (Cloud Phone System).

Provides dashboard for:
- Viewing and managing phone numbers
- Setting up forwarding
- Managing call flows, queues, and schedules
- Viewing activity log
"""

import json
import logging
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, make_response, session

logger = logging.getLogger(__name__)

from rinq.services.auth import login_required, admin_required, get_current_user
from rinq.services.twilio_service import get_twilio_service
from rinq.database.db import get_db
from rinq.config import config
from rinq.tenant.context import get_twilio_config
try:
    from shared.config.ports import get_shared_css_context
except ImportError:
    def get_shared_css_context():
        return {'shared_css_url': None}

web_bp = Blueprint('web', __name__, template_folder='templates')

# Audio type definitions — used by admin_audio page and TTS generation
AUDIO_TYPE_DEFS = [
    {'value': 'greeting', 'label': 'Greeting', 'category': 'Call Flow', 'per_flow': True,
     'description': 'Plays when a call first comes in — assigned per call flow',
     'default_text': 'Welcome to Watson Blinds and Awnings. Your call is important to us.'},
    {'value': 'closed', 'label': 'Closed Message', 'category': 'Call Flow', 'per_flow': True,
     'description': 'Plays outside business hours — assigned per call flow',
     'default_text': 'We are currently closed. Our hours are Monday to Friday, 8am to 5pm.'},
    {'value': 'voicemail', 'label': 'Voicemail Prompt', 'category': 'Call Flow', 'per_flow': True,
     'description': 'Before recording — assigned per voicemail destination',
     'default_text': 'Please leave a brief message with your name and phone number and we\'ll get back to you.'},
    {'value': 'hold_music', 'label': 'Hold Music', 'category': 'Queue', 'per_flow': True,
     'description': 'Loops while waiting in a queue — assigned per queue',
     'default_text': ''},
    {'value': 'queue_welcome_vm_cb', 'label': 'Queue Welcome (VM + Callback)', 'category': 'Queue',
     'description': 'Announces both escape options while waiting in queue',
     'default_text': 'Press 1 at any time to leave a voicemail, or press 2 to request a callback instead of waiting.'},
    {'value': 'queue_welcome_vm', 'label': 'Queue Welcome (VM Only)', 'category': 'Queue',
     'description': 'Announces voicemail option while waiting in queue',
     'default_text': 'Press 1 at any time to leave a voicemail instead of waiting.'},
    {'value': 'queue_welcome_cb', 'label': 'Queue Welcome (CB Only)', 'category': 'Queue',
     'description': 'Announces callback option while waiting in queue',
     'default_text': 'Press 2 at any time to request a callback instead of waiting.'},
    {'value': 'callback_reminder', 'label': 'Callback Reminder', 'category': 'Queue',
     'description': 'Short reminder about callback option between announcements',
     'default_text': 'Press 2 to request a callback. We\'ll call you back without losing your place in line.'},
    {'value': 'voicemail_escape', 'label': 'Voicemail Escape', 'category': 'Queue Exit',
     'description': 'Plays after caller presses 1, right before the recording tone',
     'default_text': 'No problem. Please leave your message after the tone and we\'ll get back to you as soon as possible.'},
    {'value': 'callback_confirm', 'label': 'Callback Confirm', 'category': 'Queue Exit',
     'description': 'Plays after caller presses 2, before hanging up',
     'default_text': 'No problem. We have your number and someone will call you back shortly. Goodbye.'},
    {'value': 'queue_no_agents', 'label': 'Queue No Agents', 'category': 'Queue Exit',
     'description': 'Plays when queue times out or no agents available, before voicemail',
     'default_text': 'Sorry, our team is unable to take your call right now. Please leave a message after the tone.'},
    {'value': 'ext_prompt', 'label': 'Extension Prompt', 'category': 'Extension',
     'description': 'Auto-attendant prompt to enter an extension number',
     'default_text': 'Please enter the extension of the person you are trying to reach.'},
    {'value': 'ext_unavailable', 'label': 'Extension Unavailable', 'category': 'Extension',
     'description': 'Plays when dialled extension doesn\'t answer or is on DND',
     'default_text': 'Sorry, that extension is not available right now. Please try again later.'},
    {'value': 'reopen_prefix', 'label': 'Reopen Prefix', 'category': 'Reopen',
     'description': 'Played before the day and time — e.g. "We reopen"',
     'default_text': 'We reopen'},
    {'value': 'reopen_day_tomorrow', 'label': 'Tomorrow', 'category': 'Reopen',
     'description': 'Day snippet for "tomorrow"', 'default_text': 'tomorrow'},
    {'value': 'reopen_day_later_today', 'label': 'Later Today', 'category': 'Reopen',
     'description': 'Day snippet for "later today"', 'default_text': 'later today'},
    {'value': 'reopen_day_monday', 'label': 'Monday', 'category': 'Reopen',
     'description': 'Day snippet', 'default_text': 'Monday'},
    {'value': 'reopen_day_tuesday', 'label': 'Tuesday', 'category': 'Reopen',
     'description': 'Day snippet', 'default_text': 'Tuesday'},
    {'value': 'reopen_day_wednesday', 'label': 'Wednesday', 'category': 'Reopen',
     'description': 'Day snippet', 'default_text': 'Wednesday'},
    {'value': 'reopen_day_thursday', 'label': 'Thursday', 'category': 'Reopen',
     'description': 'Day snippet', 'default_text': 'Thursday'},
    {'value': 'reopen_day_friday', 'label': 'Friday', 'category': 'Reopen',
     'description': 'Day snippet', 'default_text': 'Friday'},
    {'value': 'reopen_day_saturday', 'label': 'Saturday', 'category': 'Reopen',
     'description': 'Day snippet', 'default_text': 'Saturday'},
    {'value': 'reopen_day_sunday', 'label': 'Sunday', 'category': 'Reopen',
     'description': 'Day snippet', 'default_text': 'Sunday'},
    {'value': 'reopen_time_0830', 'label': 'at 8:30 AM', 'category': 'Reopen',
     'description': 'Time snippet for 8:30 AM opening', 'default_text': 'at 8 30 AY EM'},
    {'value': 'reopen_time_0900', 'label': 'at 9:00 AM', 'category': 'Reopen',
     'description': 'Time snippet for 9:00 AM opening', 'default_text': 'at 9 AY EM'},
]


def _audit_tag(user):
    """Build an audit performer tag from a logged-in user."""
    return f"session:{user.email}"


@web_bp.context_processor
def utility_processor():
    """Add utility functions and config to templates."""
    ctx = get_shared_css_context()
    ctx['config'] = config

    # Active nav based on path
    path = request.path
    if path.startswith('/admin/visualizer'):
        ctx['active_nav'] = 'visualizer'
    elif path.startswith('/admin'):
        ctx['active_nav'] = 'admin'
    elif path.startswith('/activity'):
        ctx['active_nav'] = 'activity'
    elif path.startswith('/recordings'):
        ctx['active_nav'] = 'recordings'
    elif path.startswith('/reports'):
        ctx['active_nav'] = 'reports'
    elif path.startswith('/my-devices'):
        ctx['active_nav'] = 'my_devices'
    elif path.startswith('/my-desk-phone'):
        ctx['active_nav'] = 'my_desk_phone'
    elif path.startswith('/desk-phones'):
        ctx['active_nav'] = 'desk_phones'
    elif path.startswith('/setup'):
        ctx['active_nav'] = 'setup'
    elif path.startswith('/phone'):
        ctx['active_nav'] = 'phone'
    elif path == '/':
        ctx['active_nav'] = 'home'
    else:
        ctx['active_nav'] = None

    def time_ago(dt_str):
        """Get human-readable relative time string."""
        if not dt_str:
            return 'Never'
        try:
            if isinstance(dt_str, str):
                # Handle SQLite format: "2024-01-15 10:30:00" or ISO "2024-01-15T10:30:00"
                dt_str_clean = dt_str.replace('T', ' ')[:19]
                dt = datetime.strptime(dt_str_clean, '%Y-%m-%d %H:%M:%S')
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt_str
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

            delta = datetime.now(timezone.utc) - dt
            seconds = delta.total_seconds()

            if seconds < 60:
                return 'Just now'
            elif seconds < 3600:
                minutes = int(seconds / 60)
                return f'{minutes} min{"s" if minutes != 1 else ""} ago'
            elif seconds < 86400:
                hours = int(seconds / 3600)
                return f'{hours} hour{"s" if hours != 1 else ""} ago'
            elif seconds < 604800:
                days = int(seconds / 86400)
                return f'{days} day{"s" if days != 1 else ""} ago'
            else:
                return dt.strftime('%Y-%m-%d')
        except (ValueError, AttributeError, TypeError):
            return str(dt_str)[:16] if dt_str else 'Never'

    ctx['time_ago'] = time_ago
    from rinq.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    if tenant:
        ctx['softphone_enabled'] = bool(
            get_twilio_config('twilio_api_key') and get_twilio_config('twilio_api_secret') and get_twilio_config('twilio_twiml_app_sid')
        )
    else:
        ctx['softphone_enabled'] = False

    return ctx


def _get_browser_identity(user):
    """Get the browser identity for a user (matches token generation)."""
    return user.email.replace('@', '_at_').replace('.', '_')


@web_bp.route('/manifest.json')
def manifest():
    """Dynamic PWA manifest — uses product_name from config."""
    manifest_data = {
        "name": f"{config.product_name} Phone",
        "short_name": "Phone",
        "description": config.description,
        "start_url": "/phone",
        "scope": "/phone",
        "display": "standalone",
        "background_color": "#2c3e50",
        "theme_color": "#e74c3c",
        "icons": [
            {"src": "/static/phone-icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/phone-icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    response = make_response(jsonify(manifest_data))
    response.headers['Content-Type'] = 'application/manifest+json'
    return response


@web_bp.route('/')
def index():
    """Landing page (rinq.cc, unauthenticated) or user dashboard."""
    if not session.get('user_id'):
        # Show landing page only on the SaaS domain
        host = request.host.split(':')[0].lower()
        if host in ('rinq.cc', 'localhost', '127.0.0.1'):
            return render_template('landing.html', now=datetime.now())
        # Other domains (e.g. tina.watsonblinds.com.au) go straight to login
        return redirect(url_for('standalone_auth.login'))

    service = get_twilio_service()
    user = get_current_user()

    if not service.is_configured:
        return render_template('setup_required.html',
                             current_user=user)

    db = get_db()

    # Get or create staff extension
    staff_ext = db.get_or_create_staff_extension(user.email, f'session:{user.email}')

    # Get user's queue memberships
    my_queues = db.get_user_queue_memberships(user.email)
    my_queue_ids = {q['queue_id'] for q in my_queues}

    # Get available self-service queues
    available_queues = db.get_self_service_queues()

    # Get phone assignments (for making calls)
    assignments = db.get_assignments_for_user(user.email)

    # Extension directory dial-in number (from bot settings)
    ext_directory_number = db.get_bot_setting('extension_directory_number')

    return render_template('index.html',
                         staff_ext=staff_ext,
                         my_queues=my_queues,
                         my_queue_ids=my_queue_ids,
                         available_queues=available_queues,
                         assignments=assignments,
                         ext_directory_number=ext_directory_number,
                         current_user=user)


@web_bp.route('/settings', methods=['POST'])
@login_required
def update_settings():
    """Update staff extension settings."""
    user = get_current_user()
    db = get_db()

    # Validate Australian mobile number if provided
    forward_to = request.form.get('forward_to', '').strip()
    if forward_to:
        from rinq.services.phone import normalize_au_mobile, is_valid_au_mobile
        forward_to = normalize_au_mobile(forward_to)
        if not forward_to or not is_valid_au_mobile(forward_to):
            flash('Invalid mobile number. Must be an Australian mobile (04XX XXX XXX).', 'error')
            return redirect(url_for('web.index'))
    else:
        forward_to = None

    # Handle extension number change
    new_extension = request.form.get('extension', '').strip()
    if new_extension:
        staff_ext = db.get_staff_extension(user.email)
        if staff_ext and new_extension != staff_ext['extension']:
            result = db.set_extension_number(user.email, new_extension, f'session:{user.email}')
            if not result['success']:
                flash(result['error'], 'error')
                return redirect(url_for('web.index'))

    data = {
        'forward_to': forward_to,
        'forward_mode': request.form.get('forward_mode', 'always'),
        'hide_mobile': request.form.get('hide_mobile') == '1',
    }

    db.update_staff_extension(user.email, data, f'session:{user.email}')
    flash('Settings updated.', 'success')
    return redirect(url_for('web.index'))


@web_bp.route('/queue/<int:queue_id>/toggle', methods=['POST'])
@login_required
def toggle_queue(queue_id):
    """Toggle membership in a self-service queue."""
    user = get_current_user()
    db = get_db()

    # Verify queue allows self-service
    queue = db.get_queue(queue_id)
    if not queue or not queue.get('allow_self_service'):
        flash('This queue does not allow self-service membership.', 'error')
        return redirect(url_for('web.index'))

    is_member = db.toggle_queue_membership(queue_id, user.email, f'session:{user.email}')

    if is_member:
        flash(f'You have joined the {queue["name"]} queue.', 'success')
    else:
        flash(f'You have left the {queue["name"]} queue.', 'success')

    return redirect(url_for('web.index'))


@web_bp.route('/admin')
@admin_required
def admin():
    """Admin dashboard - tiles linking to admin sub-pages."""
    from rinq.integrations import get_permission_service, get_staff_directory

    service = get_twilio_service()
    user = get_current_user()

    if not service.is_configured:
        return render_template('setup_required.html', current_user=user)

    db = get_db()

    # Get counts for tiles
    phone_numbers = service.get_phone_numbers()
    account_info = service.get_account_info()

    # Get current admins for the admin management section
    perms = get_permission_service()
    admins = [p for p in (perms.get_permissions('tina') if perms else [])
              if p.get('role') == 'admin']
    admins.sort(key=lambda p: p.get('email', ''))

    # Get staff list for the add-admin dropdown
    admin_emails = {p.get('email', '').lower() for p in admins}
    staff_dir = get_staff_directory()
    try:
        all_staff = staff_dir.get_active_staff() if staff_dir else []
        available_staff = []
        for s in all_staff:
            staff_email = (s.get('work_email') or s.get('google_primary_email') or s.get('email') or '').lower()
            if staff_email and staff_email not in admin_emails:
                s['_email'] = staff_email
                available_staff.append(s)
        available_staff.sort(key=lambda s: s.get('name', ''))
    except Exception as e:
        logger.warning(f"Failed to get staff list for admin dropdown: {e}")
        available_staff = []

    return render_template('admin.html',
                         phone_numbers_count=len(phone_numbers),
                         verified_caller_ids_count=len(db.get_verified_caller_ids(active_only=False)),
                         queues=db.get_queues(),
                         call_flows=db.get_call_flows(),
                         schedules=db.get_schedules(),
                         audio_files=db.get_audio_files(),
                         holiday_templates=db.get_holiday_templates(),
                         account_info=account_info,
                         admins=admins,
                         available_staff=available_staff,
                         current_user=user)


@web_bp.route('/admin/test-runsheet')
@admin_required
def admin_test_runsheet():
    """Interactive test checklist for verifying phone system functionality."""
    user = get_current_user()
    return render_template('admin_test_runsheet.html', current_user=user)


@web_bp.route('/admin/staff')
@admin_required
def admin_staff():
    """Manage staff extensions and Rinq activation status."""
    user = get_current_user()
    db = get_db()

    # Sync from staff directory - create extensions for any active staff who don't have one
    try:
        from rinq.integrations import get_staff_directory
        staff_dir = get_staff_directory()
        peter_staff = staff_dir.get_active_staff() if staff_dir else []
        if peter_staff:
            from rinq.services.phone import normalize_au_mobile

            created = 0
            updated = 0
            forwarding_set = 0
            for staff in peter_staff:
                email = (staff.get('google_primary_email') or staff.get('work_email') or '').lower().strip()
                if not email:
                    continue
                peter_ext = staff.get('extension', '').strip()
                peter_mobile = normalize_au_mobile(staff.get('phone_mobile', ''))
                existing = db.get_staff_extension(email)
                if not existing:
                    db.create_staff_extension(email, 'system:sync', extension=peter_ext or None)
                    existing = db.get_staff_extension(email)
                    created += 1
                elif peter_ext and existing.get('extension') != peter_ext and existing.get('created_by') == 'system:sync':
                    result = db.set_extension_number(email, peter_ext, 'system:sync')
                    if result.get('success'):
                        updated += 1

                # Set mobile forwarding for sync-created staff who don't have it yet
                if existing and existing.get('created_by') == 'system:sync' and peter_mobile and not existing.get('forward_to'):
                    db.update_staff_extension(email, {
                        'forward_to': peter_mobile,
                        'forward_mode': 'always',
                    }, 'system:sync')
                    forwarding_set += 1

            msgs = []
            if created:
                msgs.append(f'{created} created')
            if updated:
                msgs.append(f'{updated} extensions updated')
            if forwarding_set:
                msgs.append(f'{forwarding_set} mobile forwarding set')
            if msgs:
                flash(f'Staff sync: {", ".join(msgs)}.', 'success')
    except Exception as e:
        logger.warning(f"Could not sync staff from Peter: {e}")

    # Auto-activate staff with usage signals (won't touch locked ones)
    auto_activated = db.auto_activate_staff()
    if auto_activated:
        names = [e.split('@')[0].replace('.', ' ').title() for e in auto_activated]
        flash(f'Auto-activated {len(auto_activated)} staff: {", ".join(names)}', 'info')

    # Auto-regenerate name audio for extensions with missing or stale audio
    try:
        from rinq.services.tts_service import generate_staff_name_audio
        all_exts = db.get_all_staff_extensions()
        audio_generated = 0
        audio_failed = 0
        for ext in all_exts:
            ext_email = ext.get('email', '')
            ext_user = db.get_user_by_email(ext_email)
            friendly_name = (ext_user.get('friendly_name') if ext_user else None) or ''
            if not friendly_name:
                continue
            # Check if audio is missing or name has changed
            if ext.get('name_audio_text') != friendly_name:
                result = generate_staff_name_audio(
                    ext_email, friendly_name, ext['extension'], 'system:sync'
                )
                if result.get('success'):
                    audio_generated += 1
                else:
                    audio_failed += 1
        if audio_generated:
            flash(f'Name audio: {audio_generated} generated.', 'info')
        if audio_failed:
            flash(f'Name audio: {audio_failed} failed (check TTS settings).', 'warning')
    except Exception as e:
        logger.warning(f"Name audio generation failed: {e}")

    extensions = db.get_all_staff_extensions()
    signals = db.get_staff_usage_signals()

    return render_template('admin_staff.html',
                         extensions=extensions,
                         signals=signals,
                         current_user=user)


@web_bp.route('/admin/staff/<email>/regenerate-audio', methods=['POST'])
@admin_required
def regenerate_staff_audio(email):
    """Regenerate name audio for a single staff member."""
    from rinq.services.tts_service import generate_staff_name_audio

    user = get_current_user()
    db = get_db()
    ext = db.get_staff_extension(email)
    if not ext:
        flash('Staff extension not found.', 'error')
        return redirect(url_for('web.admin_staff'))

    user_record = db.get_user_by_email(email)
    friendly_name = (user_record.get('friendly_name') if user_record else None) or ''
    if not friendly_name:
        flash('No friendly name set for this staff member.', 'error')
        return redirect(url_for('web.admin_staff'))

    result = generate_staff_name_audio(email, friendly_name, ext['extension'], f'session:{user.email}')
    if result.get('success'):
        flash(f'Name audio generated for {friendly_name}.', 'success')
    else:
        flash(f'Failed to generate audio: {result.get("error")}', 'error')

    return redirect(url_for('web.admin_staff'))


@web_bp.route('/admin/staff/regenerate-all-audio', methods=['POST'])
@admin_required
def regenerate_all_staff_audio():
    """Regenerate name audio for all staff extensions."""
    from rinq.services.tts_service import generate_staff_name_audio

    user = get_current_user()
    db = get_db()
    extensions = db.get_all_staff_extensions()

    generated = 0
    failed = 0
    skipped = 0
    for ext in extensions:
        ext_email = ext.get('email', '')
        user_record = db.get_user_by_email(ext_email)
        friendly_name = (user_record.get('friendly_name') if user_record else None) or ''
        if not friendly_name:
            skipped += 1
            continue
        result = generate_staff_name_audio(ext_email, friendly_name, ext['extension'], f'session:{user.email}')
        if result.get('success'):
            generated += 1
        else:
            failed += 1

    parts = []
    if generated:
        parts.append(f'{generated} generated')
    if failed:
        parts.append(f'{failed} failed')
    if skipped:
        parts.append(f'{skipped} skipped (no name)')
    flash(f'Name audio: {", ".join(parts)}.', 'success' if not failed else 'warning')

    return redirect(url_for('web.admin_staff'))


@web_bp.route('/admin/staff/<email>/activate', methods=['POST'])
@admin_required
def toggle_staff_active(email):
    """Toggle a staff member's active status in Rinq.

    Manual toggle always sets is_active_locked=True so auto-activation
    won't override the admin's decision.
    """
    user = get_current_user()
    db = get_db()
    ext = db.get_staff_extension(email)
    if ext:
        new_status = not bool(ext.get('is_active'))
        # Manual toggle always locks the status
        db.set_staff_extension_active_locked(
            email, new_status, locked=True, updated_by=f'session:{user.email}'
        )
        status_text = 'activated' if new_status else 'deactivated'
        flash(f'{email.split("@")[0]} manually {status_text} on {config.product_name} (locked).', 'success')
    else:
        flash('Staff extension not found.', 'error')
    return redirect(url_for('web.admin_staff'))


@web_bp.route('/admin/staff/<email>/unlock', methods=['POST'])
@admin_required
def unlock_staff_active(email):
    """Remove manual lock so auto-activation can manage this staff member."""
    user = get_current_user()
    db = get_db()
    ext = db.get_staff_extension(email)
    if ext:
        now = __import__('datetime').datetime.now(timezone.utc).isoformat()
        with db._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET is_active_locked = 0, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (now, f'session:{user.email}', email.lower()))
            conn.commit()
        flash(f'{email.split("@")[0]} unlocked - auto-activation will manage status.', 'success')
    else:
        flash('Staff extension not found.', 'error')
    return redirect(url_for('web.admin_staff'))


@web_bp.route('/admin/staff/<email>/extension', methods=['POST'])
@admin_required
def update_staff_extension(email):
    """Update a staff member's extension number."""
    user = get_current_user()
    db = get_db()
    new_ext = request.form.get('extension', '').strip()

    if not new_ext or not new_ext.isdigit():
        flash('Extension must be a number.', 'error')
        return redirect(url_for('web.admin_staff'))

    result = db.update_staff_extension_number(email, new_ext, f'session:{user.email}')
    if result.get('success'):
        flash(f'{email.split("@")[0]} extension updated to {new_ext}.', 'success')
    else:
        flash(result.get('error', 'Failed to update extension.'), 'error')
    return redirect(url_for('web.admin_staff'))


@web_bp.route('/admin/staff/<email>/reports-to', methods=['POST'])
@admin_required
def update_staff_reports_to(email):
    """Set who a staff member reports to."""
    user = get_current_user()
    db = get_db()
    reports_to = request.form.get('reports_to', '').strip() or None
    db.update_staff_reports_to(email, reports_to, f'session:{user.email}')
    name = email.split('@')[0].replace('.', ' ').title()
    if reports_to:
        mgr_name = reports_to.split('@')[0].replace('.', ' ').title()
        flash(f'{name} now reports to {mgr_name}.', 'success')
    else:
        flash(f'{name} reports-to cleared.', 'success')
    return redirect(url_for('web.admin_staff'))


@web_bp.route('/admin/address')
@admin_required
def setup_address():
    """Set up business address for phone number purchases."""
    from flask import g
    address = None
    tenant = getattr(g, 'tenant', None)
    if tenant and tenant.get('twilio_address_sid'):
        try:
            service = get_twilio_service()
            addr = service.client.addresses(tenant['twilio_address_sid']).fetch()
            address = {
                'customer_name': addr.customer_name,
                'street': addr.street,
                'city': addr.city,
                'region': addr.region,
                'postal_code': addr.postal_code,
                'iso_country': addr.iso_country,
            }
        except Exception:
            pass
    return render_template('setup_address.html', address=address, current_user=get_current_user())


@web_bp.route('/admin/address', methods=['POST'])
@admin_required
def save_address():
    """Save business address to Twilio."""
    from flask import g
    from rinq.database.master import get_master_db

    tenant = getattr(g, 'tenant', None)
    if not tenant:
        flash('No tenant context', 'error')
        return redirect(url_for('web.setup_address'))

    service = get_twilio_service()
    try:
        # Create or update address in Twilio
        kwargs = {
            'friendly_name': request.form.get('customer_name', ''),
            'customer_name': request.form.get('customer_name', ''),
            'street': request.form.get('street', ''),
            'city': request.form.get('city', ''),
            'region': request.form.get('region', ''),
            'postal_code': request.form.get('postal_code', ''),
            'iso_country': request.form.get('iso_country', 'AU'),
        }

        if tenant.get('twilio_address_sid'):
            # Update existing
            service.client.addresses(tenant['twilio_address_sid']).update(**kwargs)
            flash('Address updated.', 'success')
        else:
            # Create new
            addr = service.client.addresses.create(**kwargs)
            master_db = get_master_db()
            master_db.update_tenant(tenant['id'], twilio_address_sid=addr.sid)
            flash('Address saved.', 'success')

    except Exception as e:
        flash(f'Failed to save address: {e}', 'error')

    return redirect(url_for('web.setup_address'))


@web_bp.route('/admin/get-number')
@admin_required
def get_number():
    """Search and purchase phone numbers."""
    from flask import g
    tenant = getattr(g, 'tenant', None)
    if not tenant or not tenant.get('twilio_address_sid'):
        flash('Please set up your business address before purchasing a number.', 'warning')
        return redirect(url_for('web.setup_address'))
    return render_template('get_number.html', current_user=get_current_user())


@web_bp.route('/admin/address-book')
@admin_required
def admin_address_book():
    """View and manage the tenant address book."""
    db = get_db()
    entries = db.get_address_book()
    return render_template('admin_address_book.html',
                           current_user=get_current_user(),
                           entries=entries)


@web_bp.route('/admin/phone-numbers')
@admin_required
def admin_phone_numbers():
    """Manage phone numbers and call flow assignments."""
    service = get_twilio_service()
    user = get_current_user()

    if not service.is_configured:
        return render_template('setup_required.html', current_user=user)

    phone_numbers = service.get_phone_numbers()
    db = get_db()

    # Get all queues and call flows
    queues = db.get_queues()
    call_flows = db.get_call_flows()

    # Load members for each queue
    for queue in queues:
        queue['members'] = db.get_queue_members(queue['id'])

    # Build lookup dicts
    queue_by_id = {q['id']: q for q in queues}
    call_flow_by_id = {cf['id']: cf for cf in call_flows}

    # Enrich each phone number with its routing info
    for number in phone_numbers:
        number['assignments'] = db.get_assignments_for_number(number['sid'])

        # Get call flow if assigned
        if number.get('call_flow_id'):
            number['call_flow'] = call_flow_by_id.get(number['call_flow_id'])
            if number['call_flow'] and number['call_flow'].get('open_queue_id'):
                number['queue'] = queue_by_id.get(number['call_flow']['open_queue_id'])
                if number['queue']:
                    number['queue']['members'] = db.get_queue_members(number['queue']['id'])

    # Fetch sections from staff directory for caller ID assignment
    sections = []
    peter_available = False
    from rinq.integrations import get_staff_directory
    staff_dir = get_staff_directory()
    if staff_dir:
        sections = staff_dir.get_sections()
        peter_available = bool(sections)

    # Get verified caller IDs for coverage check
    verified_caller_ids = db.get_verified_caller_ids(active_only=True)

    # Build caller ID coverage report
    covered_sections = {n.get('section') for n in phone_numbers if n.get('section')}
    covered_sections.update({v.get('section') for v in verified_caller_ids if v.get('section')})

    # Get all Rinq users and check coverage
    tina_users = db.get_all_staff_extensions()
    uncovered_users = []

    if peter_available and tina_users and staff_dir:
        try:
            for tina_user in tina_users:
                staff_data = staff_dir.get_staff_by_email(tina_user['email'])
                if staff_data:
                    user_section = staff_data.get('section')
                    if user_section and user_section not in covered_sections:
                        uncovered_users.append({
                            'email': tina_user['email'],
                            'extension': tina_user['extension'],
                            'section': user_section
                        })
        except Exception as e:
            logger.warning(f"Could not fetch user sections: {e}")

    # Get all active staff for assignment dropdown
    all_users = []
    if peter_available and staff_dir:
        try:
            for s in staff_dir.get_active_staff():
                email = (s.get('google_primary_email') or s.get('work_email') or s.get('email', '')).lower().strip()
                if email:
                    all_users.append({
                        'staff_email': email,
                        'friendly_name': s.get('name', email.split('@')[0]),
                    })
            all_users.sort(key=lambda u: u['friendly_name'])
        except Exception as e:
            logger.warning(f"Could not fetch staff for dropdown: {e}")
    if not all_users:
        # Use local staff extensions
        for ext in db.get_all_staff_extensions():
            if ext.get('is_active'):
                email = ext.get('email', '')
                user_rec = db.get_user_by_email(email) if email else None
                name = (user_rec.get('friendly_name') if user_rec else None) or email.split('@')[0].replace('.', ' ').title()
                all_users.append({'staff_email': email, 'friendly_name': name})
        all_users.sort(key=lambda u: u['friendly_name'])

    return render_template('admin_phone_numbers.html',
                         phone_numbers=phone_numbers,
                         call_flows=call_flows,
                         sections=sections,
                         peter_available=peter_available,
                         covered_sections=covered_sections,
                         uncovered_users=uncovered_users,
                         all_users=all_users,
                         current_user=user)


@web_bp.route('/admin/caller-id-overview')
@admin_required
def admin_caller_id_overview():
    """Overview of which outbound caller ID each user would use."""
    user = get_current_user()
    db = get_db()

    # Bulk-fetch all data to avoid N+1 queries
    extensions = db.get_all_staff_extensions()
    active_extensions = [e for e in extensions if e.get('is_active')]
    users = db.get_users()
    all_assignments = db.get_assignments()
    phone_numbers = db.get_phone_numbers()
    verified_cids = db.get_verified_caller_ids(active_only=True)

    # Build lookups
    user_by_email = {u['staff_email']: u for u in users if u.get('staff_email')}
    phone_by_sid = {n['sid']: n for n in phone_numbers}
    phone_by_number = {n['phone_number']: n for n in phone_numbers}
    vcid_by_number = {v['phone_number']: v for v in verified_cids}

    # Group assignments by email
    assignments_by_email = {}
    for a in all_assignments:
        assignments_by_email.setdefault(a['staff_email'], []).append(a)

    # Bulk fetch sections from staff directory
    section_by_email = {}
    from rinq.integrations import get_staff_directory
    staff_dir = get_staff_directory()
    if staff_dir:
        try:
            for s in staff_dir.get_active_staff():
                if s.get('email') and s.get('section'):
                    section_by_email[s['email']] = s['section']
        except Exception as e:
            logger.warning(f"Could not fetch staff sections: {e}")

    # Phone numbers by section (for section-based matching)
    number_by_section = {}
    for n in phone_numbers:
        if n.get('section') and n['section'] not in number_by_section:
            number_by_section[n['section']] = n

    def _number_display(phone_number):
        """Get friendly display for a phone number."""
        if not phone_number:
            return None
        pn = phone_by_number.get(phone_number)
        if pn:
            name = pn.get('friendly_name') or phone_number
            if pn.get('section'):
                return f"{name} ({pn['section']})"
            return name
        vcid = vcid_by_number.get(phone_number)
        if vcid:
            name = vcid.get('friendly_name') or phone_number
            if vcid.get('section'):
                return f"{name} ({vcid['section']})"
            return name
        return phone_number

    # Resolve caller ID for each active user
    staff = []
    for ext in sorted(active_extensions, key=lambda e: e.get('email', '')):
        email = ext['email']
        cred = user_by_email.get(email)
        user_assignments = assignments_by_email.get(email, [])

        caller_id = None
        source = 'none'

        # Priority 1: Manual default (from staff_extensions)
        if ext.get('default_caller_id'):
            caller_id = ext['default_caller_id']
            source = 'manual'

        # Priority 2: Direct assignment (can_make)
        if not caller_id:
            for a in user_assignments:
                if a.get('can_make'):
                    pn = phone_by_sid.get(a['phone_number_sid'])
                    if pn:
                        caller_id = pn['phone_number']
                        source = 'assigned'
                        break

        # Priority 3: Section-based
        if not caller_id:
            user_section = section_by_email.get(email)
            if user_section and user_section in number_by_section:
                caller_id = number_by_section[user_section]['phone_number']
                source = 'section'

        # Priority 4: System default / first available number
        if not caller_id:
            tenant_default = get_twilio_config('twilio_default_caller_id')
            if tenant_default:
                caller_id = tenant_default
            elif phone_numbers:
                caller_id = phone_numbers[0]['phone_number']
            if caller_id:
                source = 'default'

        source_labels = {
            'manual': 'Manual',
            'assigned': 'Assigned',
            'section': 'Section',
            'default': 'System Default',
            'none': 'None',
        }

        # Build assigned numbers display list
        assigned_display = []
        for a in user_assignments:
            pn = phone_by_sid.get(a['phone_number_sid'])
            if pn:
                name = pn.get('friendly_name') or pn['phone_number']
                assigned_display.append(name)

        # Get name from SIP credential (friendly_name) or Peter section data
        name = None
        if cred:
            name = cred.get('friendly_name')
        if not name:
            name = email.split('@')[0].replace('.', ' ').title()

        staff.append({
            'name': name,
            'email': email,
            'manual_caller_id': ext.get('default_caller_id'),
            'caller_id': caller_id,
            'caller_id_display': _number_display(caller_id) if caller_id else None,
            'source': source,
            'source_label': source_labels[source],
            'assigned_numbers': assigned_display,
        })

    # Build caller ID options for dropdowns (owned numbers + verified caller IDs)
    caller_id_options = []
    for n in sorted(phone_numbers, key=lambda x: x.get('friendly_name') or x['phone_number']):
        label = n.get('friendly_name') or n['phone_number']
        if n.get('section'):
            label += f" ({n['section']})"
        caller_id_options.append({'value': n['phone_number'], 'label': label})
    for v in sorted(verified_cids, key=lambda x: x.get('friendly_name') or x['phone_number']):
        label = v.get('friendly_name') or v['phone_number']
        if v.get('section'):
            label += f" ({v['section']})"
        caller_id_options.append({'value': v['phone_number'], 'label': f"{label} [verified]"})

    return render_template('admin_caller_id_overview.html',
                         staff=staff,
                         caller_id_options=caller_id_options,
                         current_user=user)


@web_bp.route('/admin/caller-id-overview/save', methods=['POST'])
@admin_required
def admin_caller_id_save():
    """Bulk update manual caller IDs for users."""
    user = get_current_user()
    db = get_db()

    updated = 0
    for key, value in request.form.items():
        if not key.startswith('caller_id_'):
            continue
        email = key[len('caller_id_'):]
        new_caller_id = value.strip() or None

        # Check what the current value is to avoid unnecessary updates
        ext = db.get_staff_extension(email)
        if not ext:
            continue
        current = ext.get('default_caller_id')
        if new_caller_id == current:
            continue

        db.update_staff_extension_caller_id(
            email=email,
            caller_id=new_caller_id,
            updated_by=_audit_tag(user)
        )
        db.log_activity(
            action='caller_id_updated',
            target=email,
            details=f"Caller ID set to {new_caller_id or 'auto'}" +
                    (f" (was {current})" if current else ""),
            performed_by=_audit_tag(user)
        )
        updated += 1

    if updated:
        flash(f'Updated caller ID for {updated} user{"s" if updated != 1 else ""}.', 'success')
    else:
        flash('No changes to save.', 'info')

    return redirect(url_for('web.admin_caller_id_overview'))


@web_bp.route('/admin/verified-caller-ids')
@admin_required
def admin_verified_caller_ids():
    """Manage verified caller IDs."""
    user = get_current_user()
    db = get_db()

    verified_caller_ids = db.get_verified_caller_ids(active_only=False)

    # Fetch sections from staff directory
    from rinq.integrations import get_staff_directory
    staff_dir = get_staff_directory()
    sections = staff_dir.get_sections() if staff_dir else []

    return render_template('admin_verified_caller_ids.html',
                         verified_caller_ids=verified_caller_ids,
                         sections=sections,
                         current_user=user)


# =============================================================================
# Verified Caller ID Management
# =============================================================================

@web_bp.route('/admin/verified-caller-ids/add', methods=['POST'])
@admin_required
def add_verified_caller_id():
    """Add a new verified caller ID."""
    phone_number = request.form.get('phone_number', '').strip()
    friendly_name = request.form.get('friendly_name', '').strip()
    section = request.form.get('section', '').strip()
    notes = request.form.get('notes', '').strip()

    if not phone_number:
        flash("Phone number is required", "error")
        return redirect(url_for('web.admin_verified_caller_ids'))

    from rinq.services.phone import ensure_plus
    phone_number = ensure_plus(phone_number)

    user = get_current_user()
    db = get_db()

    try:
        db.add_verified_caller_id(
            phone_number=phone_number,
            friendly_name=friendly_name or None,
            section=section or None,
            notes=notes or None,
            created_by=_audit_tag(user)
        )
        db.log_activity(
            action="add_verified_caller_id",
            target=phone_number,
            details=f"Added verified caller ID: {friendly_name or phone_number}",
            performed_by=_audit_tag(user)
        )
        flash(f"Added verified caller ID: {friendly_name or phone_number}", "success")
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            flash(f"Verified caller ID {phone_number} already exists", "error")
        else:
            flash(f"Failed to add verified caller ID: {e}", "error")

    return redirect(url_for('web.admin_verified_caller_ids'))


@web_bp.route('/admin/verified-caller-ids/sync', methods=['POST'])
@admin_required
def sync_verified_caller_ids():
    """Sync verified caller IDs from Twilio."""
    user = get_current_user()
    twilio = get_twilio_service()

    result = twilio.sync_verified_caller_ids(performed_by=_audit_tag(user))

    if result.get("success"):
        flash(
            f"Synced {result['count']} verified caller IDs from Twilio "
            f"({result['added']} added, {result['updated']} updated, {result['deactivated']} deactivated)",
            "success"
        )
    else:
        flash(f"Failed to sync verified caller IDs: {result.get('error')}", "error")

    return redirect(url_for('web.admin_verified_caller_ids'))


@web_bp.route('/admin/verified-caller-ids/<path:phone_number>/update', methods=['POST'])
@admin_required
def update_verified_caller_id(phone_number):
    """Update a verified caller ID."""
    friendly_name = request.form.get('friendly_name', '').strip()
    section = request.form.get('section', '').strip()
    is_active = request.form.get('is_active') == '1'
    notes = request.form.get('notes', '').strip()

    user = get_current_user()
    db = get_db()

    try:
        db.update_verified_caller_id(
            phone_number=phone_number,
            friendly_name=friendly_name,
            section=section,
            is_active=is_active,
            notes=notes,
            updated_by=_audit_tag(user)
        )
        db.log_activity(
            action="update_verified_caller_id",
            target=phone_number,
            details=f"Updated verified caller ID: {'active' if is_active else 'inactive'}",
            performed_by=_audit_tag(user)
        )
        flash(f"Updated verified caller ID: {phone_number}", "success")
    except Exception as e:
        flash(f"Failed to update verified caller ID: {e}", "error")

    return redirect(url_for('web.admin_verified_caller_ids'))


@web_bp.route('/admin/verified-caller-ids/<path:phone_number>/delete', methods=['POST'])
@admin_required
def delete_verified_caller_id(phone_number):
    """Delete a verified caller ID."""
    user = get_current_user()
    db = get_db()

    try:
        db.delete_verified_caller_id(phone_number)
        db.log_activity(
            action="delete_verified_caller_id",
            target=phone_number,
            details="Deleted verified caller ID",
            performed_by=_audit_tag(user)
        )
        flash(f"Deleted verified caller ID: {phone_number}", "success")
    except Exception as e:
        flash(f"Failed to delete verified caller ID: {e}", "error")

    return redirect(url_for('web.admin_verified_caller_ids'))


@web_bp.route('/admin/templates')
@admin_required
def admin_templates():
    """Holiday templates management page."""
    user = get_current_user()
    db = get_db()

    holiday_templates = db.get_holiday_templates()
    schedules = db.get_schedules()

    return render_template('admin_templates.html',
                         holiday_templates=holiday_templates,
                         schedules=schedules,
                         current_user=user)


@web_bp.route('/admin/schedules')
@admin_required
def admin_schedules():
    """Schedules management page."""
    user = get_current_user()
    db = get_db()

    schedules = db.get_schedules()
    audio_files = db.get_audio_files()

    return render_template('admin_schedules.html',
                         schedules=schedules,
                         audio_files=audio_files,
                         current_user=user)


@web_bp.route('/admin/queues')
@admin_required
def admin_queues():
    """Queues management page."""
    user = get_current_user()
    db = get_db()

    queues = db.get_queues()
    for queue in queues:
        queue['members'] = db.get_queue_members(queue['id'])

    # Get all staff who have logged into Rinq (have extensions)
    all_staff = db.get_all_staff_extensions()

    # Get audio files for hold music selection
    audio_files = db.get_audio_files()

    return render_template('admin_queues.html',
                         queues=queues,
                         all_staff=all_staff,
                         audio_files=audio_files,
                         current_user=user)


@web_bp.route('/admin/audio')
@admin_required
def admin_audio():
    """Unified audio files management page — lists, uploads, and TTS generation."""
    from rinq.services.tts_service import get_tts_service

    user = get_current_user()
    db = get_db()
    tts = get_tts_service()

    audio_files = db.get_audio_files()

    # --- TTS voice settings ---
    settings = db.get_tts_settings()
    default_provider = settings.get('default_provider', 'elevenlabs')
    default_voice = settings.get('default_voice', 'cjVigY5qzO86Huf0OWal')

    current_voice_name = 'Not Set'
    if default_provider == 'elevenlabs' and tts.elevenlabs_available:
        voices = tts.get_elevenlabs_voices()
        voice_info = voices.get(default_voice, {})
        current_voice_name = voice_info.get('name', default_voice)
        if voice_info.get('accent'):
            current_voice_name += f" ({voice_info['accent']})"
    elif default_provider == 'cartesia' and tts.cartesia_available:
        voices = tts.get_cartesia_voices()
        voice_info = voices.get(default_voice, {})
        current_voice_name = voice_info.get('name', default_voice)
        if voice_info.get('gender'):
            current_voice_name += f" ({voice_info['gender'].capitalize()})"
    elif default_provider == 'google' and tts.google_available:
        voice_info = tts.GOOGLE_VOICES.get(default_voice, {})
        current_voice_name = voice_info.get('name', default_voice)
        if voice_info.get('gender'):
            current_voice_name += f" ({voice_info['gender']})"

    tts_available = tts.elevenlabs_available or tts.cartesia_available or tts.google_available

    import copy
    audio_type_defs = copy.deepcopy(AUDIO_TYPE_DEFS)

    # Enrich with recordings data
    for t in audio_type_defs:
        existing = [a for a in audio_files if a['file_type'] == t['value']]
        t['count'] = len(existing)
        t['has_recording'] = bool(existing)
        # Include full audio record data for all types
        t['recordings'] = [dict(a) for a in existing]
        if t.get('per_flow'):
            t['existing_text'] = t['default_text']
        else:
            if existing and existing[0].get('tts_text'):
                t['existing_text'] = existing[0]['tts_text']
            else:
                t['existing_text'] = t['default_text']

    return render_template('admin_audio.html',
                         audio_type_defs=audio_type_defs,
                         tts_available=tts_available,
                         elevenlabs_available=tts.elevenlabs_available,
                         cartesia_available=tts.cartesia_available,
                         google_available=tts.google_available,
                         elevenlabs_voices=tts.get_elevenlabs_voices_grouped() if tts.elevenlabs_available else {},
                         cartesia_voices=tts.get_cartesia_voices_grouped() if tts.cartesia_available else {},
                         google_voices=tts.get_google_voices_grouped(),
                         default_provider=default_provider,
                         default_voice=default_voice,
                         current_voice_name=current_voice_name,
                         current_user=user)


@web_bp.route('/admin/call-flows')
@admin_required
def admin_call_flows():
    """Call flows management page."""
    user = get_current_user()
    db = get_db()

    call_flows = db.get_call_flows()
    queues = db.get_queues()
    schedules = db.get_schedules()
    audio_files = db.get_audio_files()
    voicemail_destinations = db.get_voicemail_destinations()

    # Fetch ticket groups for voicemail destination dropdown
    from rinq.integrations import get_ticket_service
    tickets = get_ticket_service()
    zendesk_groups = tickets.get_groups() if tickets else []

    return render_template('admin_call_flows.html',
                         call_flows=call_flows,
                         queues=queues,
                         schedules=schedules,
                         audio_files=audio_files,
                         voicemail_destinations=voicemail_destinations,
                         zendesk_groups=zendesk_groups,
                         current_user=user)


@web_bp.route('/admin/visualizer')
@admin_required
def visualizer():
    """Visual flowchart of how calls are routed."""
    user = get_current_user()
    db = get_db()
    service = get_twilio_service()

    # Get all the data we need
    phone_numbers = service.get_phone_numbers()
    call_flows = db.get_call_flows()
    schedules = db.get_schedules()
    queues = db.get_queues()
    audio_files = db.get_audio_files()

    # Load members for each queue
    for queue in queues:
        queue['members'] = db.get_queue_members(queue['id'])

    # Parse business hours for each schedule
    for schedule in schedules:
        bh = schedule.get('business_hours')
        if bh:
            try:
                schedule['business_hours_parsed'] = json.loads(bh) if isinstance(bh, str) else bh
            except (json.JSONDecodeError, TypeError):
                schedule['business_hours_parsed'] = {}
        else:
            schedule['business_hours_parsed'] = {}

    # Get voicemail destinations for display
    voicemail_destinations = db.get_voicemail_destinations()

    # Get selected phone number (if any)
    selected_sid = request.args.get('sid')
    selected_number = None
    selected_flow = None
    selected_schedule = None
    selected_queue = None
    selected_voicemail_dest = None

    if selected_sid:
        for number in phone_numbers:
            if number['sid'] == selected_sid:
                selected_number = number
                break

        if selected_number and selected_number.get('call_flow_id'):
            for flow in call_flows:
                if flow['id'] == selected_number['call_flow_id']:
                    selected_flow = flow
                    break

            if selected_flow:
                # Get the schedule
                if selected_flow.get('schedule_id'):
                    for schedule in schedules:
                        if schedule['id'] == selected_flow['schedule_id']:
                            selected_schedule = schedule
                            break

                # Get the queue
                if selected_flow.get('open_queue_id'):
                    for queue in queues:
                        if queue['id'] == selected_flow['open_queue_id']:
                            selected_queue = queue
                            break

                # Get the voicemail destination
                if selected_flow.get('voicemail_destination_id'):
                    for dest in voicemail_destinations:
                        if dest['id'] == selected_flow['voicemail_destination_id']:
                            selected_voicemail_dest = dest
                            break

    return render_template('visualizer.html',
                         phone_numbers=phone_numbers,
                         call_flows=call_flows,
                         schedules=schedules,
                         queues=queues,
                         audio_files=audio_files,
                         voicemail_destinations=voicemail_destinations,
                         selected_sid=selected_sid,
                         selected_number=selected_number,
                         selected_flow=selected_flow,
                         selected_schedule=selected_schedule,
                         selected_queue=selected_queue,
                         selected_voicemail_dest=selected_voicemail_dest,
                         current_user=user)


@web_bp.route('/sync', methods=['POST'])
@admin_required
def sync():
    """Sync phone numbers from Twilio (admin only)."""
    service = get_twilio_service()
    user = get_current_user()
    result = service.sync_phone_numbers(performed_by=_audit_tag(user))

    if result.get("success"):
        flash(f"Synced {result['count']} phone numbers from Twilio", "success")
    else:
        flash(f"Sync failed: {result.get('error')}", "error")

    return redirect(url_for('web.admin_phone_numbers'))


@web_bp.route('/forward/<sid>', methods=['POST'])
@admin_required
def update_forward(sid):
    """Update forwarding for a phone number (admin only)."""
    forward_to = request.form.get('forward_to')

    if not forward_to:
        flash("Forward to number is required", "error")
        return redirect(url_for('web.admin_phone_numbers'))

    service = get_twilio_service()
    user = get_current_user()
    result = service.update_forwarding(sid, forward_to, performed_by=_audit_tag(user))

    if result.get("success"):
        flash(f"Updated forwarding to {result['forward_to']}", "success")
    else:
        flash(f"Failed: {result.get('error')}", "error")

    return redirect(url_for('web.admin_phone_numbers'))


@web_bp.route('/admin/assign/<sid>', methods=['POST'])
@admin_required
def add_assignment(sid):
    """Assign a user to a phone number (admin only)."""
    staff_email = request.form.get('staff_email', '').strip().lower()

    if not staff_email:
        flash("Staff email is required", "error")
        return redirect(url_for('web.admin_phone_numbers'))

    user = get_current_user()
    db = get_db()

    try:
        db.add_assignment(sid, staff_email, True, True, _audit_tag(user))
        db.log_activity(
            action="assign_user",
            target=staff_email,
            details=f"Assigned to phone number {sid}",
            performed_by=_audit_tag(user)
        )
        flash(f"Assigned {staff_email} to phone number", "success")
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            flash(f"{staff_email} is already assigned to this number", "error")
        else:
            flash(f"Failed to assign: {e}", "error")

    return redirect(url_for('web.admin_phone_numbers'))


@web_bp.route('/admin/unassign/<int:assignment_id>', methods=['POST'])
@admin_required
def remove_assignment(assignment_id):
    """Remove a user assignment (admin only)."""
    user = get_current_user()
    db = get_db()

    db.remove_assignment(assignment_id)
    db.log_activity(
        action="unassign_user",
        target=str(assignment_id),
        details="Removed phone number assignment",
        performed_by=_audit_tag(user)
    )
    flash("Assignment removed", "success")

    return redirect(url_for('web.admin_phone_numbers'))


@web_bp.route('/activity')
@login_required
def activity():
    """View activity log."""
    db = get_db()
    activities = db.get_activity_log(limit=100)
    return render_template('activity.html',
                         activities=activities,
                         current_user=get_current_user())


@web_bp.route('/reports')
@login_required
def reports():
    """View call statistics and reporting dashboard.

    Admins see all staff, users with reportees see their team,
    everyone else sees just their own stats.
    """
    from rinq.services.reporting_service import get_reporting_service
    from rinq.integrations import get_staff_directory

    period = request.args.get('period', 'today')
    user = get_current_user()

    # Build team_emails based on admin status / reportees
    team_emails = None
    team_label = None
    staff_dir = get_staff_directory()

    if user.is_admin:
        # Admins see all staff
        if staff_dir:
            try:
                staff_list = staff_dir.get_active_staff()
                team_emails = [s.get('email') for s in staff_list if s.get('email')]
            except Exception as e:
                logger.warning(f"Failed to get staff list: {e}")
        team_label = 'All Staff'

    else:
        # Check if user has reportees via reports_to hierarchy
        reportees = []
        if staff_dir:
            try:
                reportees = staff_dir.get_reportees(user.email, recursive=True)
            except Exception as e:
                logger.warning(f"Failed to get reportees for {user.email}: {e}", exc_info=True)

        if reportees:
            # User has people reporting to them — show team view
            team_emails = [r.get('email') for r in reportees if r.get('email')]
            if user.email not in team_emails:
                team_emails.append(user.email)
            team_label = 'My Team'
        else:
            # Regular users see just their own stats
            team_emails = [user.email]
            team_label = 'My Calls'

    service = get_reporting_service()
    report_data = service.get_report_data(period, team_emails=team_emails)

    return render_template('reports.html',
                         report=report_data,
                         team_label=team_label,
                         format_duration=service.format_duration,
                         format_wait_time=service.format_wait_time,
                         current_user=get_current_user())


@web_bp.route('/leaderboard')
@login_required
def leaderboard():
    """Call leaderboard — gamified agent performance ranking."""
    from rinq.services.reporting_service import get_reporting_service

    period = request.args.get('period', 'today')
    user = get_current_user()

    service = get_reporting_service()
    report_data = service.get_report_data(period)

    agents = report_data.get('agent_stats', [])

    by_calls = sorted(agents, key=lambda a: a['answered_calls'], reverse=True)
    by_talk_time = sorted(agents, key=lambda a: a['total_duration_seconds'], reverse=True)

    return render_template('leaderboard.html',
                         period=period,
                         by_calls=by_calls,
                         by_talk_time=by_talk_time,
                         format_duration=service.format_duration,
                         current_user=user,
                         active_nav='leaderboard')


@web_bp.route('/admin/admins/add', methods=['POST'])
@admin_required
def admin_add():
    """Add an admin via permission service."""
    from rinq.integrations import get_permission_service

    user = get_current_user()
    email = request.form.get('email', '').strip().lower()

    if not email:
        flash('Email is required.', 'danger')
        return redirect(url_for('web.admin'))

    perms = get_permission_service()
    if not perms:
        flash('No permission service configured.', 'danger')
    elif perms.add_permission(email, 'tina', 'admin', user.email):
        flash(f'Added {email} as admin.', 'success')
    else:
        flash(f'Failed to add {email} — check server logs for details.', 'danger')

    return redirect(url_for('web.admin'))


@web_bp.route('/admin/admins/remove', methods=['POST'])
@admin_required
def admin_remove():
    """Remove an admin via permission service."""
    from rinq.integrations import get_permission_service

    user = get_current_user()
    email = request.form.get('email', '').strip().lower()

    if not email:
        flash('Email is required.', 'danger')
        return redirect(url_for('web.admin'))

    if email == user.email.lower():
        flash("You can't remove yourself.", 'danger')
        return redirect(url_for('web.admin'))

    perms = get_permission_service()
    if perms and perms.remove_permission(email, 'tina', user.email):
        flash(f'Removed {email} as admin.', 'success')
    else:
        flash('Failed to remove admin.', 'danger')

    return redirect(url_for('web.admin'))


@web_bp.route('/recordings')
@login_required
def recordings():
    """View call recordings log with filtering and user settings."""
    db = get_db()
    user = get_current_user()

    # Get filter parameters
    filter_type = request.args.get('type')
    filter_staff = request.args.get('staff')

    # Get user's recording preference
    recording_enabled = db.get_user_recording_default(user.email)

    # Build the list of staff emails this user can see recordings for
    from rinq.integrations import get_staff_directory
    is_admin = user.is_admin

    if is_admin and filter_staff != 'mine':
        # Admins see all recordings
        staff_emails = None
    elif filter_staff == 'mine':
        staff_emails = [user.email]
    else:
        # User sees own + reportees' recordings
        staff_emails = [user.email]
        staff_dir = get_staff_directory()
        if staff_dir:
            try:
                reportees = staff_dir.get_reportees(user.email, recursive=True)
                staff_emails.extend(r.get('email') for r in reportees if r.get('email'))
            except Exception as e:
                logger.warning(f"Failed to get reportees for {user.email}: {e}")

    has_reportees = is_admin or (staff_emails and len(staff_emails) > 1)
    recordings_list = db.get_recording_log(
        limit=100,
        call_type=filter_type,
        exclude_voicemail=True,
        staff_emails=staff_emails,
    )

    return render_template('recordings.html',
                         recordings=recordings_list,
                         recording_enabled=recording_enabled,
                         recordings_group=config.recordings_group_email,
                         filter_type=filter_type,
                         filter_staff=filter_staff,
                         can_filter_staff=has_reportees,
                         current_user=user)


@web_bp.route('/setup')
@login_required
def setup():
    """Setup page - shows Twilio configuration status and SIP domains."""
    service = get_twilio_service()

    sip_domains = []
    credential_lists = []
    account_info = {}
    sip_setup_status = None

    if service.is_configured:
        account_info = service.get_account_info()
        sip_domains = service.get_sip_domains()
        credential_lists = service.get_credential_lists()
        sip_setup_status = service.check_sip_domain_setup()

    return render_template('setup.html',
                         configured=service.is_configured,
                         account_info=account_info,
                         sip_domains=sip_domains,
                         credential_lists=credential_lists,
                         sip_setup_status=sip_setup_status,
                         current_user=get_current_user())


@web_bp.route('/setup/link-credential-list', methods=['POST'])
@admin_required
def link_credential_list():
    """Link the configured credential list to the SIP domain."""
    service = get_twilio_service()

    if not get_twilio_config('twilio_sip_credential_list_sid'):
        flash('No credential list configured in .env', 'error')
        return redirect(url_for('web.setup'))

    domains = service.get_sip_domains()
    if not domains:
        flash('No SIP domain found. Create one in Twilio Console first.', 'error')
        return redirect(url_for('web.setup'))

    domain_sid = domains[0]['sid']
    result = service.associate_credential_list_with_domain(domain_sid, get_twilio_config('twilio_sip_credential_list_sid'))

    if result.get('success'):
        calls_status = result.get('calls', 'unknown')
        reg_status = result.get('registrations', 'unknown')

        if calls_status == 'already_linked' and reg_status == 'already_linked':
            flash('Credential list was already linked for both calls and registrations', 'info')
        else:
            msgs = []
            if reg_status == 'linked':
                msgs.append('registrations')
            if calls_status == 'linked':
                msgs.append('calls')
            if msgs:
                flash(f'Credential list linked for {" and ".join(msgs)}!', 'success')
            else:
                flash('Credential list linking complete', 'success')
    else:
        flash(f'Failed to link: calls={result.get("calls")}, registrations={result.get("registrations")}', 'error')

    return redirect(url_for('web.setup'))


# =============================================================================
# Desk Phone Management
# =============================================================================

@web_bp.route('/desk-phones')
@admin_required
def desk_phones():
    """Desk phone credential management page."""
    service = get_twilio_service()
    db = get_db()

    configured = bool(get_twilio_config('twilio_sip_credential_list_sid'))

    # Get SIP domain for display
    sip_domain = None
    if service.is_configured:
        domains = service.get_sip_domains()
        if domains:
            sip_domain = domains[0]['domain_name']

    # Get users from local database
    users = db.get_users() if configured else []

    return render_template('desk_phones.html',
                         configured=configured,
                         sip_domain=sip_domain,
                         users=users,
                         current_user=get_current_user())


@web_bp.route('/desk-phones/add', methods=['POST'])
@admin_required
def add_desk_phone():
    """Add a new desk phone credential."""
    service = get_twilio_service()
    user = get_current_user()

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    friendly_name = request.form.get('friendly_name', '').strip() or username

    if not username or not password:
        flash('Username and password are required', 'error')
        return redirect(url_for('web.desk_phones'))

    if len(password) < 12:
        flash('Password must be at least 12 characters', 'error')
        return redirect(url_for('web.desk_phones'))

    if not get_twilio_config('twilio_sip_credential_list_sid'):
        flash('Credential list not configured', 'error')
        return redirect(url_for('web.desk_phones'))

    result = service.create_user_credential(
        credential_list_sid=get_twilio_config('twilio_sip_credential_list_sid'),
        username=username,
        password=password,
        friendly_name=friendly_name
    )

    if result.get('success'):
        flash(f'Created desk phone user: {username}', 'success')
        db = get_db()
        db.log_activity(
            action="create_desk_phone",
            target=username,
            details=f"Created SIP credential for {friendly_name}",
            performed_by=_audit_tag(user)
        )
    else:
        flash(f'Failed to create user: {result.get("error")}', 'error')

    return redirect(url_for('web.desk_phones'))


@web_bp.route('/desk-phones/delete/<sid>', methods=['POST'])
@admin_required
def delete_desk_phone(sid):
    """Delete a desk phone credential."""
    service = get_twilio_service()
    db = get_db()
    user = get_current_user()

    # Get user info before deleting
    desk_user = db.get_user(sid)
    if not desk_user:
        flash('User not found', 'error')
        return redirect(url_for('web.desk_phones'))

    if not get_twilio_config('twilio_sip_credential_list_sid'):
        flash('Credential list not configured', 'error')
        return redirect(url_for('web.desk_phones'))

    # Delete from Twilio
    result = service.delete_user_credential(
        credential_list_sid=get_twilio_config('twilio_sip_credential_list_sid'),
        credential_sid=sid
    )

    if result.get('success'):
        # Delete from local DB
        db.delete_user(sid)
        flash(f'Deleted desk phone user: {desk_user["username"]}', 'success')
        db.log_activity(
            action="delete_desk_phone",
            target=desk_user["username"],
            details=f"Deleted SIP credential",
            performed_by=_audit_tag(user)
        )
    else:
        flash(f'Failed to delete user: {result.get("error")}', 'error')

    return redirect(url_for('web.desk_phones'))


@web_bp.route('/desk-phones/sync', methods=['POST'])
@admin_required
def sync_desk_phones():
    """Sync desk phone credentials from Twilio."""
    service = get_twilio_service()
    user = get_current_user()

    result = service.sync_credentials(performed_by=_audit_tag(user))

    if result.get('success'):
        flash(f'Synced {result["count"]} credentials ({result["added"]} added, {result["updated"]} updated)', 'success')
    else:
        flash(f'Sync failed: {result.get("error")}', 'error')

    return redirect(url_for('web.desk_phones'))


@web_bp.route('/desk-phones/<sid>/password', methods=['GET'])
@admin_required
def get_desk_phone_password(sid):
    """Get the stored password for a desk phone credential (admin only)."""
    db = get_db()
    password = db.get_user_password(sid)

    if password:
        return jsonify({"success": True, "password": password})
    else:
        return jsonify({
            "success": False,
            "error": "No password stored. Password was created before this feature, or user needs to regenerate."
        }), 404


@web_bp.route('/desk-phones/<sid>/regenerate', methods=['POST'])
@admin_required
def admin_regenerate_password(sid):
    """Regenerate a desk phone password (admin only)."""
    service = get_twilio_service()
    db = get_db()
    user = get_current_user()

    if not get_twilio_config('twilio_sip_credential_list_sid'):
        flash('Desk phone credentials not configured', 'error')
        return redirect(url_for('web.desk_phones'))

    # Get the credential
    credential = db.get_user(sid)
    if not credential:
        flash('Credential not found', 'error')
        return redirect(url_for('web.desk_phones'))

    # Generate new password
    new_password = _generate_sip_password()

    # Update in Twilio
    result = service.update_user_credential_password(
        credential_list_sid=get_twilio_config('twilio_sip_credential_list_sid'),
        credential_sid=sid,
        new_password=new_password
    )

    if result.get('success'):
        db.log_activity(
            action="admin_regenerate_password",
            target=credential['username'],
            details=f"Admin regenerated SIP password for {credential.get('friendly_name', credential['username'])}",
            performed_by=_audit_tag(user)
        )
        flash(f"Password regenerated for {credential.get('friendly_name', credential['username'])}", 'success')
    else:
        flash(f'Failed to regenerate password: {result.get("error")}', 'error')

    return redirect(url_for('web.desk_phones'))


# =============================================================================
# User Desk Phone (Self-Service)
# =============================================================================

_SIP_WORDS = [
    'alpha', 'beach', 'brave', 'cedar', 'chase', 'cloud', 'coral', 'crane',
    'delta', 'drift', 'eagle', 'ember', 'fable', 'flame', 'frost', 'grace',
    'grove', 'haven', 'ivory', 'jewel', 'knack', 'lemon', 'light', 'lunar',
    'maple', 'melon', 'noble', 'ocean', 'olive', 'pearl', 'piano', 'plaza',
    'pulse', 'quail', 'rapid', 'raven', 'ridge', 'river', 'robin', 'royal',
    'sage', 'solar', 'spark', 'stone', 'storm', 'sugar', 'swift', 'tango',
    'thorn', 'tiger', 'trail', 'trend', 'tulip', 'ultra', 'valor', 'velvet',
    'vivid', 'waltz', 'wheat', 'world', 'zephyr',
]


def _generate_sip_password() -> str:
    """Generate a memorable passphrase for SIP credentials.

    Produces passwords like: Coral-Storm-Maple-47
    Meets Twilio requirements: 12+ chars, uppercase, lowercase, digit.
    """
    import secrets
    words = [secrets.choice(_SIP_WORDS).capitalize() for _ in range(3)]
    number = secrets.randbelow(90) + 10  # 10-99
    return f"{words[0]}-{words[1]}-{words[2]}-{number}"


def _email_to_sip_username(email: str) -> str:
    """Convert email to a valid SIP username.

    SIP usernames should be simple alphanumeric with limited special chars.
    We use the part before @ and replace dots with underscores.
    """
    local_part = email.split('@')[0]
    # Replace dots and other chars with underscores, lowercase
    username = local_part.lower().replace('.', '_').replace('+', '_')
    # Remove any other non-alphanumeric chars except underscore
    username = ''.join(c for c in username if c.isalnum() or c == '_')
    return username


@web_bp.route('/my-desk-phone')
@login_required
def my_desk_phone():
    """Redirect old desk phone page to combined devices page."""
    return redirect(url_for('web.my_devices'))


@web_bp.route('/my-desk-phone/caller-id', methods=['POST'])
@login_required
def set_desk_phone_caller_id():
    """Set the user's default caller ID for outbound calls."""
    caller_id = request.form.get('caller_id', '').strip()

    user = get_current_user()
    db = get_db()

    # Update caller ID on staff extension
    ext = db.get_staff_extension(user.email)
    if not ext:
        flash('No staff extension found', 'error')
        return redirect(url_for('web.my_devices'))

    db.update_staff_extension_caller_id(
        email=user.email,
        caller_id=caller_id if caller_id else None,
        updated_by=_audit_tag(user)
    )

    db.log_activity(
        action="set_default_caller_id",
        target=user.email,
        details=f"Set default caller ID to: {caller_id or 'None'}",
        performed_by=_audit_tag(user)
    )

    if caller_id:
        flash(f'Default caller ID set to {caller_id}', 'success')
    else:
        flash('Default caller ID cleared', 'success')

    return redirect(url_for('web.my_devices'))


@web_bp.route('/my-desk-phone/regenerate', methods=['POST'])
@login_required
def regenerate_desk_phone_password():
    """Regenerate the user's desk phone password."""
    service = get_twilio_service()
    db = get_db()
    user = get_current_user()

    cred_list_sid = get_twilio_config('twilio_sip_credential_list_sid')
    if not cred_list_sid:
        flash('Desk phone credentials not configured', 'error')
        return redirect(url_for('web.my_devices'))

    # Get user's existing credential
    credential = db.get_user_by_email(user.email)
    if not credential:
        flash('No desk phone credential found', 'error')
        return redirect(url_for('web.my_devices'))

    # Generate new password
    new_password = _generate_sip_password()

    # Update in Twilio
    result = service.update_user_credential_password(
        credential_list_sid=cred_list_sid,
        credential_sid=credential['sid'],
        new_password=new_password
    )

    if result.get('success'):
        db.log_activity(
            action="regenerate_desk_phone_password",
            target=credential['username'],
            details=f"Regenerated SIP password",
            performed_by=_audit_tag(user)
        )
        # Pass new password via query param to show once
        return redirect(url_for('web.my_devices', new_password=new_password))
    else:
        flash(f'Failed to regenerate password: {result.get("error")}', 'error')
        return redirect(url_for('web.my_devices'))


@web_bp.route('/my-devices')
@login_required
def my_devices():
    """User's device management page.

    Shows SIP credentials and ring settings (browser/SIP toggles).
    Auto-creates SIP credentials on first visit.
    """
    service = get_twilio_service()
    db = get_db()
    user = get_current_user()

    # Check if SIP is configured for this tenant
    tenant_sip_cred_list_sid = get_twilio_config('twilio_sip_credential_list_sid')
    sip_configured = bool(tenant_sip_cred_list_sid)
    sip_domain = None
    credential = None
    stored_password = None
    callable_numbers = []
    new_password = request.args.get('new_password')  # From regeneration redirect

    if sip_configured:
        # Get SIP domain from tenant record (avoids flaky Twilio .list() API)
        sip_domain = get_twilio_config('twilio_sip_domain')
        if not sip_domain and service.is_configured:
            # Fallback: fetch from Twilio API
            domains = service.get_sip_domains()
            if domains:
                sip_domain = domains[0]['domain_name']

        # Check if user already has a credential
        credential = db.get_user_by_email(user.email)

        if not credential:
            # Auto-create or link credential for this user
            username = _email_to_sip_username(user.email)

            # Check if this username already exists in Twilio
            existing_creds = service.get_credentials_in_list(tenant_sip_cred_list_sid)
            existing_cred = next((c for c in existing_creds if c['username'] == username), None)

            if existing_cred:
                # Username already exists in Twilio - link it to this user
                db.upsert_user({
                    "sid": existing_cred['sid'],
                    "username": username,
                    "friendly_name": user.name,
                    "staff_email": user.email,
                    "is_active": 1,
                    "synced_at": None,
                })
                db.log_activity(
                    action="link_desk_phone",
                    target=username,
                    details=f"Linked existing SIP credential to {user.name}",
                    performed_by=_audit_tag(user)
                )
                credential = db.get_user_by_email(user.email)
                # Don't show password - they need to use existing or regenerate
                flash('Your existing desk phone credential has been linked. Use your existing password, or click Regenerate to get a new one.', 'info')
            else:
                # Create new credential
                new_password = _generate_sip_password()

                result = service.create_user_credential(
                    credential_list_sid=tenant_sip_cred_list_sid,
                    username=username,
                    password=new_password,
                    friendly_name=user.name,
                    staff_email=user.email
                )

                if result.get('success'):
                    db.log_activity(
                        action="auto_create_desk_phone",
                        target=username,
                        details=f"Auto-created SIP credential for {user.name}",
                        performed_by=_audit_tag(user)
                    )
                    # Fetch the newly created credential
                    credential = db.get_user_by_email(user.email)
                else:
                    flash(f'Failed to create desk phone credentials: {result.get("error")}', 'error')

        # Get stored password if available
        if credential:
            stored_password = db.get_user_password(credential['sid'])

        # Get available caller IDs (owned numbers + verified caller IDs)
        phone_numbers = db.get_phone_numbers()
        callable_numbers = [{'phone_number': n['phone_number'], 'friendly_name': n.get('friendly_name'), 'section': n.get('section')}
                            for n in phone_numbers if n.get('section')]
        verified_caller_ids = db.get_verified_caller_ids(active_only=True)
        for vcid in verified_caller_ids:
            callable_numbers.append({
                'phone_number': vcid['phone_number'],
                'friendly_name': vcid['friendly_name'],
                'section': vcid.get('section'),
            })

    # Get user's ring settings and staff extension
    ring_settings = db.get_user_ring_settings(user.email)
    staff_ext = db.get_staff_extension(user.email)

    return render_template('my_devices.html',
                         sip_configured=sip_configured,
                         sip_domain=sip_domain,
                         credential=credential,
                         new_password=new_password,
                         stored_password=stored_password,
                         callable_numbers=callable_numbers,
                         ring_settings=ring_settings,
                         staff_ext=staff_ext,
                         current_user=user)


@web_bp.route('/my-devices/vvx300-setup')
@login_required
def vvx300_setup():
    """Setup guide for Polycom VVX 300 desk phones."""
    user = get_current_user()
    db = get_db()
    service = get_twilio_service()

    credential = None
    sip_domain = None
    stored_password = None
    if get_twilio_config('twilio_sip_credential_list_sid') and service.is_configured:
        domains = service.get_sip_domains()
        if domains:
            sip_domain = domains[0]['domain_name']
        credential = db.get_user_by_email(user.email)
        if credential:
            stored_password = credential.get('password')

    return render_template('vvx300_setup.html',
                         credential=credential,
                         sip_domain=sip_domain,
                         stored_password=stored_password,
                         current_user=user)


@web_bp.route('/my-devices/ring-settings', methods=['POST'])
@login_required
def update_ring_settings():
    """Update user's ring settings (which devices ring on incoming calls)."""
    db = get_db()
    user = get_current_user()

    # Checkboxes only submit if checked, so missing = False
    ring_browser = 'ring_browser' in request.form
    ring_sip = 'ring_sip' in request.form

    db.update_user_ring_settings(
        email=user.email,
        ring_browser=ring_browser,
        ring_sip=ring_sip,
        updated_by=_audit_tag(user)
    )

    db.log_activity(
        action="update_ring_settings",
        target=user.email,
        details=f"Ring browser: {ring_browser}, Ring SIP: {ring_sip}",
        performed_by=_audit_tag(user)
    )

    flash('Ring settings updated.', 'success')
    return redirect(url_for('web.my_devices'))


@web_bp.route('/phone')
@login_required
def phone():
    """Browser-based softphone for making and receiving calls."""
    service = get_twilio_service()
    user = get_current_user()

    if not service.is_configured:
        return render_template('setup_required.html',
                             current_user=user)

    db = get_db()

    # Resolve outbound caller ID via priority chain
    from rinq.services.caller_id import resolve_caller_id
    cid = resolve_caller_id(user.email, db)
    default_caller_id = cid['caller_id']
    caller_id_display = cid['display']

    # Check if API key is configured (required for browser phone)
    api_key_configured = bool(get_twilio_config('twilio_api_key') and get_twilio_config('twilio_api_secret'))
    twiml_app_configured = bool(get_twilio_config('twilio_twiml_app_sid'))

    # Get staff extension and dial-in number for display
    staff_ext = db.get_or_create_staff_extension(user.email, f'session:{user.email}')
    ext_directory_number = db.get_bot_setting('extension_directory_number')

    response = make_response(render_template('phone.html',
                         default_caller_id=default_caller_id,
                         caller_id_display=caller_id_display,
                         api_key_configured=api_key_configured,
                         twiml_app_configured=twiml_app_configured,
                         staff_ext=staff_ext,
                         ext_directory_number=ext_directory_number,
                         current_user=user))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


# =============================================================================
# Extracted route modules
# =============================================================================
from rinq.web.admin_queue_routes import register as _register_queue_routes
_register_queue_routes(web_bp)

from rinq.web.admin_flow_routes import register as _register_flow_routes
_register_flow_routes(web_bp)

from rinq.web.admin_schedule_routes import register as _register_schedule_routes
_register_schedule_routes(web_bp)

from rinq.web.admin_audio_routes import register as _register_audio_routes
_register_audio_routes(web_bp)
