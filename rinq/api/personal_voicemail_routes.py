"""Twilio webhooks for personal voicemail lines (services/personal_voicemail.py).

The incoming call itself is answered in routes.voice_incoming, before
anything is logged; this module takes the finished recording.

Registered via register(api_bp) at import time.
"""

import base64
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import request, Response

from rinq.database.db import get_db

logger = logging.getLogger(__name__)

_EMPTY = Response('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                  mimetype='application/xml')


def _download(url, auth):
    """The recording's MP3 from our own Twilio account, or None."""
    import requests
    if not url:
        return None
    for attempt in range(5):
        r = requests.get(url, auth=auth, timeout=30)
        if r.status_code == 200:
            return r.content
        if r.status_code == 404 and attempt < 4:
            time.sleep(2 ** attempt)  # not ready yet: 1, 2, 4, 8s
            continue
        logger.error(f"Personal voicemail: download of {url.rsplit('/', 1)[-1]} got {r.status_code}")
        return None
    return None


def _spawn(fn):
    """Run fn in a background thread (tests run it inline)."""
    threading.Thread(target=fn, daemon=True).start()


def handle_recorded(db, tenant, form, args=None):
    """Take one of Twilio's two callbacks for a message: store the row and
    hand the slow work (download, transcribe, email) to a background thread,
    so Twilio is answered at once. Never raises.

    The Record `action` callback carries the caller (To/From); the
    recordingStatusCallback carries only the recording, so its URL names
    the line (?line=). Whichever arrives first creates the row; a message
    the first one couldn't finish (recording not ready yet, worker killed)
    is finished by a later callback."""
    from rinq.services import personal_voicemail as pv
    from rinq.config import config

    args = args or {}
    tenant = tenant or {}
    recording_sid = form.get('RecordingSid')
    status = form.get('RecordingStatus')     # only on the status callback
    is_status_cb = bool(status)

    line = pv.line_for_number(db, form.get('To')) if form.get('To') else None
    if not line and str(args.get('line') or '').isdigit():
        line = pv.get_line(db, int(args['line']))
    if not line or not recording_sid:
        logger.warning(f"Personal voicemail: recording {recording_sid} for {form.get('To')} "
                       f"has no line")
        return
    msg_id = pv.claim_message(db, line, recording_sid, form.get('From'),
                              form.get('ForwardedFrom'), form.get('RecordingDuration'))
    if not msg_id:
        return
    if is_status_cb:
        pv.note_status_callback(db, msg_id)
    if not pv.take_for_processing(db, msg_id):
        return  # done already, deleted, or another worker has it

    # Everything the thread needs from the request, captured here (gotcha 2:
    # no flask.g in a thread - get_twilio_config/get_current_tenant fail there)
    from rinq.services.recording_service import twilio_recording_media_url
    from rinq.tenant.context import get_twilio_config
    job = {
        'db': db, 'tenant': tenant, 'tenant_id': tenant.get('id'), 'line': line,
        'msg_id': msg_id, 'recording_sid': recording_sid,
        'media_url': twilio_recording_media_url(recording_sid),
        'auth': (get_twilio_config('twilio_account_sid'), get_twilio_config('twilio_auth_token')),
        'base_url': config.webhook_base_url,
        'no_recording': is_status_cb and status != 'completed',
        'last_chance': is_status_cb,
    }

    def run():
        try:
            finish_in_background(**job)
        except Exception as e:
            # The lease goes stale and a later callback can take it over
            logger.exception(f"Personal voicemail {msg_id}: background work failed: {e}")

    _spawn(run)


def finish_in_background(db, tenant, tenant_id, line, msg_id, recording_sid, media_url, auth,
                         base_url, no_recording=False, last_chance=False):
    """Download, transcribe, look up the caller and email the owner - each
    step skipped if an earlier attempt already did it. Runs with no request
    context: everything comes in as arguments."""
    from rinq.services import personal_voicemail as pv

    msg = pv.get_message(db, msg_id)
    if not msg or msg.get('deleted_at'):
        return

    audio = None
    existing = pv.message_audio_path(tenant_id, msg)
    if existing:
        with open(existing, 'rb') as f:
            audio = f.read()
    elif not no_recording:
        audio = _download(media_url, auth)
        if audio:
            pv.finish_message(db, tenant_id, msg_id, audio)
        elif not last_chance and pv.release_for_retry(db, msg_id):
            # Not ready yet: Twilio's status callback will bring it back
            logger.warning(f"Personal voicemail {msg_id}: recording not fetched yet - "
                           f"leaving it for the status callback")
            return

    text, problem = msg.get('transcription'), None
    if audio and not text:
        from rinq.integrations.openai.whisper import get_whisper_service
        whisper = get_whisper_service()
        if whisper.is_configured:
            result = whisper.transcribe(audio, filename=f"voicemail_{recording_sid}.mp3")
            if result.ok:
                text = result.text
            else:
                problem = result.error
                if result.fault:
                    from rinq.services import transcription_alerts
                    transcription_alerts.note_failure(db, tenant, result.error, recording_sid)
    elif not audio:
        problem = ("Twilio kept no recording of it" if no_recording
                   else "the recording couldn't be fetched from Twilio")

    msg = pv.get_message(db, msg_id) or msg   # the caller may have arrived meanwhile
    from_number = msg.get('from_number')
    name = msg.get('caller_name')
    if not name:
        try:
            from rinq.integrations import get_customer_lookup
            from rinq.services.phone import is_lookupable_number
            lookup = get_customer_lookup()
            if lookup and is_lookupable_number(from_number or ''):
                name = (lookup.find_by_phone(from_number) or {}).get('name')
        except Exception as e:
            logger.warning(f"Personal voicemail: caller lookup failed: {e}")

    pv.finish_message(db, tenant_id, msg_id, None, caller_name=name,
                      transcription=text, transcription_problem=problem)

    line = pv.get_line(db, line['id']) or line
    if line.get('email_copies') and not msg.get('emailed_at'):
        if text:
            words = text.strip()
        else:
            words = (f"(Couldn't write this one out - {problem}. Please play the recording.)"
                     if problem else "(Couldn't write this one out. Please play the recording.)")
        when = datetime.now(ZoneInfo('Australia/Sydney')).strftime('%a %-d %b, %-I:%M%p') \
            .replace('AM', 'am').replace('PM', 'pm')
        sent = pv.email_message(
            line['owner_email'], pv.caller_label(from_number, name), when,
            msg.get('duration_seconds'), words,
            base64.b64encode(audio).decode() if audio else None, recording_sid,
            f"{base_url}/#voicemail")
        if sent:
            pv.finish_message(db, tenant_id, msg_id, None,
                              emailed_at=datetime.now(ZoneInfo('UTC')).isoformat())
        else:
            logger.error(f"Personal voicemail {msg_id}: email copy to {line['owner_email']} failed")

    pv.mark_processed(db, msg_id)


def register(bp):

    @bp.route('/voice/personal-voicemail/recorded', methods=['POST'])
    def personal_voicemail_recorded():
        """Twilio: the caller finished their message on a personal line
        (the Record action), or the recording is ready (its status
        callback). Answered at once; the work happens in a thread."""
        from rinq.tenant.context import get_current_tenant
        db = get_db()
        tenant = get_current_tenant() or {}
        try:
            handle_recorded(db, tenant, request.form, request.args)
        except Exception as e:
            logger.exception(f"Personal voicemail: failed to store message: {e}")
        return _EMPTY
