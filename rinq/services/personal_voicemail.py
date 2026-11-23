"""Personal voicemail lines: one person's own message bank (migration 088).

A personal line is a Tina number whose every call plays the owner's greeting
and takes a message. It is deliberately kept apart from the rest of Tina:

- calls are never written to call_log or recording_log, so no report,
  leaderboard, recordings page, Audrey feed or ticket ever shows them;
- the Monday Tina-vs-Twilio cross-check skips these numbers;
- messages are listed and played only to the line's OWNER (My Numbers page).
  Admins set a line up, but no admin screen lists messages;
- any admin change to a line emails the owner, so nobody can quietly
  redirect someone's messages.

Audio lives in data/tenants/<tenant>/personal_voicemail/, not the shared
recordings cache (which is purged and served to managers).
"""

import logging
import os
import re
import secrets
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

from rinq.config import config

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r'[0-9a-f]{32}')
TENANT_RE = re.compile(r'[A-Za-z0-9_-]+')
GREETING_TYPES = {'wav': 'audio/wav', 'mp3': 'audio/mpeg'}
MAX_MESSAGE_SECONDS = 180


def _now():
    return datetime.now(timezone.utc).isoformat()


def data_dir(tenant_id: str, *parts) -> str:
    """This tenant's personal voicemail folder (created on demand)."""
    if not TENANT_RE.fullmatch(tenant_id or ''):
        raise ValueError(f"bad tenant id {tenant_id!r}")
    path = os.path.join(config.tenants_dir, tenant_id, 'personal_voicemail', *parts)
    os.makedirs(path if not os.path.splitext(path)[1] else os.path.dirname(path), exist_ok=True)
    return path


# ---------------------------------------------------------------- lines

def _rows(db, sql, params=()):
    with db._get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(db, sql, params=()):
    rows = _rows(db, sql, params)
    return rows[0] if rows else None


def list_lines(db) -> list[dict]:
    return _rows(db, "SELECT * FROM personal_voicemail_lines ORDER BY owner_email")


def get_line(db, line_id) -> dict | None:
    return _row(db, "SELECT * FROM personal_voicemail_lines WHERE id = ?", (line_id,))


def line_for_number(db, phone_number) -> dict | None:
    if not phone_number:
        return None
    return _row(db, "SELECT * FROM personal_voicemail_lines WHERE phone_number = ?", (phone_number,))


def line_for_owner(db, email) -> dict | None:
    if not email:
        return None
    return _row(db, "SELECT * FROM personal_voicemail_lines WHERE lower(owner_email) = lower(?)",
                (email,))


def personal_numbers(db) -> set:
    """Numbers that are personal lines - kept out of Tina-wide checks."""
    try:
        return {r['phone_number'] for r in _rows(db, "SELECT phone_number FROM personal_voicemail_lines")}
    except Exception:
        return set()  # table not there yet (pre-migration)


def create_line(db, phone_number, owner_email, by) -> int:
    now = _now()
    with db._get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO personal_voicemail_lines (phone_number, owner_email, created_at,
                created_by, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (phone_number, owner_email.strip().lower(), now, by, now, by))
        # The number now belongs to this line, not to a call flow or box.
        conn.execute("UPDATE phone_numbers SET call_flow_id = NULL, voicemail_destination_id = NULL "
                     "WHERE phone_number = ?", (phone_number,))
        conn.commit()
        return cur.lastrowid


def update_line(db, line_id, by, **fields) -> None:
    allowed = {'phone_number', 'owner_email', 'email_copies', 'greeting_token', 'greeting_ext'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ', '.join(f"{k} = ?" for k in fields)
    with db._get_conn() as conn:
        conn.execute(f"UPDATE personal_voicemail_lines SET {sets}, updated_at = ?, updated_by = ? "
                     f"WHERE id = ?", (*fields.values(), _now(), by, line_id))
        if 'phone_number' in fields:
            conn.execute("UPDATE phone_numbers SET call_flow_id = NULL, voicemail_destination_id = NULL "
                         "WHERE phone_number = ?", (fields['phone_number'],))
        conn.commit()


def delete_line(db, tenant_id, line_id) -> None:
    """Remove a line and everything in it - messages and audio included."""
    line = get_line(db, line_id)
    if not line:
        return
    for m in _rows(db, "SELECT audio_file FROM personal_voicemails WHERE line_id = ?", (line_id,)):
        _remove_file(tenant_id, 'messages', m.get('audio_file'))
    if line.get('greeting_token'):
        _remove_file(tenant_id, 'greetings', f"{line['greeting_token']}.{line['greeting_ext']}")
    with db._get_conn() as conn:
        conn.execute("DELETE FROM personal_voicemails WHERE line_id = ?", (line_id,))
        conn.execute("DELETE FROM personal_voicemail_lines WHERE id = ?", (line_id,))
        conn.commit()


def _remove_file(tenant_id, folder, name):
    if not name or '/' in name or '\\' in name:
        return
    try:
        os.remove(os.path.join(data_dir(tenant_id, folder), name))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Personal voicemail: couldn't remove {folder}/{name}: {e}")


# ---------------------------------------------------------------- greeting

def save_greeting(db, tenant_id, line, audio: bytes, ext: str, by) -> None:
    """Replace the line's greeting. A new random file name each time, so a
    Twilio-cached copy of the old one is never played again."""
    if ext not in GREETING_TYPES:
        raise ValueError("greeting must be a WAV or MP3 file")
    token = secrets.token_hex(16)
    with open(os.path.join(data_dir(tenant_id, 'greetings'), f"{token}.{ext}"), 'wb') as f:
        f.write(audio)
    old = (line.get('greeting_token'), line.get('greeting_ext'))
    update_line(db, line['id'], by, greeting_token=token, greeting_ext=ext)
    if old[0]:
        _remove_file(tenant_id, 'greetings', f"{old[0]}.{old[1]}")


def clear_greeting(db, tenant_id, line, by) -> None:
    if line.get('greeting_token'):
        _remove_file(tenant_id, 'greetings', f"{line['greeting_token']}.{line['greeting_ext']}")
    update_line(db, line['id'], by, greeting_token=None, greeting_ext=None)


def greeting_file(tenant_id, token, ext) -> str | None:
    """Path of a greeting, or None. Twilio fetches these unauthenticated, so
    the 32-hex random name is the only key - nothing else is served."""
    if not (TENANT_RE.fullmatch(tenant_id or '') and TOKEN_RE.fullmatch(token or '')
            and ext in GREETING_TYPES):
        return None
    path = os.path.join(config.tenants_dir, tenant_id, 'personal_voicemail', 'greetings',
                        f"{token}.{ext}")
    return path if os.path.isfile(path) else None


def greeting_url(tenant_id, line) -> str | None:
    if not line.get('greeting_token'):
        return None
    return (f"{config.webhook_base_url}/personal-greeting/{tenant_id}/"
            f"{line['greeting_token']}.{line['greeting_ext']}")


# ---------------------------------------------------------------- calls

def call_twiml(tenant_id, line) -> str:
    """Every call: the owner's greeting (or a generic one), then the tone."""
    url = greeting_url(tenant_id, line)
    if url:
        prompt = f'    <Play>{xml_escape(url)}</Play>'
    else:
        prompt = ('    <Say voice="Polly.Olivia-Neural">The person you are calling is not '
                  'available. Please leave a message after the tone.</Say>')
    action = f"{config.webhook_base_url}/api/voice/personal-voicemail/recorded"
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
            '    <Pause length="1"/>\n'
            f'{prompt}\n'
            f'    <Record action="{xml_escape(action)}" maxLength="{MAX_MESSAGE_SECONDS}" '
            'playBeep="true" />\n'
            '</Response>')


def claim_message(db, line, recording_sid, from_number, forwarded_from, duration) -> int | None:
    """Insert the message row; None if this recording was already taken
    (Twilio retries its webhook)."""
    with db._get_conn() as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO personal_voicemails (line_id, recording_sid, from_number,
                forwarded_from, duration_seconds, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (line['id'], recording_sid, from_number, forwarded_from,
              int(duration) if str(duration or '').isdigit() else None, _now()))
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def finish_message(db, tenant_id, message_id, audio: bytes | None, **fields) -> None:
    if audio:
        name = f"{message_id}_{secrets.token_hex(8)}.mp3"
        with open(os.path.join(data_dir(tenant_id, 'messages'), name), 'wb') as f:
            f.write(audio)
        fields['audio_file'] = name
    allowed = {'audio_file', 'caller_name', 'transcription', 'transcription_problem', 'emailed_at'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        sets = ', '.join(f"{k} = ?" for k in fields)
        with db._get_conn() as conn:
            conn.execute(f"UPDATE personal_voicemails SET {sets} WHERE id = ?",
                         (*fields.values(), message_id))
            conn.commit()


# ---------------------------------------------------------------- messages (owner only)

def messages(db, line_id) -> list[dict]:
    return _rows(db, """SELECT * FROM personal_voicemails
                        WHERE line_id = ? AND deleted_at IS NULL
                        ORDER BY created_at DESC LIMIT 200""", (line_id,))


def message_for_owner(db, message_id, owner_email) -> dict | None:
    """The message, only if it is on a line this person owns."""
    return _row(db, """
        SELECT m.* FROM personal_voicemails m
        JOIN personal_voicemail_lines l ON l.id = m.line_id
        WHERE m.id = ? AND m.deleted_at IS NULL AND lower(l.owner_email) = lower(?)
    """, (message_id, owner_email))


def message_audio_path(tenant_id, message) -> str | None:
    name = (message or {}).get('audio_file')
    if not name or '/' in name or '\\' in name:
        return None
    path = os.path.join(config.tenants_dir, tenant_id, 'personal_voicemail', 'messages', name)
    return path if os.path.isfile(path) else None


def delete_message(db, tenant_id, message) -> None:
    """Gone for good: the audio and the words. The row stays, emptied, so a
    late Twilio retry of the same recording can't bring it back."""
    _remove_file(tenant_id, 'messages', message.get('audio_file'))
    with db._get_conn() as conn:
        conn.execute("""UPDATE personal_voicemails
                        SET deleted_at = ?, audio_file = NULL, transcription = NULL,
                            from_number = NULL, caller_name = NULL, forwarded_from = NULL
                        WHERE id = ?""", (_now(), message['id']))
        conn.commit()


# ---------------------------------------------------------------- email

def caller_label(from_number, name=None) -> str:
    from rinq.services.phone import to_local, is_lookupable_number
    if not is_lookupable_number(from_number or ''):
        return 'a withheld number'
    local = to_local(from_number)
    return f"{name} ({local})" if name else local


def email_message(owner_email, who, when_text, duration, message_text, audio_b64, recording_sid,
                  page_url) -> bool:
    from rinq.integrations import get_email_service
    body = (f"From: {who}\n"
            f"When: {when_text}\n"
            f"Length: {duration or '?'} seconds\n\n"
            f"Message:\n{message_text}\n\n"
            f"The recording is attached. Your messages are also on Tina: {page_url}\n")
    email = get_email_service()
    attachments = [{'filename': f"voicemail_{recording_sid}.mp3", 'content_type': 'audio/mpeg',
                    'content_base64': audio_b64}] if audio_b64 else None
    try:
        return bool(email and email.send_email(to=owner_email, subject=f"Voicemail from {who}",
                                               text_body=body, attachments=attachments))
    except Exception as e:
        logger.error(f"Personal voicemail email to {owner_email} failed: {e}")
        return False


def notify_owner_of_change(owners, what, by_email) -> None:
    """Tell the owner(s) an admin changed their line. Best effort."""
    from rinq.integrations import get_email_service
    email = get_email_service()
    if not email:
        return
    body = (f"{by_email} changed your personal voicemail on Tina:\n\n{what}\n\n"
            f"If you didn't expect this, tell Derek.\n")
    for owner in {o for o in owners if o}:
        try:
            email.send_email(to=owner, subject="Your Tina voicemail was changed", text_body=body)
        except Exception as e:
            logger.error(f"Personal voicemail change notice to {owner} failed: {e}")
