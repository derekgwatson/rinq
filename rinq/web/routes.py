"""
Web routes for Tina (Twilio PBX Manager).

Provides dashboard for:
- Viewing and managing phone numbers
- Setting up forwarding
- Managing call flows, queues, and schedules
- Viewing activity log
"""

import json
import logging
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, make_response

logger = logging.getLogger(__name__)

from rinq.services.auth import login_required, admin_required, manager_required, get_current_user
from rinq.services.twilio_service import get_twilio_service
from rinq.database.db import get_db
from rinq.config import config
try:
    from shared.config.ports import get_shared_css_context
except ImportError:
    def get_shared_css_context():
        return {'shared_css_url': None}

web_bp = Blueprint('web', __name__, template_folder='templates')


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
    ctx['softphone_enabled'] = bool(
        config.twilio_api_key and config.twilio_api_secret and config.twilio_twiml_app_sid
    )

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
@login_required
def index():
    """User dashboard - extension settings and queue membership."""
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
        import re
        # Normalise to +614 format
        forward_to = forward_to.replace(' ', '').replace('-', '')
        if forward_to.startswith('04'):
            forward_to = '+61' + forward_to[1:]
        elif forward_to.startswith('614'):
            forward_to = '+' + forward_to
        elif not forward_to.startswith('+614'):
            flash('Invalid mobile number. Must be an Australian mobile (04XX XXX XXX).', 'error')
            return redirect(url_for('web.index'))

        # Validate format
        if not re.match(r'^\+614\d{8}$', forward_to):
            flash('Invalid mobile number format.', 'error')
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
        'show_in_pam': request.form.get('show_in_pam') == '1',
        'forward_to': forward_to,
        'forward_mode': request.form.get('forward_mode', 'always'),
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
    service = get_twilio_service()
    user = get_current_user()

    if not service.is_configured:
        return render_template('setup_required.html', current_user=user)

    db = get_db()

    # Get counts for tiles
    phone_numbers = service.get_phone_numbers()
    account_info = service.get_account_info()

    return render_template('admin.html',
                         phone_numbers_count=len(phone_numbers),
                         verified_caller_ids_count=len(db.get_verified_caller_ids(active_only=False)),
                         queues=db.get_queues(),
                         call_flows=db.get_call_flows(),
                         schedules=db.get_schedules(),
                         audio_files=db.get_audio_files(),
                         holiday_templates=db.get_holiday_templates(),
                         account_info=account_info,
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
    """Manage staff extensions and Tina activation status."""
    user = get_current_user()
    db = get_db()

    # Sync from staff directory - create extensions for any active staff who don't have one
    try:
        from rinq.integrations import get_staff_directory
        staff_dir = get_staff_directory()
        peter_staff = staff_dir.get_active_staff() if staff_dir else []
        if peter_staff:
            import re

            def _normalize_au_mobile(number):
                """Normalize Australian mobile to +614 format, or return None."""
                if not number:
                    return None
                cleaned = re.sub(r'[\s\-()]', '', number)
                if cleaned.startswith('04') and len(cleaned) == 10:
                    return '+61' + cleaned[1:]
                if cleaned.startswith('+614') and len(cleaned) == 12:
                    return cleaned
                if cleaned.startswith('614') and len(cleaned) == 11:
                    return '+' + cleaned
                return None

            created = 0
            updated = 0
            forwarding_set = 0
            for staff in peter_staff:
                email = (staff.get('google_primary_email') or staff.get('work_email') or '').lower().strip()
                if not email:
                    continue
                peter_ext = staff.get('extension', '').strip()
                peter_mobile = _normalize_au_mobile(staff.get('phone_mobile', ''))
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
                        'show_in_pam': existing.get('show_in_pam', False),
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
    """Toggle a staff member's active status in Tina.

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
        now = __import__('datetime').datetime.utcnow().isoformat()
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

    # Get all Tina users and check coverage
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
            if config.twilio_default_caller_id:
                caller_id = config.twilio_default_caller_id
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
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action='caller_id_updated',
            target=email,
            details=f"Caller ID set to {new_caller_id or 'auto'}" +
                    (f" (was {current})" if current else ""),
            performed_by=f"session:{user.email}"
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

    # Normalize to E.164 format if needed
    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number

    user = get_current_user()
    db = get_db()

    try:
        db.add_verified_caller_id(
            phone_number=phone_number,
            friendly_name=friendly_name or None,
            section=section or None,
            notes=notes or None,
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="add_verified_caller_id",
            target=phone_number,
            details=f"Added verified caller ID: {friendly_name or phone_number}",
            performed_by=f"session:{user.email}"
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
    from rinq.services.twilio_service import get_twilio_service

    user = get_current_user()
    twilio = get_twilio_service()

    result = twilio.sync_verified_caller_ids(performed_by=f"session:{user.email}")

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
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_verified_caller_id",
            target=phone_number,
            details=f"Updated verified caller ID: {'active' if is_active else 'inactive'}",
            performed_by=f"session:{user.email}"
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
            performed_by=f"session:{user.email}"
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

    # Get all staff who have logged into Tina (have extensions)
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

    # --- Audio type definitions ---
    audio_type_defs = [
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
    result = service.sync_phone_numbers(performed_by=f"session:{user.email}")

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
    result = service.update_forwarding(sid, forward_to, performed_by=f"session:{user.email}")

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
        db.add_assignment(sid, staff_email, True, True, f"session:{user.email}")
        db.log_activity(
            action="assign_user",
            target=staff_email,
            details=f"Assigned to phone number {sid}",
            performed_by=f"session:{user.email}"
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
        performed_by=f"session:{user.email}"
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
    """View call statistics and reporting dashboard."""
    from rinq.services.reporting_service import get_reporting_service
    from rinq.database.db import get_db

    period = request.args.get('period', 'today')
    queue_filter = request.args.get('queue', '')

    db = get_db()
    user = get_current_user()

    # Admins see all queues; regular users see only their queues
    if user.is_admin:
        queues = db.get_queues()
    else:
        queues = db.get_queues_for_user(user.email)

    # If user has exactly one queue, default to it
    if not queue_filter and len(queues) == 1:
        queue_filter = str(queues[0]['id'])

    # Resolve queue name(s) and member emails for filtering
    queue_name = None
    queue_names = None
    agent_emails = None
    if queue_filter:
        # Specific queue selected
        for q in queues:
            if str(q['id']) == queue_filter:
                queue_name = q['name']
                members = db.get_queue_members(q['id'])
                agent_emails = [m['user_email'] for m in members]
                break
    else:
        # "All Queues" — filter to only queued calls + outbound by all queue members
        queue_names = [q['name'] for q in queues] if queues else None
        if queues:
            all_emails = set()
            for q in queues:
                members = db.get_queue_members(q['id'])
                all_emails.update(m['user_email'] for m in members)
            agent_emails = list(all_emails) if all_emails else None

    # For managers, get their team from staff directory for agent stats
    team_emails = None
    user_role = getattr(user, '_role', 'user')
    if user_role == 'manager' or user.is_admin:
        from rinq.integrations import get_staff_directory
        staff_dir = get_staff_directory()
        if staff_dir:
            try:
                if user.is_admin:
                    staff_list = staff_dir.get_active_staff()
                    team_emails = [s.get('work_email') or s.get('google_primary_email') or s.get('email') for s in staff_list if s.get('work_email') or s.get('google_primary_email') or s.get('email')]
                else:
                    reportees = staff_dir.get_reportees(user.email, recursive=True)
                    team_emails = [r.get('work_email') or r.get('google_primary_email') or r.get('email') for r in reportees if r.get('work_email') or r.get('google_primary_email') or r.get('email')]
                    if user.email not in team_emails:
                        team_emails.append(user.email)
            except Exception as e:
                logger.warning(f"Failed to get team from staff directory: {e}")

    service = get_reporting_service()
    report_data = service.get_report_data(period, queue_name=queue_name, queue_names=queue_names,
                                          agent_emails=agent_emails, team_emails=team_emails)

    return render_template('reports.html',
                         report=report_data,
                         queues=queues,
                         current_queue=queue_filter,
                         format_duration=service.format_duration,
                         format_wait_time=service.format_wait_time,
                         current_user=get_current_user())


@web_bp.route('/leaderboard')
@login_required
def leaderboard():
    """Call leaderboard — gamified agent performance ranking."""
    from rinq.services.reporting_service import get_reporting_service
    from rinq.database.db import get_db

    period = request.args.get('period', 'today')
    queue_filter = request.args.get('queue', '')

    db = get_db()
    user = get_current_user()

    if user.is_admin:
        queues = db.get_queues()
    else:
        queues = db.get_queues_for_user(user.email)

    if not queue_filter and len(queues) == 1:
        queue_filter = str(queues[0]['id'])

    queue_name = None
    queue_names = None
    agent_emails = None
    if queue_filter:
        for q in queues:
            if str(q['id']) == queue_filter:
                queue_name = q['name']
                members = db.get_queue_members(q['id'])
                agent_emails = [m['user_email'] for m in members]
                break
    else:
        queue_names = [q['name'] for q in queues] if queues else None
        if queues:
            all_emails = set()
            for q in queues:
                members = db.get_queue_members(q['id'])
                all_emails.update(m['user_email'] for m in members)
            agent_emails = list(all_emails) if all_emails else None

    service = get_reporting_service()
    report_data = service.get_report_data(period, queue_name=queue_name, queue_names=queue_names, agent_emails=agent_emails)

    # Enrich agent stats are already done by reporting service
    agents = report_data.get('agent_stats', [])

    # Build rankings for different categories
    by_calls = sorted(agents, key=lambda a: a['answered_calls'], reverse=True)
    by_talk_time = sorted(agents, key=lambda a: a['total_duration_seconds'], reverse=True)

    return render_template('leaderboard.html',
                         period=period,
                         by_calls=by_calls,
                         by_talk_time=by_talk_time,
                         queues=queues,
                         current_queue=queue_filter,
                         format_duration=service.format_duration,
                         current_user=user,
                         active_nav='leaderboard')


@web_bp.route('/team')
@manager_required
def team():
    """Team management — add/remove Tina users and managers."""
    from rinq.integrations import get_permission_service, get_staff_directory

    user = get_current_user()
    perms = get_permission_service()
    staff_dir = get_staff_directory()

    # Get current Tina permissions
    permissions = perms.get_permissions('tina') if perms else []

    # Only show elevated roles (user access is automatic for all domain staff)
    permissions = [p for p in permissions if p.get('role') in ('manager', 'admin')]

    # Get staff list for the dropdown
    elevated_emails = {p.get('email', '').lower() for p in permissions}
    try:
        all_staff = staff_dir.get_active_staff() if staff_dir else []
        # Filter to staff not already elevated
        available_staff = []
        for s in all_staff:
            staff_email = (s.get('work_email') or s.get('google_primary_email') or '').lower()
            if staff_email and staff_email not in elevated_emails:
                s['_email'] = staff_email  # Stash for the template
                available_staff.append(s)
        available_staff.sort(key=lambda s: s.get('name', ''))
    except Exception:
        available_staff = []

    # Sort: admins first, then managers
    role_order = {'admin': 0, 'manager': 1}
    permissions.sort(key=lambda p: (role_order.get(p.get('role', 'manager'), 9), p.get('email', '')))

    is_admin = user.is_admin
    available_roles = [
        {'value': 'manager', 'label': 'Manager'},
    ]
    if is_admin:
        available_roles.append({'value': 'admin', 'label': 'Admin'})

    return render_template('team.html',
                         permissions=permissions,
                         available_roles=available_roles,
                         available_staff=available_staff,
                         is_admin=is_admin,
                         current_user=user,
                         active_nav='team')


@web_bp.route('/team/add', methods=['POST'])
@manager_required
def team_add():
    """Add a user to Tina via permission service."""
    from rinq.integrations import get_permission_service

    user = get_current_user()
    email = request.form.get('email', '').strip().lower()
    role = request.form.get('role', 'user')

    if not email:
        flash('Email is required.', 'danger')
        return redirect(url_for('web.team'))

    # Managers can only assign user/manager
    base_role = role.split(':')[0]
    if base_role == 'admin' and not user.is_admin:
        flash('Only admins can assign admin roles.', 'danger')
        return redirect(url_for('web.team'))

    perms = get_permission_service()
    if perms and perms.add_permission(email, 'tina', role, user.email):
        flash(f'Added {email} as {role}.', 'success')
    else:
        flash('Failed to add user.', 'danger')

    return redirect(url_for('web.team'))


@web_bp.route('/team/remove', methods=['POST'])
@manager_required
def team_remove():
    """Remove a user from Tina via permission service."""
    from rinq.integrations import get_permission_service

    user = get_current_user()
    email = request.form.get('email', '').strip().lower()

    if not email:
        flash('Email is required.', 'danger')
        return redirect(url_for('web.team'))

    # Don't let managers remove admins
    perms_svc = get_permission_service()
    if not user.is_admin and perms_svc:
        try:
            all_perms = perms_svc.get_permissions('tina')
            target_perm = next((p for p in all_perms if p.get('email', '').lower() == email), None)
            if target_perm and target_perm.get('role') == 'admin':
                flash('Only admins can remove other admins.', 'danger')
                return redirect(url_for('web.team'))
        except Exception:
            pass

    # Don't let users remove themselves
    if email == user.email.lower():
        flash("You can't remove yourself.", 'danger')
        return redirect(url_for('web.team'))

    if perms_svc and perms_svc.remove_permission(email, 'tina', user.email):
        flash(f'Removed {email}.', 'success')
    else:
        flash('Failed to remove user.', 'danger')

    return redirect(url_for('web.team'))


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

    # Check if user is admin or manager (both can see all recordings)
    is_admin = user.is_admin
    user_role = getattr(user, '_role', 'user')
    can_see_all = is_admin or user_role == 'manager'

    # Get recordings based on filters
    # Regular users can only see their own recordings
    # Admins and managers can toggle between "mine" and "all"
    if not can_see_all or filter_staff == 'mine':
        # Regular users always get their own, admins/managers get theirs if they select "mine"
        recordings_list = db.get_recordings_for_staff(user.email, limit=100)
        # Apply call type filter if specified
        if filter_type:
            recordings_list = [r for r in recordings_list if r.get('call_type') == filter_type]
    elif filter_type:
        recordings_list = db.get_recording_log(limit=100, call_type=filter_type, exclude_voicemail=True)
    else:
        recordings_list = db.get_recording_log(limit=100, exclude_voicemail=True)

    return render_template('recordings.html',
                         recordings=recordings_list,
                         recording_enabled=recording_enabled,
                         recordings_group=config.recordings_group_email,
                         filter_type=filter_type,
                         filter_staff=filter_staff,
                         is_admin=can_see_all,
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

    if not config.sip_credential_list_sid:
        flash('No credential list configured in .env', 'error')
        return redirect(url_for('web.setup'))

    domains = service.get_sip_domains()
    if not domains:
        flash('No SIP domain found. Create one in Twilio Console first.', 'error')
        return redirect(url_for('web.setup'))

    domain_sid = domains[0]['sid']
    result = service.associate_credential_list_with_domain(domain_sid, config.sip_credential_list_sid)

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

    configured = bool(config.sip_credential_list_sid)

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

    if not config.sip_credential_list_sid:
        flash('Credential list not configured', 'error')
        return redirect(url_for('web.desk_phones'))

    result = service.create_user_credential(
        credential_list_sid=config.sip_credential_list_sid,
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
            performed_by=f"session:{user.email}"
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

    if not config.sip_credential_list_sid:
        flash('Credential list not configured', 'error')
        return redirect(url_for('web.desk_phones'))

    # Delete from Twilio
    result = service.delete_user_credential(
        credential_list_sid=config.sip_credential_list_sid,
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
            performed_by=f"session:{user.email}"
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

    result = service.sync_credentials(performed_by=f"session:{user.email}")

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

    if not config.sip_credential_list_sid:
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
        credential_list_sid=config.sip_credential_list_sid,
        credential_sid=sid,
        new_password=new_password
    )

    if result.get('success'):
        db.log_activity(
            action="admin_regenerate_password",
            target=credential['username'],
            details=f"Admin regenerated SIP password for {credential.get('friendly_name', credential['username'])}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Password regenerated for {credential.get('friendly_name', credential['username'])}", 'success')
    else:
        flash(f'Failed to regenerate password: {result.get("error")}', 'error')

    return redirect(url_for('web.desk_phones'))


# =============================================================================
# User Desk Phone (Self-Service)
# =============================================================================

def _generate_sip_password() -> str:
    """Generate a memorable password for SIP credentials.

    Uses shared password generator for human-friendly passwords like:
    purple-tiger-sunset-42
    """
    try:
        from shared.password_generator import generate_memorable_password
        return generate_memorable_password()
    except ImportError:
        import secrets
        import string
        chars = string.ascii_lowercase + string.digits
        return '-'.join(''.join(secrets.choice(chars) for _ in range(5)) for _ in range(4))


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
        updated_by=f"session:{user.email}"
    )

    db.log_activity(
        action="set_default_caller_id",
        target=user.email,
        details=f"Set default caller ID to: {caller_id or 'None'}",
        performed_by=f"session:{user.email}"
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

    from flask import g
    tenant = getattr(g, 'tenant', None)
    cred_list_sid = (tenant.get('twilio_sip_credential_list_sid') if tenant else None) or config.sip_credential_list_sid
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
            performed_by=f"session:{user.email}"
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

    # Check if SIP is configured — use tenant's credential list SID if available
    from flask import g
    tenant = getattr(g, 'tenant', None)
    tenant_sip_cred_list_sid = (tenant.get('twilio_sip_credential_list_sid') if tenant else None) or config.sip_credential_list_sid
    sip_configured = bool(tenant_sip_cred_list_sid)
    sip_domain = None
    credential = None
    stored_password = None
    callable_numbers = []
    new_password = request.args.get('new_password')  # From regeneration redirect

    if sip_configured:
        # Get SIP domain for display
        if service.is_configured:
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
                    performed_by=f"session:{user.email}"
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
                        performed_by=f"session:{user.email}"
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
    if config.sip_credential_list_sid and service.is_configured:
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
        updated_by=f"session:{user.email}"
    )

    db.log_activity(
        action="update_ring_settings",
        target=user.email,
        details=f"Ring browser: {ring_browser}, Ring SIP: {ring_sip}",
        performed_by=f"session:{user.email}"
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

    # Get user's default caller ID from their staff extension
    staff_ext = db.get_staff_extension(user.email)
    default_caller_id = staff_ext.get('default_caller_id') if staff_ext else None

    # If no default set, check if user is directly assigned to a number
    if not default_caller_id:
        assignments = db.get_assignments_for_user(user.email)
        if assignments:
            # Use the first assigned number (with can_make) as caller ID
            for assignment in assignments:
                if assignment.get('can_make'):
                    # Look up the actual phone number from the SID
                    phone_numbers = db.get_phone_numbers()
                    for number in phone_numbers:
                        if number['sid'] == assignment['phone_number_sid']:
                            default_caller_id = number['phone_number']
                            break
                    if default_caller_id:
                        break

    # If no assignment, fall back to user's section-based number
    if not default_caller_id:
        user_section = None
        from rinq.integrations import get_staff_directory
        staff_dir = get_staff_directory()
        if staff_dir:
            staff_data = staff_dir.get_staff_by_email(user.email)
            if staff_data:
                user_section = staff_data.get('section')

        # Find a matching number for their section
        if user_section:
            phone_numbers = db.get_phone_numbers()
            for number in phone_numbers:
                if number.get('section') == user_section:
                    default_caller_id = number['phone_number']
                    break

    # If still no caller ID, use system default
    if not default_caller_id:
        default_caller_id = config.twilio_default_caller_id

    # Get friendly name for display
    caller_id_display = default_caller_id
    if default_caller_id:
        phone_numbers = db.get_phone_numbers()
        for number in phone_numbers:
            if number['phone_number'] == default_caller_id:
                caller_id_display = number.get('friendly_name') or default_caller_id
                if number.get('section'):
                    caller_id_display += f" ({number['section']})"
                break
        else:
            # Check verified caller IDs
            verified_caller_ids = db.get_verified_caller_ids(active_only=True)
            for vcid in verified_caller_ids:
                if vcid['phone_number'] == default_caller_id:
                    caller_id_display = vcid.get('friendly_name') or default_caller_id
                    break

    # Check if API key is configured (required for browser phone)
    api_key_configured = bool(config.twilio_api_key and config.twilio_api_secret)
    twiml_app_configured = bool(config.twilio_twiml_app_sid)

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
# Queue Management
# =============================================================================

@web_bp.route('/admin/queue/create', methods=['POST'])
@admin_required
def create_queue():
    """Create a new queue."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    ring_strategy = request.form.get('ring_strategy', 'simultaneous')
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
                'allow_self_service': allow_self_service,
                'allow_voicemail_escape': allow_voicemail_escape,
                'offer_callback': offer_callback,
                'callback_threshold': callback_threshold,
                'escape_announcement_delay': escape_announcement_delay,
                'escape_repeat_interval': escape_repeat_interval,
                'reject_action': reject_action,
                'hold_music_id': hold_music_id,
            },
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="create_queue",
            target=name,
            details=f"Created queue ID {queue_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Created queue '{name}'", "success")
    except Exception as e:
        flash(f"Failed to create queue: {e}", "error")

    return redirect(url_for('web.admin'))


@web_bp.route('/admin/queue/<int:queue_id>/member/add', methods=['POST'])
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
            created_by=f"session:{user.email}"
        )

        # Note: ring_browser and ring_sip default to True in users table,
        # so no need to create device entries

        db.log_activity(
            action="add_queue_member",
            target=user_email,
            details=f"Added to queue {queue_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Added {user_email} to queue", "success")
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            flash(f"{user_email} is already in this queue", "error")
        else:
            flash(f"Failed to add member: {e}", "error")

    return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')


@web_bp.route('/admin/queue/<int:queue_id>/member/<int:member_id>/remove', methods=['POST'])
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
        performed_by=f"session:{user.email}"
    )
    flash("Removed from queue", "success")

    return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')


@web_bp.route('/admin/queue/<int:queue_id>/update', methods=['POST'])
@admin_required
def update_queue(queue_id):
    """Update a queue's settings."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    ring_strategy = request.form.get('ring_strategy', 'simultaneous')
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
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_queue",
            target=name,
            details=f"Updated queue {queue_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Updated queue '{name}'", "success")
    except Exception as e:
        flash(f"Failed to update queue: {e}", "error")

    return redirect(url_for('web.admin_queues') + f'#queue_{queue_id}')


@web_bp.route('/admin/queue/<int:queue_id>/delete', methods=['POST'])
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
            performed_by=f"session:{user.email}"
        )
        flash("Queue deleted", "success")
    except Exception as e:
        flash(f"Failed to delete queue: {e}", "error")

    return redirect(url_for('web.admin_queues'))


# =============================================================================
# Call Flow Management
# =============================================================================

@web_bp.route('/admin/call-flow/create', methods=['POST'])
@admin_required
def create_call_flow():
    """Create a new call flow."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    open_queue_id = request.form.get('open_queue_id')
    schedule_id = request.form.get('schedule_id')

    if not name:
        flash("Call flow name is required", "error")
        return redirect(url_for('web.admin'))

    user = get_current_user()
    db = get_db()

    greeting_audio_id = request.form.get('greeting_audio_id')
    closed_audio_id = request.form.get('closed_audio_id')
    closed_action = request.form.get('closed_action', 'ai_receptionist')
    open_no_answer_action = request.form.get('open_no_answer_action', 'ai_receptionist')
    voicemail_destination_id = request.form.get('voicemail_destination_id', '').strip()
    closed_forward_number = request.form.get('closed_forward_number', '').strip()
    open_forward_number = request.form.get('open_forward_number', '').strip()
    closed_message_parts = request.form.get('closed_message_parts', '').strip() or None

    open_action = request.form.get('open_action', 'queue')
    extension_prompt_audio_id = request.form.get('extension_prompt_audio_id')
    extension_invalid_audio_id = request.form.get('extension_invalid_audio_id')

    try:
        flow_id = db.create_call_flow(
            data={
                'name': name,
                'description': description or None,
                'greeting_audio_id': int(greeting_audio_id) if greeting_audio_id else None,
                'open_action': open_action,
                'open_queue_id': int(open_queue_id) if open_queue_id else None,
                'open_forward_number': open_forward_number or None,
                'open_no_answer_action': open_no_answer_action,
                'schedule_id': int(schedule_id) if schedule_id else None,
                'closed_action': closed_action,
                'closed_audio_id': int(closed_audio_id) if closed_audio_id else None,
                'closed_message_parts': closed_message_parts,
                'voicemail_destination_id': int(voicemail_destination_id) if voicemail_destination_id else None,
                'closed_forward_number': closed_forward_number or None,
                'extension_prompt_audio_id': int(extension_prompt_audio_id) if extension_prompt_audio_id else None,
                'extension_invalid_audio_id': int(extension_invalid_audio_id) if extension_invalid_audio_id else None,
            },
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="create_call_flow",
            target=name,
            details=f"Created call flow ID {flow_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Created call flow '{name}'", "success")
    except Exception as e:
        flash(f"Failed to create call flow: {e}", "error")

    return redirect(url_for('web.admin'))


@web_bp.route('/admin/phone/<sid>/call-flow', methods=['POST'])
@admin_required
def assign_call_flow(sid):
    """Assign a call flow to a phone number."""
    call_flow_id = request.form.get('call_flow_id')

    user = get_current_user()
    db = get_db()

    try:
        db.set_phone_number_call_flow(
            phone_sid=sid,
            call_flow_id=int(call_flow_id) if call_flow_id else None,
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="assign_call_flow",
            target=sid,
            details=f"Assigned call flow {call_flow_id}",
            performed_by=f"session:{user.email}"
        )
        flash("Call flow assigned", "success")
    except Exception as e:
        flash(f"Failed to assign call flow: {e}", "error")

    return redirect(url_for('web.admin_phone_numbers'))


@web_bp.route('/admin/call-flow/<int:flow_id>/update', methods=['POST'])
@admin_required
def update_call_flow(flow_id):
    """Update an existing call flow."""
    user = get_current_user()
    db = get_db()

    name = request.form.get('name', '').strip()
    if not name:
        flash("Call flow name is required", "error")
        return redirect(url_for('web.admin_call_flows'))

    description = request.form.get('description', '').strip()
    greeting_audio_id = request.form.get('greeting_audio_id')
    schedule_id = request.form.get('schedule_id')
    open_action = request.form.get('open_action', 'queue')
    open_queue_id = request.form.get('open_queue_id')
    closed_action = request.form.get('closed_action', 'ai_receptionist')
    open_no_answer_action = request.form.get('open_no_answer_action', 'ai_receptionist')
    closed_audio_id = request.form.get('closed_audio_id')
    closed_forward_number = request.form.get('closed_forward_number', '').strip()
    open_forward_number = request.form.get('open_forward_number', '').strip()
    voicemail_destination_id = request.form.get('voicemail_destination_id', '').strip()
    extension_prompt_audio_id = request.form.get('extension_prompt_audio_id')
    extension_invalid_audio_id = request.form.get('extension_invalid_audio_id')
    closed_message_parts = request.form.get('closed_message_parts', '').strip() or None

    try:
        db.update_call_flow(
            flow_id=flow_id,
            data={
                'name': name,
                'description': description or None,
                'greeting_audio_id': int(greeting_audio_id) if greeting_audio_id else None,
                'schedule_id': int(schedule_id) if schedule_id else None,
                'open_action': open_action,
                'open_queue_id': int(open_queue_id) if open_queue_id else None,
                'open_forward_number': open_forward_number or None,
                'open_no_answer_action': open_no_answer_action,
                'closed_action': closed_action,
                'closed_audio_id': int(closed_audio_id) if closed_audio_id else None,
                'closed_message_parts': closed_message_parts,
                'closed_forward_number': closed_forward_number or None,
                'voicemail_destination_id': int(voicemail_destination_id) if voicemail_destination_id else None,
                'extension_prompt_audio_id': int(extension_prompt_audio_id) if extension_prompt_audio_id else None,
                'extension_invalid_audio_id': int(extension_invalid_audio_id) if extension_invalid_audio_id else None,
            },
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_call_flow",
            target=name,
            details=f"Updated call flow ID {flow_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Call flow '{name}' updated", "success")
    except Exception as e:
        flash(f"Failed to update call flow: {e}", "error")

    return redirect(url_for('web.admin_call_flows'))


@web_bp.route('/admin/call-flow/<int:flow_id>/clone', methods=['POST'])
@admin_required
def clone_call_flow(flow_id):
    """Clone a call flow."""
    user = get_current_user()
    db = get_db()

    source = db.get_call_flow(flow_id)
    if not source:
        flash("Call flow not found", "error")
        return redirect(url_for('web.admin_call_flows'))

    try:
        clone_data = {
            'name': f"{source['name']} (Copy)",
            'description': source.get('description'),
            'greeting_audio_id': source.get('greeting_audio_id'),
            'schedule_id': source.get('schedule_id'),
            'open_action': source.get('open_action', 'queue'),
            'open_queue_id': source.get('open_queue_id'),
            'open_forward_number': source.get('open_forward_number'),
            'open_audio_id': source.get('open_audio_id'),
            'open_no_answer_action': source.get('open_no_answer_action', 'ai_receptionist'),
            'closed_action': source.get('closed_action', 'message'),
            'closed_audio_id': source.get('closed_audio_id'),
            'closed_forward_number': source.get('closed_forward_number'),
            'voicemail_email': source.get('voicemail_email'),
            'voicemail_destination_id': source.get('voicemail_destination_id'),
            'extension_prompt_audio_id': source.get('extension_prompt_audio_id'),
            'extension_no_answer_action': source.get('extension_no_answer_action', 'voicemail'),
        }
        new_id = db.create_call_flow(data=clone_data, created_by=f"session:{user.email}")
        db.log_activity(
            action="clone_call_flow",
            target=clone_data['name'],
            details=f"Cloned from '{source['name']}' (ID {flow_id}) to new ID {new_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Cloned call flow '{source['name']}' as '{clone_data['name']}'", "success")
    except Exception as e:
        flash(f"Failed to clone call flow: {e}", "error")

    return redirect(url_for('web.admin_call_flows'))


@web_bp.route('/admin/call-flow/<int:flow_id>/delete', methods=['POST'])
@admin_required
def delete_call_flow(flow_id):
    """Delete a call flow."""
    user = get_current_user()
    db = get_db()

    # Get the flow name for logging
    flow = db.get_call_flow(flow_id)
    if not flow:
        flash("Call flow not found", "error")
        return redirect(url_for('web.admin_call_flows'))

    try:
        deleted = db.delete_call_flow(flow_id)
        if deleted:
            db.log_activity(
                action="delete_call_flow",
                target=flow['name'],
                details=f"Deleted call flow ID {flow_id}",
                performed_by=f"session:{user.email}"
            )
            flash(f"Call flow '{flow['name']}' deleted", "success")
        else:
            flash(f"Cannot delete '{flow['name']}' - it is still assigned to phone numbers", "error")
    except Exception as e:
        flash(f"Failed to delete call flow: {e}", "error")

    return redirect(url_for('web.admin_call_flows'))


# =============================================================================
# Voicemail Destinations Management
# =============================================================================

@web_bp.route('/admin/voicemail-destination/create', methods=['POST'])
@admin_required
def create_voicemail_destination():
    """Create a new voicemail destination."""
    name = request.form.get('name', '').strip()
    routing_type = request.form.get('routing_type', 'zendesk').strip()
    email = request.form.get('email', '').strip() or None
    description = request.form.get('description', '').strip() or None
    zendesk_group_id_str = request.form.get('zendesk_group_id', '').strip()

    # Convert zendesk_group_id to int or None
    zendesk_group_id = None
    if zendesk_group_id_str:
        try:
            zendesk_group_id = int(zendesk_group_id_str)
        except ValueError:
            pass

    # Validate based on routing type
    if not name:
        flash("Name is required", "error")
        return redirect(url_for('web.admin_call_flows'))

    if routing_type == 'zendesk' and not zendesk_group_id:
        flash("Zendesk group is required for Zendesk routing", "error")
        return redirect(url_for('web.admin_call_flows'))

    if routing_type == 'email' and not email:
        flash("Email address is required for Email routing", "error")
        return redirect(url_for('web.admin_call_flows'))

    user = get_current_user()
    db = get_db()

    try:
        db.create_voicemail_destination(
            data={
                'name': name,
                'routing_type': routing_type,
                'email': email,
                'description': description,
                'zendesk_group_id': zendesk_group_id,
            },
            created_by=f"session:{user.email}"
        )

        detail = f"group_id={zendesk_group_id}" if routing_type == 'zendesk' else f"email={email}"
        db.log_activity(
            action="create_voicemail_destination",
            target=name,
            details=f"Created {routing_type} voicemail destination: {detail}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Voicemail destination '{name}' created", "success")
    except Exception as e:
        if 'UNIQUE constraint' in str(e):
            flash(f"A destination with email '{email}' already exists", "error")
        else:
            flash(f"Failed to create voicemail destination: {e}", "error")

    return redirect(url_for('web.admin_call_flows'))


@web_bp.route('/admin/voicemail-destination/<int:destination_id>/update', methods=['POST'])
@admin_required
def update_voicemail_destination(destination_id):
    """Update a voicemail destination."""
    name = request.form.get('name', '').strip()
    routing_type = request.form.get('routing_type', 'zendesk').strip()
    email = request.form.get('email', '').strip() or None
    description = request.form.get('description', '').strip() or None
    zendesk_group_id_str = request.form.get('zendesk_group_id', '').strip()

    # Convert zendesk_group_id to int or None
    zendesk_group_id = None
    if zendesk_group_id_str:
        try:
            zendesk_group_id = int(zendesk_group_id_str)
        except ValueError:
            pass

    # Validate based on routing type
    if not name:
        flash("Name is required", "error")
        return redirect(url_for('web.admin_call_flows'))

    if routing_type == 'zendesk' and not zendesk_group_id:
        flash("Zendesk group is required for Zendesk routing", "error")
        return redirect(url_for('web.admin_call_flows'))

    if routing_type == 'email' and not email:
        flash("Email address is required for Email routing", "error")
        return redirect(url_for('web.admin_call_flows'))

    user = get_current_user()
    db = get_db()

    destination = db.get_voicemail_destination(destination_id)
    if not destination:
        flash("Voicemail destination not found", "error")
        return redirect(url_for('web.admin_call_flows'))

    try:
        db.update_voicemail_destination(
            destination_id=destination_id,
            data={
                'name': name,
                'routing_type': routing_type,
                'email': email,
                'description': description,
                'zendesk_group_id': zendesk_group_id,
            },
            updated_by=f"session:{user.email}"
        )

        detail = f"group_id={zendesk_group_id}" if routing_type == 'zendesk' else f"email={email}"
        db.log_activity(
            action="update_voicemail_destination",
            target=name,
            details=f"Updated {routing_type} destination: {detail}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Voicemail destination '{name}' updated", "success")
    except Exception as e:
        flash(f"Failed to update voicemail destination: {e}", "error")

    return redirect(url_for('web.admin_call_flows'))


@web_bp.route('/admin/voicemail-destination/<int:destination_id>/delete', methods=['POST'])
@admin_required
def delete_voicemail_destination(destination_id):
    """Delete a voicemail destination."""
    user = get_current_user()
    db = get_db()

    destination = db.get_voicemail_destination(destination_id)
    if not destination:
        flash("Voicemail destination not found", "error")
        return redirect(url_for('web.admin_call_flows'))

    try:
        deleted = db.delete_voicemail_destination(destination_id)
        if deleted:
            db.log_activity(
                action="delete_voicemail_destination",
                target=destination['name'],
                details=f"Deleted voicemail destination ID {destination_id}",
                performed_by=f"session:{user.email}"
            )
            flash(f"Voicemail destination '{destination['name']}' deleted", "success")
        else:
            flash(f"Cannot delete '{destination['name']}' - it is still in use by call flows", "error")
    except Exception as e:
        flash(f"Failed to delete voicemail destination: {e}", "error")

    return redirect(url_for('web.admin_call_flows'))


@web_bp.route('/admin/phone/<sid>/section', methods=['POST'])
@admin_required
def update_phone_section(sid):
    """Update the section for a phone number (for caller ID assignment)."""
    section = request.form.get('section', '').strip() or None

    user = get_current_user()
    db = get_db()

    try:
        db.update_phone_number_section(
            sid=sid,
            section=section,
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_phone_section",
            target=sid,
            details=f"Set section to '{section}'" if section else "Cleared section",
            performed_by=f"session:{user.email}"
        )
        if section:
            flash(f"Section set to {section}", "success")
        else:
            flash("Section cleared", "success")
    except Exception as e:
        flash(f"Failed to update section: {e}", "error")

    return redirect(url_for('web.admin_phone_numbers'))


# =============================================================================
# Schedule Management
# =============================================================================

@web_bp.route('/admin/schedule/create', methods=['POST'])
@admin_required
def create_schedule():
    """Create a new schedule with business hours."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    timezone = request.form.get('timezone', 'Australia/Sydney').strip()

    # Parse business hours from form
    business_hours = {}
    for day in ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']:
        open_time = request.form.get(f'{day}_open', '').strip()
        close_time = request.form.get(f'{day}_close', '').strip()
        if open_time and close_time:
            # Normalize to HH:MM (zero-pad hours for consistent string comparison)
            open_time = open_time.zfill(5)  # "9:00" -> "09:00"
            close_time = close_time.zfill(5)
            business_hours[day] = {'open': open_time, 'close': close_time}

    # Parse closure defaults
    default_closure_action = request.form.get('default_closure_action', '').strip() or None
    default_closure_audio_id = request.form.get('default_closure_audio_id', '').strip()
    default_closure_audio_id = int(default_closure_audio_id) if default_closure_audio_id else None
    default_closure_forward_to = request.form.get('default_closure_forward_to', '').strip() or None

    if not name:
        flash("Schedule name is required", "error")
        return schedule_redirect()

    user = get_current_user()
    db = get_db()

    schedule_id = None
    try:
        schedule_id = db.create_schedule(
            data={
                'name': name,
                'description': description or None,
                'timezone': timezone,
                'business_hours': json.dumps(business_hours) if business_hours else None,
                'default_closure_action': default_closure_action,
                'default_closure_audio_id': default_closure_audio_id,
                'default_closure_forward_to': default_closure_forward_to,
            },
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="create_schedule",
            target=name,
            details=f"Created schedule ID {schedule_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Created schedule '{name}'", "success")
    except Exception as e:
        flash(f"Failed to create schedule: {e}", "error")

    return schedule_redirect(schedule_id)


@web_bp.route('/admin/schedule/<int:schedule_id>/update', methods=['POST'])
@admin_required
def update_schedule(schedule_id):
    """Update a schedule (name, description, timezone, business hours)."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    timezone = request.form.get('timezone', 'Australia/Sydney').strip()

    # Parse business hours from form
    business_hours = {}
    for day in ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']:
        open_time = request.form.get(f'{day}_open', '').strip()
        close_time = request.form.get(f'{day}_close', '').strip()
        if open_time and close_time:
            # Normalize to HH:MM (zero-pad hours for consistent string comparison)
            open_time = open_time.zfill(5)  # "9:00" -> "09:00"
            close_time = close_time.zfill(5)
            business_hours[day] = {'open': open_time, 'close': close_time}

    if not name:
        flash("Schedule name is required", "error")
        return redirect(url_for('web.admin'))

    user = get_current_user()
    db = get_db()

    try:
        db.update_schedule(
            schedule_id=schedule_id,
            data={
                'name': name,
                'description': description or None,
                'timezone': timezone,
                'business_hours': json.dumps(business_hours) if business_hours else None,
            },
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_schedule",
            target=name,
            details=f"Updated schedule ID {schedule_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Updated schedule '{name}'", "success")
    except Exception as e:
        flash(f"Failed to update schedule: {e}", "error")

    return schedule_redirect(schedule_id)


@web_bp.route('/admin/schedule/<int:schedule_id>/closure-defaults', methods=['POST'])
@admin_required
def update_closure_defaults(schedule_id):
    """Update closure defaults for a schedule."""
    default_closure_action = request.form.get('default_closure_action', '').strip() or None
    default_closure_audio_id = request.form.get('default_closure_audio_id', '').strip()
    default_closure_audio_id = int(default_closure_audio_id) if default_closure_audio_id else None
    default_closure_forward_to = request.form.get('default_closure_forward_to', '').strip() or None

    user = get_current_user()
    db = get_db()

    schedule = db.get_schedule(schedule_id)
    if not schedule:
        flash("Schedule not found", "error")
        return schedule_redirect()

    try:
        db.update_schedule(
            schedule_id=schedule_id,
            data={
                # Preserve existing values for fields we're not changing
                'name': schedule['name'],
                'description': schedule['description'],
                'timezone': schedule['timezone'],
                'business_hours': schedule['business_hours'],
                # Update closure defaults
                'default_closure_action': default_closure_action,
                'default_closure_audio_id': default_closure_audio_id,
                'default_closure_forward_to': default_closure_forward_to,
            },
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_closure_defaults",
            target=schedule['name'],
            details=f"Updated closure defaults: action={default_closure_action or 'none'}",
            performed_by=f"session:{user.email}"
        )
        flash("Updated closure defaults", "success")
    except Exception as e:
        flash(f"Failed to update closure defaults: {e}", "error")

    return schedule_redirect(schedule_id)


@web_bp.route('/admin/schedule/<int:schedule_id>/clone', methods=['POST'])
@admin_required
def clone_schedule(schedule_id):
    """Clone a schedule with its business hours and holidays."""
    new_name = request.form.get('name', '').strip()

    if not new_name:
        flash("New schedule name is required", "error")
        return schedule_redirect(schedule_id)

    user = get_current_user()
    db = get_db()

    schedule = db.get_schedule(schedule_id)
    if not schedule:
        flash("Schedule not found", "error")
        return schedule_redirect()

    new_id = None
    try:
        new_id = db.clone_schedule(
            schedule_id=schedule_id,
            new_name=new_name,
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="clone_schedule",
            target=new_name,
            details=f"Cloned schedule '{schedule['name']}' (ID {schedule_id}) to new schedule ID {new_id}",
            performed_by=f"session:{user.email}"
        )
        holiday_count = len(schedule.get('holidays', []))
        flash(f"Created '{new_name}' from '{schedule['name']}' with {holiday_count} holiday{'s' if holiday_count != 1 else ''}", "success")
    except Exception as e:
        flash(f"Failed to clone schedule: {e}", "error")

    # Redirect to the NEW cloned schedule
    return schedule_redirect(new_id if new_id else schedule_id)


@web_bp.route('/admin/schedule/<int:schedule_id>/delete', methods=['POST'])
@admin_required
def delete_schedule(schedule_id):
    """Delete a schedule (only if not used by any call flows)."""
    user = get_current_user()
    db = get_db()

    schedule = db.get_schedule(schedule_id)
    if not schedule:
        flash("Schedule not found", "error")
        return schedule_redirect()

    try:
        db.delete_schedule(schedule_id)
        db.log_activity(
            action="delete_schedule",
            target=schedule['name'],
            details=f"Deleted schedule ID {schedule_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Deleted schedule '{schedule['name']}'", "success")
    except ValueError as e:
        # Schedule is in use
        flash(str(e), "error")
        return schedule_redirect(schedule_id)
    except Exception as e:
        flash(f"Failed to delete schedule: {e}", "error")
        return schedule_redirect(schedule_id)

    return schedule_redirect()


@web_bp.route('/admin/schedule/<int:schedule_id>/holiday/add', methods=['POST'])
@admin_required
def add_holiday(schedule_id):
    """Add a closure to a schedule (one-off or weekly recurring)."""
    name = request.form.get('name', '').strip()
    recurrence = request.form.get('recurrence', 'once')
    date = request.form.get('date', '').strip()
    day_of_week = request.form.get('day_of_week', '').strip()
    start_time = request.form.get('start_time', '').strip()
    end_time = request.form.get('end_time', '').strip()
    audio_id = request.form.get('audio_id')
    action = request.form.get('action', '').strip()
    forward_to = request.form.get('forward_to', '').strip()

    if not name:
        flash("Closure name is required", "error")
        return schedule_redirect(schedule_id)

    # Validate based on recurrence type
    if recurrence == 'once':
        if not date:
            flash("Date is required for one-off closures", "error")
            return schedule_redirect(schedule_id)
        if len(date) != 10:
            flash("Invalid date format. Use YYYY-MM-DD.", "error")
            return schedule_redirect(schedule_id)
        day_of_week_int = None
    else:  # weekly
        if not day_of_week:
            flash("Day of week is required for weekly closures", "error")
            return schedule_redirect(schedule_id)
        day_of_week_int = int(day_of_week)
        date = None  # Weekly doesn't use date

    user = get_current_user()
    db = get_db()

    try:
        holiday_id = db.add_schedule_holiday(
            schedule_id=schedule_id,
            name=name,
            date=date,
            is_recurring=False,
            created_by=f"session:{user.email}",
            audio_id=int(audio_id) if audio_id else None,
            recurrence=recurrence,
            day_of_week=day_of_week_int,
            start_time=start_time.zfill(5) if start_time else None,
            end_time=end_time.zfill(5) if end_time else None,
            action=action or None,
            forward_to=forward_to or None
        )
        if recurrence == 'weekly':
            days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            detail = f"Weekly on {days[day_of_week_int]}"
            if start_time and end_time:
                detail += f" {start_time}-{end_time}"
        else:
            detail = f"date={date}"

        db.log_activity(
            action="add_closure",
            target=name,
            details=f"Added to schedule {schedule_id} ({detail})",
            performed_by=f"session:{user.email}"
        )
        flash(f"Added closure '{name}'", "success")
    except Exception as e:
        flash(f"Failed to add closure: {e}", "error")

    return schedule_redirect(schedule_id)


@web_bp.route('/admin/schedule/<int:schedule_id>/holiday/<int:holiday_id>/edit', methods=['POST'])
@admin_required
def update_holiday(schedule_id, holiday_id):
    """Update a closure."""
    name = request.form.get('name', '').strip()
    recurrence = request.form.get('recurrence', 'once')
    date = request.form.get('date', '').strip()
    day_of_week = request.form.get('day_of_week', '').strip()
    start_time = request.form.get('start_time', '').strip()
    end_time = request.form.get('end_time', '').strip()
    audio_id = request.form.get('audio_id')
    action = request.form.get('action', '').strip()
    forward_to = request.form.get('forward_to', '').strip()

    if not name:
        flash("Closure name is required", "error")
        return schedule_redirect(schedule_id)

    # Validate based on recurrence type
    if recurrence == 'once':
        if not date:
            flash("Date is required for one-off closures", "error")
            return schedule_redirect(schedule_id)
        day_of_week_int = None
    else:  # weekly
        if not day_of_week:
            flash("Day of week is required for weekly closures", "error")
            return schedule_redirect(schedule_id)
        day_of_week_int = int(day_of_week)
        date = None

    user = get_current_user()
    db = get_db()

    try:
        db.update_schedule_holiday(
            holiday_id=holiday_id,
            data={
                'name': name,
                'date': date,
                'audio_id': int(audio_id) if audio_id else None,
                'recurrence': recurrence,
                'day_of_week': day_of_week_int,
                'start_time': start_time or None,
                'end_time': end_time or None,
                'action': action or None,
                'forward_to': forward_to or None,
            },
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_closure",
            target=name,
            details=f"Updated closure ID {holiday_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Updated closure '{name}'", "success")
    except Exception as e:
        flash(f"Failed to update closure: {e}", "error")

    return schedule_redirect(schedule_id)


@web_bp.route('/admin/schedule/<int:schedule_id>/holiday/<int:holiday_id>/delete', methods=['POST'])
@admin_required
def remove_holiday(schedule_id, holiday_id):
    """Remove a holiday from a schedule."""
    user = get_current_user()
    db = get_db()

    try:
        db.remove_schedule_holiday(holiday_id)
        db.log_activity(
            action="remove_holiday",
            target=str(holiday_id),
            details="Removed holiday",
            performed_by=f"session:{user.email}"
        )
        flash("Holiday removed", "success")
    except Exception as e:
        flash(f"Failed to remove holiday: {e}", "error")

    return schedule_redirect(schedule_id)


# =============================================================================
# Holiday Template Management
# =============================================================================

# Helper for template/schedule redirects - keeps specific item expanded
def template_redirect(template_id=None):
    """Generate redirect URL for template operations."""
    if template_id:
        return redirect(url_for('web.admin_templates') + f'#template_{template_id}')
    return redirect(url_for('web.admin_templates'))


def schedule_redirect(schedule_id=None):
    """Generate redirect URL for schedule operations."""
    if schedule_id:
        return redirect(url_for('web.admin_schedules') + f'#schedule_{schedule_id}')
    return redirect(url_for('web.admin_schedules'))


@web_bp.route('/admin/template/create', methods=['POST'])
@admin_required
def create_template():
    """Create a new holiday template."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    source_url = request.form.get('source_url', '').strip()
    data_as_at = request.form.get('data_as_at', '').strip()

    if not name:
        flash("Template name is required", "error")
        return template_redirect()

    user = get_current_user()
    db = get_db()

    template_id = None
    try:
        template_id = db.create_holiday_template(
            name=name,
            description=description or None,
            source_url=source_url or None,
            data_as_at=data_as_at or None,
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="create_template",
            target=name,
            details=f"Created holiday template ID {template_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Created holiday template '{name}'", "success")
    except Exception as e:
        flash(f"Failed to create template: {e}", "error")

    return template_redirect(template_id)


@web_bp.route('/admin/template/<int:template_id>/update', methods=['POST'])
@admin_required
def update_template(template_id):
    """Update a holiday template."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    source_url = request.form.get('source_url', '').strip()
    data_as_at = request.form.get('data_as_at', '').strip()

    if not name:
        flash("Template name is required", "error")
        return template_redirect(template_id)

    user = get_current_user()
    db = get_db()

    template = db.get_holiday_template(template_id)
    if not template:
        flash("Template not found", "error")
        return template_redirect()

    try:
        db.update_holiday_template(
            template_id=template_id,
            name=name,
            description=description or None,
            source_url=source_url or None,
            data_as_at=data_as_at or None,
            updated_by=f"session:{user.email}"
        )
        db.log_activity(
            action="update_template",
            target=name,
            details=f"Updated holiday template ID {template_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Updated template '{name}'", "success")
    except Exception as e:
        flash(f"Failed to update template: {e}", "error")

    return template_redirect(template_id)


@web_bp.route('/admin/template/<int:template_id>/clone', methods=['POST'])
@admin_required
def clone_template(template_id):
    """Clone a holiday template with all its items."""
    new_name = request.form.get('name', '').strip()

    if not new_name:
        flash("New template name is required", "error")
        return template_redirect(template_id)

    user = get_current_user()
    db = get_db()

    template = db.get_holiday_template(template_id)
    if not template:
        flash("Template not found", "error")
        return template_redirect()

    new_id = None
    try:
        new_id = db.clone_holiday_template(
            template_id=template_id,
            new_name=new_name,
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="clone_template",
            target=new_name,
            details=f"Cloned template '{template['name']}' (ID {template_id}) to new template ID {new_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Created '{new_name}' from '{template['name']}' with {len(template.get('items', []))} holidays", "success")
    except Exception as e:
        flash(f"Failed to clone template: {e}", "error")

    # Redirect to the NEW cloned template
    return template_redirect(new_id if new_id else template_id)


@web_bp.route('/admin/template/<int:template_id>/delete', methods=['POST'])
@admin_required
def delete_template(template_id):
    """Delete a holiday template."""
    user = get_current_user()
    db = get_db()

    template = db.get_holiday_template(template_id)
    if not template:
        flash("Template not found", "error")
        return template_redirect()

    try:
        db.delete_holiday_template(template_id)
        db.log_activity(
            action="delete_template",
            target=template['name'],
            details=f"Deleted holiday template ID {template_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Deleted template '{template['name']}'", "success")
    except Exception as e:
        flash(f"Failed to delete template: {e}", "error")

    # Template deleted, so redirect to section (not specific template)
    return template_redirect()


@web_bp.route('/admin/template/<int:template_id>/item/add', methods=['POST'])
@admin_required
def add_template_item(template_id):
    """Add a holiday item to a template."""
    name = request.form.get('name', '').strip()
    date = request.form.get('date', '').strip()

    if not name or not date:
        flash("Holiday name and date are required", "error")
        return template_redirect(template_id)

    # Validate date format (YYYY-MM-DD)
    if len(date) != 10:
        flash("Invalid date format. Use YYYY-MM-DD.", "error")
        return template_redirect(template_id)

    user = get_current_user()
    db = get_db()

    try:
        item_id = db.add_template_item(
            template_id=template_id,
            name=name,
            date=date,
            is_recurring=False,  # Always use absolute dates now
            created_by=f"session:{user.email}"
        )
        db.log_activity(
            action="add_template_item",
            target=name,
            details=f"Added to template {template_id} (date={date})",
            performed_by=f"session:{user.email}"
        )
        flash(f"Added '{name}' to template", "success")
    except Exception as e:
        flash(f"Failed to add item: {e}", "error")

    return template_redirect(template_id)


@web_bp.route('/admin/template/<int:template_id>/item/<int:item_id>/delete', methods=['POST'])
@admin_required
def remove_template_item(template_id, item_id):
    """Remove a holiday item from a template."""
    user = get_current_user()
    db = get_db()

    try:
        db.remove_template_item(item_id)
        db.log_activity(
            action="remove_template_item",
            target=str(item_id),
            details="Removed template item",
            performed_by=f"session:{user.email}"
        )
        flash("Holiday removed from template", "success")
    except Exception as e:
        flash(f"Failed to remove item: {e}", "error")

    return template_redirect(template_id)


@web_bp.route('/admin/template/<int:template_id>/item/<int:item_id>/edit', methods=['POST'])
@admin_required
def update_template_item(template_id, item_id):
    """Update a holiday item in a template."""
    name = request.form.get('name', '').strip()
    date = request.form.get('date', '').strip()

    if not name or not date:
        flash("Name and date are required", "error")
        return template_redirect(template_id)

    user = get_current_user()
    db = get_db()

    try:
        db.update_template_item(item_id, name=name, date=date, updated_by=f"session:{user.email}")
        db.log_activity(
            action="update_template_item",
            target=name,
            details=f"Updated to date={date}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Updated '{name}'", "success")
    except Exception as e:
        flash(f"Failed to update item: {e}", "error")

    return template_redirect(template_id)


@web_bp.route('/admin/template/<int:template_id>/link-schedule', methods=['POST'])
@admin_required
def link_schedule_to_template(template_id):
    """Link a schedule to a template."""
    schedule_id = request.form.get('schedule_id')
    if not schedule_id:
        flash("Schedule is required", "error")
        return template_redirect(template_id)

    user = get_current_user()
    db = get_db()

    try:
        schedule_id = int(schedule_id)
        created = db.link_template_to_schedule(
            template_id=template_id,
            schedule_id=schedule_id,
            created_by=f"session:{user.email}"
        )
        if created:
            db.log_activity(
                action="link_template_schedule",
                target=str(template_id),
                details=f"Linked template {template_id} to schedule {schedule_id}",
                performed_by=f"session:{user.email}"
            )
            flash("Schedule linked to template", "success")
        else:
            flash("Schedule already linked", "info")
    except Exception as e:
        flash(f"Failed to link schedule: {e}", "error")

    return template_redirect(template_id)


@web_bp.route('/admin/template/<int:template_id>/unlink-schedule/<int:schedule_id>', methods=['POST'])
@admin_required
def unlink_schedule_from_template(template_id, schedule_id):
    """Remove a schedule link from a template."""
    user = get_current_user()
    db = get_db()

    try:
        removed = db.unlink_template_from_schedule(template_id, schedule_id)
        if removed:
            db.log_activity(
                action="unlink_template_schedule",
                target=str(template_id),
                details=f"Unlinked template {template_id} from schedule {schedule_id}",
                performed_by=f"session:{user.email}"
            )
            flash("Schedule unlinked from template", "success")
        else:
            flash("Schedule was not linked", "info")
    except Exception as e:
        flash(f"Failed to unlink schedule: {e}", "error")

    return template_redirect(template_id)


@web_bp.route('/admin/template/<int:template_id>/sync-preview')
@admin_required
def template_sync_preview(template_id):
    """Preview what would happen when syncing a template to schedules."""
    db = get_db()
    preview = db.get_template_sync_preview(template_id)

    if 'error' in preview:
        flash(preview['error'], "error")
        return template_redirect(template_id)

    return render_template('sync_preview.html',
                           preview=preview,
                           current_user=get_current_user())


@web_bp.route('/admin/template/<int:template_id>/apply', methods=['POST'])
@admin_required
def apply_template(template_id):
    """Apply a template to selected schedules."""
    schedule_ids = request.form.getlist('schedule_ids')
    if not schedule_ids:
        flash("No schedules selected", "error")
        return redirect(url_for('web.template_sync_preview', template_id=template_id))

    schedule_ids = [int(sid) for sid in schedule_ids]

    user = get_current_user()
    db = get_db()

    try:
        result = db.apply_template_to_schedules(
            template_id=template_id,
            schedule_ids=schedule_ids,
            created_by=f"session:{user.email}"
        )

        if 'error' in result:
            flash(result['error'], "error")
        else:
            added_count = len(result.get('added', []))
            skipped_count = len(result.get('skipped', []))
            db.log_activity(
                action="apply_template",
                target=str(template_id),
                details=f"Applied to schedules: {added_count} added, {skipped_count} skipped",
                performed_by=f"session:{user.email}"
            )
            flash(f"Applied template: {added_count} holidays added, {skipped_count} already existed", "success")
    except Exception as e:
        flash(f"Failed to apply template: {e}", "error")

    return template_redirect(template_id)


# =============================================================================
# Audio File Management
# =============================================================================

ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'wav', 'ogg', 'm4a'}
AUDIO_FOLDER = config.base_dir / 'audio'


def allowed_audio_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_AUDIO_EXTENSIONS


@web_bp.route('/admin/audio/upload', methods=['POST'])
@admin_required
def upload_audio():
    """Upload an audio file for greetings, hold music, etc."""
    from werkzeug.utils import secure_filename
    import os

    name = request.form.get('name', '').strip()
    file_type = request.form.get('file_type', 'greeting')
    description = request.form.get('description', '').strip()
    tts_text = request.form.get('tts_text', '').strip()

    if not name:
        flash("Audio file name is required", "error")
        return redirect(url_for('web.admin_audio'))

    if 'audio_file' not in request.files:
        flash("No audio file uploaded", "error")
        return redirect(url_for('web.admin_audio'))

    file = request.files['audio_file']
    if file.filename == '':
        flash("No audio file selected", "error")
        return redirect(url_for('web.admin_audio'))

    if not allowed_audio_file(file.filename):
        flash(f"Invalid file type. Allowed: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}", "error")
        return redirect(url_for('web.admin_audio'))

    user = get_current_user()
    db = get_db()

    # Ensure audio folder exists
    AUDIO_FOLDER.mkdir(exist_ok=True)

    # Generate unique filename
    ext = file.filename.rsplit('.', 1)[1].lower()
    safe_name = secure_filename(name.replace(' ', '_').lower())
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{safe_name}_{timestamp}.{ext}"
    file_path = AUDIO_FOLDER / filename

    try:
        # Save the file
        file.save(str(file_path))

        # Try to detect audio duration
        duration_seconds = None
        try:
            import subprocess
            result = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                duration_seconds = round(float(result.stdout.strip()))
        except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
            pass  # ffprobe not available or failed — duration stays None

        # Store just the path - full URL is constructed at runtime
        file_url = f"/audio/{filename}"

        # Create database record
        audio_id = db.create_audio_file(
            data={
                'name': name,
                'description': description or None,
                'file_type': file_type,
                'file_url': file_url,
                'file_path': str(file_path),
                'tts_text': tts_text or None,
                'duration_seconds': duration_seconds,
            },
            created_by=f"session:{user.email}"
        )

        db.log_activity(
            action="upload_audio",
            target=name,
            details=f"Uploaded {file_type} audio: {filename}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Uploaded audio file '{name}'", "success")
    except Exception as e:
        flash(f"Failed to upload audio: {e}", "error")

    return redirect(url_for('web.admin_audio'))


@web_bp.route('/audio/<filename>')
def serve_audio(filename):
    """Serve audio files to Twilio (no auth required for Twilio access)."""
    from flask import send_from_directory
    return send_from_directory(str(AUDIO_FOLDER), filename)


@web_bp.route('/admin/audio/<int:audio_id>/edit', methods=['POST'])
@admin_required
def update_audio(audio_id):
    """Update an audio file's metadata (name, type, description, spoken text)."""
    name = request.form.get('name', '').strip()
    file_type = request.form.get('file_type', 'greeting')
    description = request.form.get('description', '').strip()
    tts_text = request.form.get('tts_text', '').strip()

    if not name:
        flash("Audio file name is required", "error")
        return redirect(url_for('web.admin_audio'))

    user = get_current_user()
    db = get_db()

    audio = db.get_audio_file(audio_id)
    if not audio:
        flash("Audio file not found", "error")
        return redirect(url_for('web.admin_audio'))

    try:
        db.update_audio_file(
            audio_id=audio_id,
            data={
                'name': name,
                'description': description or None,
                'file_type': file_type,
                'tts_text': tts_text or None,
            },
            updated_by=f"session:{user.email}"
        )

        db.log_activity(
            action="update_audio",
            target=name,
            details=f"Updated audio file ID {audio_id} (type={file_type})",
            performed_by=f"session:{user.email}"
        )
        flash(f"Updated audio file '{name}'", "success")
    except Exception as e:
        flash(f"Failed to update audio: {e}", "error")

    return redirect(url_for('web.admin_audio'))


@web_bp.route('/admin/audio/<int:audio_id>/delete', methods=['POST'])
@admin_required
def delete_audio(audio_id):
    """Delete an audio file."""
    import os

    user = get_current_user()
    db = get_db()

    audio = db.get_audio_file(audio_id)
    if not audio:
        flash("Audio file not found", "error")
        return redirect(url_for('web.admin_audio'))

    try:
        # Delete file from disk if it exists
        if audio.get('file_path') and os.path.exists(audio['file_path']):
            os.remove(audio['file_path'])

        # Soft delete in database (set is_active = 0)
        db.deactivate_audio_file(audio_id)

        db.log_activity(
            action="delete_audio",
            target=audio['name'],
            details=f"Deleted audio file ID {audio_id}",
            performed_by=f"session:{user.email}"
        )
        flash(f"Deleted audio file '{audio['name']}'", "success")
    except Exception as e:
        flash(f"Failed to delete audio: {e}", "error")

    return redirect(url_for('web.admin_audio'))


# =============================================================================
# Admin Settings
# =============================================================================

@web_bp.route('/admin/settings')
@admin_required
def admin_settings():
    """Admin settings page - configure bot-wide settings."""
    user = get_current_user()
    db = get_db()

    settings = db.get_bot_settings()
    audio_files = db.get_audio_files()

    return render_template('admin_settings.html',
                           settings=settings,
                           audio_files=audio_files,
                           current_user=user)


@web_bp.route('/admin/settings', methods=['POST'])
@admin_required
def save_admin_settings():
    """Save admin settings."""
    user = get_current_user()
    db = get_db()

    # Save Drive folder ID
    drive_folder_id = request.form.get('drive_recordings_folder_id', '').strip()

    # Allow empty string to clear the setting, but save None
    if drive_folder_id:
        db.set_bot_setting('drive_recordings_folder_id', drive_folder_id, f'session:{user.email}')
        flash('Settings saved. Drive folder configured.', 'success')
    else:
        db.set_bot_setting('drive_recordings_folder_id', '', f'session:{user.email}')
        flash('Settings saved. Drive folder cleared.', 'warning')

    # Clear the cached folder ID in drive_service so it picks up the new value
    from rinq.services.drive_service import drive_service
    drive_service._recordings_folder_id = None

    # Save extension directory number
    ext_dir_number = request.form.get('extension_directory_number', '').strip()
    db.set_bot_setting('extension_directory_number', ext_dir_number, f'session:{user.email}')

    # Save connecting prefix audio path
    connecting_prefix = request.form.get('connecting_prefix_audio_path', '').strip()
    db.set_bot_setting('connecting_prefix_audio_path', connecting_prefix, f'session:{user.email}')

    return redirect(url_for('web.admin_settings'))


# =============================================================================
# Text-to-Speech (TTS) Generation
# =============================================================================

@web_bp.route('/admin/tts')
@admin_required
def admin_tts():
    """Redirect to unified audio page."""
    return redirect(url_for('web.admin_audio'))

def _admin_tts_legacy():
    """Legacy TTS page handler — kept for reference, no longer routed."""
    from rinq.services.tts_service import get_tts_service

    user = get_current_user()
    db = get_db()
    tts = get_tts_service()

    # Load default TTS settings
    settings = db.get_tts_settings()
    default_provider = settings.get('default_provider', 'elevenlabs')
    default_voice = settings.get('default_voice', 'cjVigY5qzO86Huf0OWal')

    # Get voice name for display
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

    # Check for prefill text from query params (for regenerate)
    prefill_text = request.args.get('text', '')
    prefill_type = request.args.get('type', '')

    # Build audio type data for the type picker panel
    audio_files = db.get_audio_files()
    # per_flow types are assigned per call-flow/queue — multiple recordings are normal
    # global types are auto-selected by type — only the first recording is used
    audio_type_defs = [
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
         'description': 'Day snippet for "tomorrow"',
         'default_text': 'tomorrow'},
        {'value': 'reopen_day_later_today', 'label': 'Later Today', 'category': 'Reopen',
         'description': 'Day snippet for "later today"',
         'default_text': 'later today'},
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
         'description': 'Time snippet for 8:30 AM opening',
         'default_text': 'at 8 30 AY EM'},
        {'value': 'reopen_time_0900', 'label': 'at 9:00 AM', 'category': 'Reopen',
         'description': 'Time snippet for 9:00 AM opening',
         'default_text': 'at 9 AY EM'},
    ]

    # Enrich with status, existing recordings, and audio URLs
    for t in audio_type_defs:
        existing = [a for a in audio_files if a['file_type'] == t['value']]
        t['count'] = len(existing)
        t['has_recording'] = bool(existing)
        # For per-flow types, list all recordings; for global types, just the first
        if t.get('per_flow'):
            t['recordings'] = [{'name': a.get('name', ''), 'url': a.get('file_url', '')} for a in existing]
            t['audio_url'] = ''  # No single URL for per-flow types
            t['existing_text'] = t['default_text']  # Always use default for new per-flow recordings
        else:
            t['recordings'] = []
            t['audio_url'] = existing[0].get('file_url', '') if existing else ''
            # Use existing tts_text if available, otherwise default_text
            if existing and existing[0].get('tts_text'):
                t['existing_text'] = existing[0]['tts_text']
            else:
                t['existing_text'] = t['default_text']

    return render_template('admin_tts.html',
                         elevenlabs_available=tts.elevenlabs_available,
                         cartesia_available=tts.cartesia_available,
                         google_available=tts.google_available,
                         elevenlabs_voices=tts.get_elevenlabs_voices_grouped() if tts.elevenlabs_available else {},
                         cartesia_voices=tts.get_cartesia_voices_grouped() if tts.cartesia_available else {},
                         google_voices=tts.get_google_voices_grouped(),
                         default_provider=default_provider,
                         default_voice=default_voice,
                         current_voice_name=current_voice_name,
                         prefill_text=prefill_text,
                         prefill_type=prefill_type,
                         audio_type_defs=audio_type_defs,
                         current_user=user)


@web_bp.route('/admin/tts/settings', methods=['POST'])
@admin_required
def save_tts_settings():
    """Save default TTS voice settings."""
    provider = request.form.get('provider', 'elevenlabs')
    voice = request.form.get('voice', '')

    if not voice:
        flash("Voice is required", "error")
        return redirect(url_for('web.admin_audio'))

    user = get_current_user()
    db = get_db()

    try:
        db.set_tts_setting('default_provider', provider, f"session:{user.email}")
        db.set_tts_setting('default_voice', voice, f"session:{user.email}")

        db.log_activity(
            action="update_tts_settings",
            target="default_voice",
            details=f"Set default TTS to {provider} voice {voice}",
            performed_by=f"session:{user.email}"
        )

        flash("Voice settings saved", "success")
    except Exception as e:
        logger.exception(f"Failed to save TTS settings: {e}")
        flash(f"Failed to save settings: {e}", "error")

    return redirect(url_for('web.admin_audio'))


@web_bp.route('/admin/tts/preview', methods=['POST'])
@admin_required
def preview_tts():
    """Generate TTS audio for preview (returns audio blob)."""
    from flask import Response, jsonify
    from rinq.services.tts_service import get_tts_service

    provider = request.form.get('provider', 'elevenlabs')
    text = request.form.get('text', '').strip()
    voice = request.form.get('voice', '')

    if not text:
        return jsonify({'error': 'Text is required'}), 400

    if not voice:
        return jsonify({'error': 'Voice is required'}), 400

    tts = get_tts_service()

    try:
        if provider == 'elevenlabs':
            if not tts.elevenlabs_available:
                return jsonify({'error': 'ElevenLabs API key not configured'}), 400
            stability = float(request.form.get('stability', 0.5))
            audio_bytes = tts.generate_elevenlabs(text, voice_id=voice, stability=stability)

        elif provider == 'cartesia':
            if not tts.cartesia_available:
                return jsonify({'error': 'Cartesia API key not configured'}), 400
            speed = float(request.form.get('speed', 1.0))
            audio_bytes = tts.generate_cartesia(text, voice_id=voice, speed=speed)

        elif provider == 'google':
            if not tts.google_available:
                return jsonify({'error': 'Google TTS API key not configured'}), 400
            speed = float(request.form.get('speed', 1.0))
            audio_bytes = tts.generate_google(text, voice_name=voice, speaking_rate=speed)

        else:
            return jsonify({'error': f'Unknown provider: {provider}'}), 400

        return Response(audio_bytes, mimetype='audio/mpeg')

    except Exception as e:
        logger.exception(f"TTS preview failed: {e}")
        return jsonify({'error': str(e)}), 500


@web_bp.route('/admin/tts/save', methods=['POST'])
@admin_required
def save_tts_audio():
    """Save TTS audio file (uses uploaded preview audio, not regenerating)."""
    from werkzeug.utils import secure_filename
    from rinq.services.tts_service import get_tts_service

    provider = request.form.get('provider', 'elevenlabs')
    text = request.form.get('text', '').strip()
    voice = request.form.get('voice', '')
    name = request.form.get('name', '').strip()
    file_type = request.form.get('file_type', 'greeting')
    description = request.form.get('description', '').strip()

    if not text:
        flash("Text is required", "error")
        return redirect(url_for('web.admin_audio'))

    if not name:
        flash("Name is required", "error")
        return redirect(url_for('web.admin_audio'))

    if not voice:
        flash("Voice is required", "error")
        return redirect(url_for('web.admin_audio'))

    # Check for uploaded audio data (from preview)
    audio_file = request.files.get('audio_data')
    if not audio_file:
        flash("No audio data - please preview first", "error")
        return redirect(url_for('web.admin_audio'))

    user = get_current_user()
    db = get_db()
    tts = get_tts_service()

    try:
        # Read the uploaded audio bytes
        audio_bytes = audio_file.read()

        # Get voice name for description
        if provider == 'elevenlabs':
            voices = tts.get_elevenlabs_voices()
            voice_name = voices.get(voice, {}).get('name', voice)
            provider_info = f"ElevenLabs {voice_name}"
        elif provider == 'cartesia':
            voices = tts.get_cartesia_voices()
            voice_name = voices.get(voice, {}).get('name', voice)
            provider_info = f"Cartesia {voice_name}"
        else:
            provider_info = f"Google Cloud {voice}"

        # Ensure audio folder exists
        AUDIO_FOLDER.mkdir(exist_ok=True)

        # Generate unique filename
        safe_name = secure_filename(name.replace(' ', '_').lower())
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{safe_name}_{timestamp}.mp3"
        file_path = AUDIO_FOLDER / filename

        # Save the file
        with open(file_path, 'wb') as f:
            f.write(audio_bytes)

        # Store just the path - full URL is constructed at runtime
        file_url = f"/audio/{filename}"

        # Create database record with TTS metadata
        full_description = description
        if full_description:
            full_description += f" [TTS: {provider_info}]"
        else:
            full_description = f"TTS: {provider_info}"

        # Build TTS settings for storage
        tts_settings = {}
        if provider == 'elevenlabs':
            stability = float(request.form.get('stability', 0.5))
            tts_settings['stability'] = stability
        elif provider in ('cartesia', 'google'):
            speed = float(request.form.get('speed', 1.0))
            tts_settings['speed'] = speed

        audio_id = db.create_audio_file(
            data={
                'name': name,
                'description': full_description,
                'file_type': file_type,
                'file_url': file_url,
                'file_path': str(file_path),
                'tts_text': text,
                'tts_provider': provider,
                'tts_voice': voice,
                'tts_settings': json.dumps(tts_settings),
            },
            created_by=f"session:{user.email}"
        )

        db.log_activity(
            action="save_tts_audio",
            target=name,
            details=f"Saved {file_type} audio with {provider_info}",
            performed_by=f"session:{user.email}"
        )

        flash(f"Saved audio file '{name}'", "success")
        return redirect(url_for('web.admin_audio'))

    except Exception as e:
        logger.exception(f"TTS save failed: {e}")
        flash(f"Failed to save audio: {e}", "error")
        return redirect(url_for('web.admin_audio'))
