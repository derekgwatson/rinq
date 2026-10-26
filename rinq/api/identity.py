"""Identity helpers — email/browser/SIP identity conversion.

Extracted from routes.py. Handles the various formats that Twilio and
the browser softphone use to identify call participants.
"""

import logging

from rinq.database.db import get_db

logger = logging.getLogger(__name__)


def email_to_browser_identity(email: str) -> str:
    """Convert email to browser identity format (for Twilio Client)."""
    return email.replace('@', '_at_').replace('.', '_')


def browser_identity_to_email(identity: str) -> str:
    """Convert browser identity format back to email.

    Format: client:user_at_domain_com -> user@domain.com
    """
    if identity.startswith('client:'):
        identity = identity[7:]
    return identity.replace('_at_', '@').replace('_', '.')


def normalize_staff_identifier(identifier: str) -> tuple[str | None, str | None]:
    """Normalize various staff identifier formats to (email, friendly_name).

    Handles:
    - client:user_at_domain_com -> user@domain.com
    - session:user@domain.com -> user@domain.com
    - user@domain.com -> user@domain.com
    - SIP URIs like sip:user@domain.sip.twilio.com -> attempts to match
    - Phone numbers -> returns (None, None)

    Returns:
        Tuple of (email or None, friendly_name or None)
    """
    if not identifier:
        return None, None

    # Browser client identity: client:user_at_domain_com
    if identifier.startswith('client:'):
        email = browser_identity_to_email(identifier)
        name = email.split('@')[0].replace('_', ' ').replace('.', ' ').title() if '@' in email else None
        return email, name

    # Session format: session:user@domain.com
    if identifier.startswith('session:'):
        email = identifier[8:]
        name = email.split('@')[0].replace('_', ' ').replace('.', ' ').title() if '@' in email else None
        return email, name

    # Already an email
    if '@' in identifier and not identifier.startswith('sip:'):
        name = identifier.split('@')[0].replace('_', ' ').replace('.', ' ').title()
        return identifier, name

    # SIP URI: sip:user@domain.sip.twilio.com
    if identifier.startswith('sip:'):
        sip_part = identifier[4:]
        if '@' in sip_part:
            username = sip_part.split('@')[0]
            try:
                db = get_db()
                # The one answer for "who is this SIP login right now" — a shared
                # desk phone is whoever is logged in to it (migration 083). The
                # name-pattern guess below predates shared phones and never
                # matches them, so a call answered on one was credited to the
                # handset ("Sip:Desk_Yealink_9B571C") instead of the person.
                staff_email = db.get_staff_email_for_sip_username(username)
                if staff_email:
                    name = staff_email.split('@')[0].replace('_', ' ').replace('.', ' ').title()
                    return staff_email, name
                users = db.get_users()
                for user in users:
                    staff_email = user.get('staff_email', '')
                    if staff_email:
                        expected_username = staff_email.split('@')[0].replace('.', '_').lower()
                        if username.lower() == expected_username:
                            name = staff_email.split('@')[0].replace('_', ' ').replace('.', ' ').title()
                            return staff_email, name
            except Exception as e:
                logger.debug(f"SIP identifier lookup failed for {identifier}: {e}")
        return None, None

    return None, None
