"""Caller ID resolution service for Rinq.

Resolves the outbound caller ID for a user based on priority:
1. Manual default (staff_extensions.default_caller_id)
2. Direct assignment (phone_assignments with can_make)
3. Section-based (staff directory section → phone number)
4. System default (tenant twilio_default_caller_id)
"""

import logging

from rinq.database.db import get_db
from rinq.tenant.context import get_twilio_config

logger = logging.getLogger(__name__)


def resolve_caller_id(email: str, db=None) -> dict:
    """Resolve the outbound caller ID for a user.

    Returns dict with:
        caller_id: The E.164 phone number to use (or None)
        source: 'manual' | 'assigned' | 'section' | 'default' | None
        display: Friendly display name (with section if applicable)
    """
    if not db:
        db = get_db()

    caller_id = None
    source = None

    # Priority 1: Manual default from staff_extensions
    staff_ext = db.get_staff_extension(email)
    if staff_ext and staff_ext.get('default_caller_id'):
        caller_id = staff_ext['default_caller_id']
        source = 'manual'

    # Priority 2: Direct assignment (can_make)
    if not caller_id:
        assignments = db.get_assignments_for_user(email)
        if assignments:
            phone_numbers = db.get_phone_numbers()
            phone_by_sid = {pn['sid']: pn for pn in phone_numbers}
            for assignment in assignments:
                if assignment.get('can_make'):
                    pn = phone_by_sid.get(assignment['phone_number_sid'])
                    if pn:
                        caller_id = pn['phone_number']
                        source = 'assigned'
                        break

    # Priority 3: Section-based
    if not caller_id:
        try:
            from rinq.integrations import get_staff_directory
            staff_dir = get_staff_directory()
            if staff_dir:
                staff_data = staff_dir.get_staff_by_email(email)
                if staff_data:
                    user_section = staff_data.get('section')
                    if user_section:
                        phone_numbers = db.get_phone_numbers()
                        for number in phone_numbers:
                            if number.get('section') == user_section:
                                caller_id = number['phone_number']
                                source = 'section'
                                break
        except Exception:
            pass

    # Priority 4: System default
    if not caller_id:
        tenant_default = get_twilio_config('twilio_default_caller_id')
        if tenant_default:
            caller_id = tenant_default
            source = 'default'
        else:
            phone_numbers = db.get_phone_numbers()
            if phone_numbers:
                caller_id = phone_numbers[0]['phone_number']
                source = 'default'

    # Build display name
    display = caller_id
    if caller_id:
        phone_numbers = db.get_phone_numbers()
        for number in phone_numbers:
            if number['phone_number'] == caller_id:
                display = number.get('friendly_name') or caller_id
                if number.get('section'):
                    display += f" ({number['section']})"
                break
        else:
            # Check verified caller IDs
            verified = db.get_verified_caller_ids(active_only=True)
            for vcid in verified:
                if vcid['phone_number'] == caller_id:
                    display = vcid.get('friendly_name') or caller_id
                    break

    return {
        'caller_id': caller_id,
        'source': source,
        'display': display,
    }
