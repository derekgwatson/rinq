"""Staff API routes — PAM integration, staff sync, staff phones, contacts.

Extracted from routes.py. Registered via register(api_bp) at import time.
"""

import logging

from flask import jsonify, request

from rinq.database.db import get_db
from rinq.tenant.context import get_twilio_config

try:
    from shared.auth.bot_api import api_or_session_auth, get_api_caller, get_api_caller_email
except ImportError:
    from rinq.auth.decorators import api_or_session_auth, get_api_caller, get_api_caller_email

logger = logging.getLogger(__name__)


def register(bp):
    """Register all staff routes on the given blueprint."""

    # =========================================================================
    # PAM Integration
    # =========================================================================

    @bp.route('/pam/directory-overrides')
    @api_or_session_auth
    def get_pam_directory_overrides():
        """Get directory overrides for PAM.

        GET /api/pam/directory-overrides

        Returns the extension directory phone number and a list of staff who are
        active on Rinq. PAM uses this to replace dead old VoIP numbers with the
        extension directory number + Rinq extension.

        A staff member is considered "on Rinq" if staff_extensions.is_active = 1,
        which is controlled from /admin/staff (auto-activated by usage signals,
        or manually set by an admin).

        Returns:
            {
                "extension_directory_number": "0261926833",
                "tina_staff": {
                    "cid.cortes@watsonblinds.com.au": {"extension": "1234"},
                    "jane.doe@watsonblinds.com.au": {"extension": "1235"},
                    ...
                }
            }
        """
        db = get_db()

        # Find the extension directory phone number:
        # It's a phone number whose call flow has open_action = 'extension_directory'
        ext_dir_number = None
        phone_numbers = db.get_phone_numbers()
        call_flows = db.get_call_flows()
        ext_dir_flow_ids = {cf['id'] for cf in call_flows if cf.get('open_action') == 'extension_directory'}

        from rinq.services.phone import to_local
        for pn in phone_numbers:
            if pn.get('call_flow_id') in ext_dir_flow_ids:
                ext_dir_number = to_local(pn['phone_number'])
                break

        # Get staff who are marked active in Rinq (staff_extensions.is_active)
        # This is the flag controlled from /admin/staff
        active_extensions = db.get_active_staff_extensions()
        tina_staff = {}
        for ext in active_extensions:
            email = ext['email'].lower()
            tina_staff[email] = {
                'extension': ext['extension'],
            }

        # All extensions (including inactive) - PAM uses this to show the
        # extension directory number for staff who have no fixed line
        all_extensions = {}
        for ext in db.get_all_staff_extensions():
            email = ext['email'].lower()
            all_extensions[email] = {
                'extension': ext['extension'],
            }

        return jsonify({
            'extension_directory_number': ext_dir_number,
            'tina_staff': tina_staff,
            'all_extensions': all_extensions,
        })

    # =========================================================================
    # Staff Sync
    # =========================================================================

    @bp.route('/staff/sync', methods=['POST'])
    @api_or_session_auth
    def sync_staff_extensions():
        """Sync staff from Peter - create extensions for anyone who doesn't have one.

        POST /api/staff/sync

        Fetches all active staff from Peter and ensures each has a Rinq
        staff_extensions record with an auto-assigned extension number.

        Returns:
            {"created": 5, "existing": 42, "total": 47, "details": [...]}
        """
        from rinq.integrations import get_staff_directory

        caller = get_api_caller()
        db = get_db()

        # Fetch active staff from staff directory
        staff_dir = get_staff_directory()
        peter_staff = staff_dir.get_active_staff() if staff_dir else []
        if not peter_staff:
            return jsonify({'error': 'Could not fetch staff from directory'}), 502

        created = 0
        existing = 0
        details = []

        for staff in peter_staff:
            email = (staff.get('google_primary_email') or staff.get('work_email') or '').lower().strip()
            if not email:
                continue

            ext = db.get_staff_extension(email)
            if ext:
                existing += 1
            else:
                peter_ext = staff.get('extension', '').strip() or None
                ext = db.create_staff_extension(email, caller or 'system:sync', extension=peter_ext)
                created += 1
                details.append({'email': email, 'extension': ext['extension']})

        db.log_activity(
            action="staff_sync",
            target="staff_extensions",
            details=f"Synced from Peter: {created} created, {existing} existing",
            performed_by=caller or 'system:sync'
        )

        return jsonify({
            'created': created,
            'existing': existing,
            'total': created + existing,
            'details': details,
        })

    @bp.route('/staff/import-hierarchy', methods=['POST'])
    @api_or_session_auth
    def import_staff_hierarchy():
        """One-off import of reporting hierarchy from Peter (Watson bot-team).

        POST /api/staff/import-hierarchy

        Fetches each active staff member's reportees from Peter and populates
        the reports_to field in staff_extensions. Only updates staff who don't
        already have a reports_to set (unless ?force=true).

        Returns:
            {"updated": 5, "skipped": 3, "details": [...]}
        """
        from rinq.integrations.watson.staff import WatsonStaffDirectory

        caller = get_api_caller()
        db = get_db()
        force = request.args.get('force', '').lower() == 'true'

        peter = WatsonStaffDirectory()
        peter_staff = peter.get_active_staff()
        if not peter_staff:
            return jsonify({'error': 'Could not fetch staff from Peter'}), 502

        # Build manager→reportees mapping from Peter
        # For each staff member, ask Peter for their direct reports
        reports_to_map = {}  # email -> manager_email
        for staff in peter_staff:
            email = (staff.get('google_primary_email') or staff.get('work_email') or '').lower().strip()
            if not email:
                continue
            direct_reports = peter.get_reportees(email, recursive=False)
            for report in direct_reports:
                report_email = (report.get('google_primary_email') or report.get('work_email') or report.get('email', '')).lower().strip()
                if report_email:
                    reports_to_map[report_email] = email

        updated = 0
        skipped = 0
        details = []

        for email, manager_email in reports_to_map.items():
            ext = db.get_staff_extension(email)
            if not ext:
                skipped += 1
                continue
            if ext.get('reports_to') and not force:
                skipped += 1
                continue
            db.update_staff_reports_to(email, manager_email, caller or 'system:peter-import')
            updated += 1
            details.append({'email': email, 'reports_to': manager_email})

        db.log_activity(
            action="hierarchy_import",
            target="staff_extensions",
            details=f"Imported from Peter: {updated} updated, {skipped} skipped",
            performed_by=caller or 'system:peter-import'
        )

        return jsonify({
            'updated': updated,
            'skipped': skipped,
            'details': details,
        })

    # =========================================================================
    # Staff Phones (for PAM integration)
    # =========================================================================

    @bp.route('/staff-phones')
    @api_or_session_auth
    def get_staff_phones():
        """Get staff phone extensions for PAM directory.

        Returns only extensions where show_in_pam is enabled.

        Response:
            {
                "extensions": [
                    {"email": "user@example.com", "extension": "101"},
                    ...
                ]
            }
        """
        db = get_db()
        extensions = db.get_visible_staff_extensions()

        return jsonify({
            "extensions": [
                {
                    "email": ext['email'],
                    "extension": ext['extension'],
                }
                for ext in extensions
            ]
        })

    @bp.route('/staff-phones/active')
    @api_or_session_auth
    def get_active_staff_phones():
        """Get staff who are actively using Rinq.

        For Peter integration — only returns users an admin has activated.

        Response:
            {
                "extensions": [
                    {"email": "...", "extension": "1234", "forward_to": "...", "forward_mode": "..."},
                    ...
                ]
            }
        """
        db = get_db()
        extensions = db.get_active_staff_extensions()

        return jsonify({
            "extensions": [
                {
                    "email": ext['email'],
                    "extension": ext['extension'],
                    "forward_to": ext.get('forward_to'),
                    "forward_mode": ext.get('forward_mode'),
                }
                for ext in extensions
            ]
        })

    @bp.route('/staff-phones/<email>')
    @api_or_session_auth
    def get_staff_phone(email):
        """Get a specific staff member's extension.

        Returns extension info regardless of show_in_pam setting.
        """
        db = get_db()
        ext = db.get_staff_extension(email)

        if not ext:
            return jsonify({"error": "Extension not found"}), 404

        return jsonify({
            "email": ext['email'],
            "extension": ext['extension'],
            "show_in_pam": bool(ext['show_in_pam']),
            "is_active": bool(ext.get('is_active')),
            "forward_to": ext['forward_to'],
            "forward_mode": ext['forward_mode'],
        })

    @bp.route('/staff-phones/resolved')
    @api_or_session_auth
    def get_resolved_staff_phones():
        """Get resolved external phone numbers for all staff.

        GET /api/staff-phones/resolved

        Returns the extension directory number as each staff member's external
        number. To reach a specific person, callers dial this number then
        enter the extension.

        Phone assignments (store numbers, queue lines) are operational — they
        route calls to queues/teams, not to individuals. If true personal DIDs
        are needed in future, add a did_number field to staff_extensions.

        This is the source of truth for external phone numbers. Peter pulls
        from this endpoint to display phone info without owning it.

        Returns:
            {
                "staff": {
                    "user@example.com": {
                        "extension": "1042",
                        "external_number": "0261920000"
                    }
                }
            }
        """
        from rinq.services.phone import to_local
        db = get_db()

        # Find extension directory phone number
        ext_dir_number = None
        phone_numbers = db.get_phone_numbers()
        call_flows = db.get_call_flows()
        ext_dir_flow_ids = {cf['id'] for cf in call_flows if cf.get('open_action') == 'extension_directory'}

        for pn in phone_numbers:
            if pn.get('call_flow_id') in ext_dir_flow_ids:
                ext_dir_number = to_local(pn['phone_number'])
                break

        # Resolve each staff extension — everyone gets the extension directory
        # number as their external number (callers dial it + enter extension)
        all_extensions = db.get_all_staff_extensions()
        staff = {}
        for ext in all_extensions:
            email = ext['email'].lower()
            extension = ext['extension']

            staff[email] = {
                'extension': extension,
                'external_number': ext_dir_number if extension else None,
            }

        return jsonify({'staff': staff})

    # =========================================================================
    # Contacts / Address Book
    # =========================================================================

    @bp.route('/contacts', methods=['GET'])
    @api_or_session_auth
    def get_contacts():
        """Get staff contacts for the address book.

        GET /api/contacts?q=search+term

        Merges Peter staff directory with Rinq extensions/assignments.

        Query params:
            q: Optional search term (filters by name)

        Returns:
            {
                "contacts": [
                    {
                        "name": "John Smith",
                        "email": "john.smith@watsonblinds.com.au",
                        "position": "Sales Consultant",
                        "section": "Canberra",
                        "extension": "123",
                        "phone": "+61412345678",
                        "has_browser": true,
                        "has_sip": true
                    },
                    ...
                ]
            }
        """
        from rinq.integrations import get_staff_directory

        search = request.args.get('q', '').strip().lower()
        db = get_db()

        # Fetch staff from external directory (Peter) if available
        staff_dir = get_staff_directory()
        peter_staff = staff_dir.get_active_staff() if staff_dir else []

        # Get local extensions and ring settings
        extensions = {ext['email']: ext for ext in db.get_all_staff_extensions()}
        assignments = {}
        for assignment in db.get_assignments():
            email = assignment.get('staff_email')
            if email and email not in assignments:
                # The assignments query joins phone_numbers, so phone_number is included
                phone_num = assignment.get('phone_number')
                if phone_num:
                    assignments[email] = phone_num

        # Build contacts list
        contacts = []

        if peter_staff:
            # External staff directory available — merge with local data
            for staff in peter_staff:
                name = staff.get('name', '')
                # Peter uses google_primary_email, local uses email
                email = (staff.get('google_primary_email') or staff.get('work_email') or staff.get('email') or '').lower()

                if not email:
                    continue

                # Apply search filter
                if search:
                    searchable = f"{name} {email} {staff.get('section', '')} {staff.get('position', '')}".lower()
                    if search not in searchable:
                        continue

                ext = extensions.get(email, {})
                ring_settings = db.get_user_ring_settings(email) if ext else {}

                hide_mobile = bool(ext.get('hide_mobile'))
                mobile = '' if hide_mobile else staff.get('phone_mobile', '')

                # Phone: assignment > mobile > fixed line > extension
                phone = (assignments.get(email, '')
                         or mobile
                         or staff.get('phone_fixed', '')
                         or ext.get('extension', ''))

                contacts.append({
                    'name': name,
                    'email': email,
                    'position': staff.get('position', ''),
                    'section': staff.get('section', ''),
                    'extension': ext.get('extension', ''),
                    'phone': phone,
                    'has_browser': ring_settings.get('ring_browser', False),
                    'has_sip': ring_settings.get('ring_sip', False),
                    'is_active_in_tina': ext.get('is_active', False),
                    'dnd': bool(ext.get('dnd_enabled')),
                })
        else:
            # No external directory — build contacts from local staff extensions + users
            users_by_email = {}
            for user in db.get_users():
                email = (user.get('staff_email') or '').lower()
                if email:
                    users_by_email[email] = user

            for email, ext in extensions.items():
                user = users_by_email.get(email, {})
                name = user.get('friendly_name') or email.split('@')[0].replace('.', ' ').title()

                # Apply search filter
                if search:
                    searchable = f"{name} {email} {ext.get('extension', '')}".lower()
                    if search not in searchable:
                        continue

                ring_settings = db.get_user_ring_settings(email)
                forward_to = '' if ext.get('hide_mobile') else (ext.get('forward_to') or '')
                phone = assignments.get(email, '') or forward_to or ext.get('extension', '')

                contacts.append({
                    'name': name,
                    'email': email,
                    'position': '',
                    'section': '',
                    'extension': ext.get('extension', ''),
                    'phone': phone,
                    'has_browser': ring_settings.get('ring_browser', False),
                    'has_sip': ring_settings.get('ring_sip', False),
                    'is_active_in_tina': ext.get('is_active', False),
                    'dnd': bool(ext.get('dnd_enabled')),
                })

        contacts.sort(key=lambda c: c['name'].lower())

        # Merge address book entries (tagged so UI can distinguish them).
        # Skip entries whose email matches a staff member with hide_mobile set —
        # the directory should respect that flag across both sources.
        hidden_mobile_emails = {
            (ext.get('email') or '').lower()
            for ext in extensions.values()
            if ext.get('hide_mobile')
        }
        try:
            ab_entries = db.get_address_book()
            for entry in ab_entries:
                entry_email = (entry.get('email') or '').lower()
                if entry_email and entry_email in hidden_mobile_emails:
                    continue
                contacts.append({
                    'name': entry['name'],
                    'email': None,
                    'position': entry.get('position', ''),
                    'section': entry.get('section', ''),
                    'phone': entry.get('display_mobile', ''),
                    'mobile': entry.get('display_mobile', ''),
                    'extension': '',
                    'has_browser': False,
                    'has_sip': False,
                    'is_active_in_tina': False,
                    'dnd': False,
                    'source': entry.get('source', 'manual'),
                })
        except Exception as e:
            logger.warning(f"Could not merge address book into contacts: {e}")

        contacts.sort(key=lambda c: c['name'].lower())

        return jsonify({"contacts": contacts})

    # =========================================================================
    # Address Book
    # =========================================================================

    @bp.route('/address-book', methods=['GET'])
    @api_or_session_auth
    def get_address_book():
        """List all address book entries.

        GET /api/address-book?q=search+term
        """
        db = get_db()
        q = request.args.get('q', '').strip()
        entries = db.get_address_book(search=q or None)
        return jsonify({'entries': entries})

    @bp.route('/address-book', methods=['POST'])
    @api_or_session_auth
    def create_address_book_entry():
        """Add a manual address book entry.

        POST /api/address-book
        Body: {name, mobile, section?, position?}
        """
        from rinq.services.address_book_sync import _normalise_mobile
        db = get_db()
        data = request.get_json(force=True) or {}
        name = (data.get('name') or '').strip()
        mobile = (data.get('mobile') or '').strip()
        if not name or not mobile:
            return jsonify({'error': 'name and mobile are required'}), 400
        mobile_e164 = _normalise_mobile(mobile)
        if not mobile_e164:
            return jsonify({'error': f'Unrecognised mobile number: {mobile}'}), 400
        entry_id = db.upsert_address_book_entry(
            name=name,
            display_mobile=mobile,
            mobile_e164=mobile_e164,
            section=(data.get('section') or '').strip() or None,
            position=(data.get('position') or '').strip() or None,
            source='manual',
            external_id=None,
        )
        return jsonify({'id': entry_id}), 201

    @bp.route('/address-book/<int:entry_id>', methods=['DELETE'])
    @api_or_session_auth
    def delete_address_book_entry(entry_id):
        """Delete an address book entry by id."""
        db = get_db()
        deleted = db.delete_address_book_entry(entry_id)
        if not deleted:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'ok': True})

    @bp.route('/address-book/sync', methods=['POST'])
    @api_or_session_auth
    def sync_address_book():
        """Trigger an address book sync from the configured source.

        When called from a session (admin user), syncs the current tenant only.
        When called via unix socket (cron, no session), iterates all tenants.

        No-ops gracefully for tenants that have no sync source configured.
        """
        from rinq.services.address_book_sync import sync_address_book as _sync
        from rinq.services.address_book_sync import PeterAddressBookSource
        from rinq.integrations.watson.staff import WatsonStaffDirectory

        def _sync_tenant(tenant_db):
            try:
                source = PeterAddressBookSource(WatsonStaffDirectory())
                return _sync(tenant_db, source=source)
            except Exception as e:
                logger.warning(f"Address book sync not available: {e}")
                return None

        import os
        from rinq.config import config as rinq_config
        from flask import session

        totals = {'added': 0, 'updated': 0, 'removed': 0}

        if session.get('user_id'):
            # Session request — sync current tenant only
            tenant_db = get_db()
            result = _sync_tenant(tenant_db)
            if result:
                added, updated, removed = result
                totals = {'added': added, 'updated': updated, 'removed': removed}
        else:
            # Cron / unix-socket — iterate all tenants
            from rinq.database.master import get_master_db
            from rinq.database.db import Database
            master_db = get_master_db()
            for tenant in master_db.get_tenants():
                db_dir = os.path.join(rinq_config.tenants_dir, tenant['id'])
                if not os.path.exists(db_dir):
                    continue
                tenant_db = Database(db_path=os.path.join(db_dir, 'rinq.db'))
                result = _sync_tenant(tenant_db)
                if result:
                    added, updated, removed = result
                    totals['added'] += added
                    totals['updated'] += updated
                    totals['removed'] += removed

        return jsonify({'ok': True, **totals})
