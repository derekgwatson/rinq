"""Phone number utilities — normalisation, formatting, validation.

Extracted from scattered inline code across routes.py and twilio_service.py.
All phone helpers live here so there's one place to maintain AU-specific logic.
"""

import re


def ensure_plus(number: str) -> str:
    """Ensure a phone number has a + prefix.

    Twilio URL-decodes + as space, so numbers arriving via query params
    often lose their prefix. This restores it.
    """
    if number and not number.startswith('+'):
        return '+' + number
    return number


def to_e164(number: str) -> str:
    """Format a phone number to E.164 (Australian rules).

    Handles local (04xx, 02xxxx), national (61...), SIP URIs, 1300/1800,
    and already-formatted numbers.
    """
    if not number:
        return number

    # Handle SIP URI format: sip:62804443@domain.com;transport=UDP
    if number.startswith('sip:'):
        number = number[4:]
        if '@' in number:
            number = number.split('@')[0]

    digits = ''.join(c for c in number if c.isdigit())

    if number.startswith('+'):
        return number

    # Already E.164 without +
    if digits.startswith('61') and len(digits) == 11:
        return f'+{digits}'

    # Australian mobile (04xx) or landline with area code (0x) — 10 digits
    if digits.startswith('0') and len(digits) == 10:
        return f'+61{digits[1:]}'

    # 1300/1800 numbers (10 digits) and 13xx short numbers (6 digits)
    if digits.startswith('1300') or digits.startswith('1800'):
        return f'+61{digits}'
    if digits.startswith('13') and len(digits) == 6:
        return f'+61{digits}'

    # 9-digit national format without leading 0
    if len(digits) == 9:
        return f'+61{digits}'

    # 8-digit local number — assume Canberra (02)
    if len(digits) == 8:
        return f'+612{digits}'

    return number


def to_local(number: str) -> str:
    """Convert +61... to local Australian format (0...)."""
    if number and number.startswith('+61'):
        return '0' + number[3:]
    return number


def is_lookupable_number(number: str) -> bool:
    """True if a string is a real phone number worth a CRM/identity lookup.

    Withheld caller ID arrives as 'anonymous'/'restricted'/'unavailable' or an
    empty string; spoofed or malformed numbers are too short to identify anyone.
    Feeding any of these into a fuzzy substring search returns false matches
    (e.g. 'anonymous' substring-hits a customer whose email is
    'anonymousdane117@gmail.com', and an empty term matches every customer), so
    callers should skip enrichment for them and treat the caller as anonymous.
    """
    if not number:
        return False
    digits = ''.join(c for c in number if c.isdigit())
    # AU local subscriber numbers are 8 digits; anything shorter can't identify.
    return len(digits) >= 8


def normalize_au_mobile(number: str) -> str | None:
    """Normalize an Australian mobile to +614 format, or return None."""
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


def is_valid_au_mobile(number: str) -> bool:
    """Check if a string is a valid +614 Australian mobile."""
    return bool(number and re.match(r'^\+614\d{8}$', number))


def format_for_speech(phone_number: str) -> str:
    """Format a phone number for TTS — reads each digit separately.

    Converts +61412345678 to "6 1 4 1 2 3 4 5 6 7 8" so Twilio
    reads individual digits instead of "61 billion".
    """
    digits_only = ''.join(c for c in phone_number if c.isdigit())
    return ' '.join(digits_only)
