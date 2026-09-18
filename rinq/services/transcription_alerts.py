"""Tell somebody when voicemail transcription stops working.

A Whisper failure is invisible from the outside: the voicemail still arrives,
still carries its recording, and the ticket just says no transcription was
available. Between 2026-09-10 and 2026-09-18 that state lasted eight days and
254 voicemails before a staff member mentioned it — the reason was sitting in
the log the whole time.

One email per outage, not one per voicemail, and not one per flap:

* the same reason inside COOLDOWN_HOURS is not re-sent, so a service that
  fails, works, and fails again every few minutes still costs one email;
* a DIFFERENT reason always gets through (a rejected key after a credit
  problem is new news);
* a success marks the outage resolved but leaves the timestamp, so the
  cooldown still applies — clearing it outright is what would let a flap send
  an email per voicemail.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_KEY = 'transcription_failure_alert'
COOLDOWN_HOURS = 6


def _now():
    return datetime.now(timezone.utc)


def _load(db) -> dict:
    try:
        raw = db.get_bot_setting(SETTING_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        # A malformed value must not stop the alert — treat it as "nothing
        # recorded", which errs toward telling someone.
        return {}


def _save(db, payload: dict) -> None:
    try:
        db.set_bot_setting(SETTING_KEY, json.dumps(payload), 'tina')
    except Exception as e:
        logger.warning(f"Could not record transcription alert state: {e}")


def _admin_emails(tenant_id: str) -> list[str]:
    """Tenant admins. Derived, so nobody has to maintain a recipient list."""
    from rinq.database.master import get_master_db
    try:
        users = get_master_db().get_tenant_users(tenant_id)
    except Exception as e:
        logger.warning(f"Could not read tenant admins for {tenant_id}: {e}")
        return []
    return [u['email'] for u in users if u.get('role') == 'admin' and u.get('email')]


def _within_cooldown(stored: dict) -> bool:
    try:
        alerted_at = datetime.fromisoformat(stored['alerted_at'])
    except Exception:
        return False
    return _now() - alerted_at < timedelta(hours=COOLDOWN_HOURS)


def note_success(db) -> None:
    """Mark the outage resolved after a transcription works.

    Writes only when there is an unresolved outage on record, so the ordinary
    path costs one read.
    """
    stored = _load(db)
    if not stored or stored.get('resolved_at'):
        return
    stored['resolved_at'] = _now().isoformat()
    _save(db, stored)
    logger.info(f"Voicemail transcription recovered (was: {stored.get('reason')})")


def note_failure(db, tenant, reason: str, recording_sid: str = None) -> bool:
    """Record a transcription failure and email the admins if it is news.

    Returns True if an alert was sent.
    """
    if not reason or not tenant:
        return False

    stored = _load(db)
    if stored.get('reason') == reason and _within_cooldown(stored):
        return False

    recipients = _admin_emails(tenant['id'])
    product = tenant.get('product_name') or 'Rinq'
    subject = f"{product}: voicemail transcriptions have stopped"
    body = (
        f"Voicemail transcription is failing — {reason}.\n\n"
        "What this means day to day: voicemails are still arriving and the "
        "recording is still attached, so nothing is being lost. But the "
        "typed-out message is missing, so staff have to play each one to find "
        "out what it says.\n\n"
        "Voicemails recorded while this is broken will not transcribe "
        "themselves later — they need a backfill once it is fixed "
        "(rinq/scripts/backfill_voicemail_transcriptions.py).\n\n"
        f"First noticed: {_now().strftime('%Y-%m-%d %H:%M UTC')}\n"
    )
    if recording_sid:
        body += f"First affected recording: {recording_sid}\n"
    body += (
        f"\nYou will not get another email about this for at least "
        f"{COOLDOWN_HOURS} hours, or until the reason changes.\n"
    )

    sent = False
    if not recipients:
        logger.error(
            f"Voicemail transcription failing ({reason}) and tenant "
            f"{tenant['id']} has no admin to tell"
        )
    else:
        from rinq.integrations import get_email_service
        email_service = get_email_service()
        if not email_service:
            logger.error(
                f"Voicemail transcription failing ({reason}) and no email "
                f"service is configured to report it"
            )
        else:
            for address in recipients:
                try:
                    if email_service.send_email(to=address, subject=subject, text_body=body):
                        sent = True
                    else:
                        logger.warning(f"Transcription alert to {address} was not accepted")
                except Exception as e:
                    logger.warning(f"Transcription alert to {address} failed: {e}")

    # Record the attempt whether or not the email got out: a failed send must
    # not put us into a loop of retrying an email on every single voicemail.
    # The log carries the reason if nobody could be reached.
    _save(db, {
        'reason': reason,
        'alerted_at': _now().isoformat(),
        'resolved_at': None,
        'notified': recipients if sent else [],
        'recording_sid': recording_sid,
    })

    if sent:
        logger.error(f"Voicemail transcription failing ({reason}) — alerted {', '.join(recipients)}")
    return sent
