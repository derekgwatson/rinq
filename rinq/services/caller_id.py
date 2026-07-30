"""Caller ID resolution service for Rinq.

Resolves the outbound caller ID for a user based on priority:
1. Manual default (staff_extensions.default_caller_id)
2. Direct assignment (phone_assignments with can_make)
3. Section-based (staff directory section → phone number)
4. System default (tenant twilio_default_caller_id)

If none of those resolve, caller_id is None and the caller must refuse the call.
There is no "use whichever number happens to be first" fallback — that made the
number a user presented to customers a side effect of alphabetical sort order.
"""

import logging

from rinq.database.db import get_db
from rinq.tenant.context import get_twilio_config

logger = logging.getLogger(__name__)


def _digits(value: str) -> str:
    """Strip everything but digits, so '+61269316440' == '61269316440'."""
    return ''.join(c for c in (value or '') if c.isdigit())


def resolve_caller_id(email: str, db=None) -> dict:
    """Resolve the outbound caller ID for a user.

    Returns dict with:
        caller_id: The E.164 phone number to use, or None if nothing resolves.
                   Callers MUST handle None by refusing the call and telling the
                   user why — never by substituting an arbitrary number.
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
    #
    # get_staff_directory() can be a remote call, and this resolver now runs on
    # the live outbound call path. Check we actually have section-tagged numbers
    # to match against BEFORE reaching out — with no sections configured this
    # step can never resolve, so the round-trip would be pure added latency on
    # every call.
    if not caller_id:
        try:
            sectioned = [n for n in db.get_phone_numbers() if n.get('section')]
            if not sectioned:
                logger.debug(
                    f"Skipping section-based caller ID for {email}: no phone "
                    f"numbers have a section set."
                )
            else:
                from rinq.integrations import get_staff_directory
                staff_dir = get_staff_directory()
                if staff_dir:
                    staff_data = staff_dir.get_staff_by_email(email)
                    if staff_data:
                        user_section = staff_data.get('section')
                        if user_section:
                            for number in sectioned:
                                if number.get('section') == user_section:
                                    caller_id = number['phone_number']
                                    source = 'section'
                                    break
        except Exception as e:
            logger.warning(f"Section-based caller ID resolution failed for {email} "
                           f"— falling through to default: {e}")

    # Priority 4: Explicit tenant default
    #
    # There is deliberately NO "just use the first number we own" fallback here.
    # That used to pick phone_numbers[0], and get_phone_numbers() is
    # ORDER BY friendly_name over bare digits — so the number a user presented
    # to customers was decided by string sort order, and changed silently
    # whenever a number was added, removed or renamed. Callers must handle
    # caller_id=None and refuse the call rather than dial from a number chosen
    # by chance.
    if not caller_id:
        tenant_default = get_twilio_config('twilio_default_caller_id')
        if tenant_default:
            caller_id = tenant_default
            source = 'default'

    if not caller_id:
        logger.error(
            f"No outbound caller ID resolves for {email}: no manual default, no "
            f"can_make assignment, no section match, and the tenant has no "
            f"twilio_default_caller_id set. Outbound calls will be refused until "
            f"one is set (Admin -> Caller ID overview)."
        )

    # Build display name.
    #
    # phone_numbers.friendly_name is often just the number's own digits (Twilio
    # seeds it that way), which tells the user nothing — they'd see "Calling as
    # 61269316440" instead of "Calling as Wagga Wagga". Treat a name that is
    # merely the digits as no name at all and prefer the verified_caller_ids
    # label, which is where the office names actually live.
    display = caller_id
    if caller_id:
        wanted_digits = _digits(caller_id)

        def _label(row):
            name = (row.get('friendly_name') or '').strip()
            return name if name and _digits(name) != wanted_digits else None

        owned = next((n for n in db.get_phone_numbers()
                      if n['phone_number'] == caller_id), None)
        verified = next((v for v in db.get_verified_caller_ids(active_only=True)
                         if v['phone_number'] == caller_id), None)

        display = (_label(owned) if owned else None) \
            or (_label(verified) if verified else None) \
            or caller_id

        if owned and owned.get('section'):
            display += f" ({owned['section']})"

    return {
        'caller_id': caller_id,
        'source': source,
        'display': display,
    }
