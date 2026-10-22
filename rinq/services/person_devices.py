"""Which devices ring when Tina rings a person.

One answer for every path that rings a staff member — queue calls, direct
calls to their number or extension, and transfers to them — so the paths
can't drift apart. They did: transfers to an extension rang only the browser,
so someone working from a desk phone or their mobile never heard them.

A person can have up to three devices:

- browser: the Tina softphone (unless they turned browser ringing off)
- sip:     a desk phone or SIP softphone (if they have SIP credentials and
           haven't turned SIP ringing off)
- mobile:  their call-forward mobile (staff_extensions.forward_to)

DND rings nothing. A mobile leg must always be screened ("press 1 to
answer") by the caller, because a mobile that is off or declines goes to its
own voicemail, which the network reports exactly like a person answering.
See mobile_screen_url below.

Pass db and sip_domain in from request context — this runs in background
threads too (gotcha 2).
"""

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class RingTarget:
    kind: str        # 'browser' | 'sip' | 'mobile'
    to: str          # 'client:<identity>' | 'sip:<user>@<domain>' | '+61...'
    email: str


def browser_identity(email: str) -> str:
    return email.replace('@', '_at_').replace('.', '_')


def sip_uri_for_person(email: str, db, sip_domain: str) -> str | None:
    """The desk phone that rings for this person.

    A shared desk phone they are logged in to wins over their own SIP login:
    they are sitting at it, and logging in somewhere means "ring me here",
    not "ring me here as well".
    """
    username = db.get_desk_phone_for_staff(email)
    if not username:
        user = db.get_user_by_email(email)
        username = user.get('username') if user else None
    return f"sip:{username}@{sip_domain}" if username else None


def person_ring_targets(email: str, db, sip_domain: str | None) -> list[RingTarget]:
    """Every device that should ring for this person, in browser/sip/mobile order."""
    if not email:
        return []
    settings = db.get_user_ring_settings(email)
    if settings.get('dnd'):
        return []

    targets = []
    if settings.get('ring_browser', True):
        targets.append(RingTarget('browser', f"client:{browser_identity(email)}", email))

    if settings.get('ring_sip', True) and sip_domain:
        uri = sip_uri_for_person(email, db, sip_domain)
        if uri:
            targets.append(RingTarget('sip', uri, email))

    ext = db.get_staff_extension(email)
    if ext and ext.get('forward_to'):
        targets.append(RingTarget('mobile', ext['forward_to'], email))

    return targets


def mobile_screen_url(base_url: str, next_path: str, label: str) -> str:
    """URL for a mobile leg: "press 1 to answer", then on 1 redirect to next_path.

    next_path must be one of our own /api/voice/ webhooks — the screen
    refuses anything else (_safe_screen_next in api/routes.py).
    """
    return (
        f"{base_url}/api/voice/mobile-screen"
        f"?next={quote(next_path, safe='')}&label={quote(label, safe='')}"
    )
