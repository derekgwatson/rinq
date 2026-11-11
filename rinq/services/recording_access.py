"""Whose call recordings a signed-in user may see and play.

One rule for the Recordings page and the recordings API:
- admins, managers and users given recordings access see everyone's;
- everyone else sees their own plus their reportees' (reports_to, recursive).
"""

import logging

logger = logging.getLogger(__name__)


def visible_staff_emails(user, mine_only: bool = False) -> list[str] | None:
    """Staff emails whose recordings `user` may see, or None for all of them.

    `mine_only` narrows the list to the user's own recordings (the page's
    "Mine" filter).
    """
    if mine_only:
        return [user.email]
    if user.can_view_all_recordings:
        return None

    from rinq.integrations import get_staff_directory

    emails = [user.email]
    staff_dir = get_staff_directory()
    if staff_dir:
        try:
            reportees = staff_dir.get_reportees(user.email, recursive=True)
            emails.extend(r.get('email') for r in reportees if r.get('email'))
        except Exception as e:
            logger.warning(f"Failed to get reportees for {user.email}: {e}")
    return emails


def can_access_recording(user, recording: dict) -> bool:
    """Whether `user` may see/play this recording_log row."""
    scope = visible_staff_emails(user)
    if scope is None:
        return True
    owner = (recording.get('staff_email') or '').lower()
    return bool(owner) and owner in {e.lower() for e in scope}
