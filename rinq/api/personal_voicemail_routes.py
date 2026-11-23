"""Twilio webhooks for personal voicemail lines (services/personal_voicemail.py).

The incoming call itself is answered in routes.voice_incoming, before
anything is logged; this module takes the finished recording.

Registered via register(api_bp) at import time.
"""

import base64
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import request, Response

from rinq.database.db import get_db

logger = logging.getLogger(__name__)

_EMPTY = Response('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                  mimetype='application/xml')


def _download(recording_sid):
    """The recording's MP3 from our own Twilio account, or None."""
    import requests
    from rinq.services.recording_service import twilio_recording_media_url
    from rinq.tenant.context import get_twilio_config
    url = twilio_recording_media_url(recording_sid)
    if not url:
        return None
    auth = (get_twilio_config('twilio_account_sid'), get_twilio_config('twilio_auth_token'))
    for attempt in range(5):
        r = requests.get(url, auth=auth, timeout=30)
        if r.status_code == 200:
            return r.content
        if r.status_code == 404 and attempt < 4:
            time.sleep(2 ** attempt)  # not ready yet: 1, 2, 4, 8s
            continue
        logger.error(f"Personal voicemail: download of {recording_sid} got {r.status_code}")
        return None
    return None


def handle_recorded(db, tenant_id, form):
    """Store one message on its line and email the owner. Never raises."""
    from rinq.services import personal_voicemail as pv
    from rinq.config import config

    line = pv.line_for_number(db, form.get('To'))
    recording_sid = form.get('RecordingSid')
    if not line or not recording_sid:
        logger.warning(f"Personal voicemail: recording {recording_sid} for {form.get('To')} "
                       f"has no line")
        return
    msg_id = pv.claim_message(db, line, recording_sid, form.get('From'),
                              form.get('ForwardedFrom'), form.get('RecordingDuration'))
    if not msg_id:
        return  # Twilio retried; already taken

    audio = _download(recording_sid)

    text, problem = None, None
    if audio:
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
                    from rinq.tenant.context import get_current_tenant
                    transcription_alerts.note_failure(db, get_current_tenant(), result.error,
                                                      recording_sid)
    else:
        problem = "the recording couldn't be fetched from Twilio"

    name = None
    try:
        from rinq.integrations import get_customer_lookup
        from rinq.services.phone import is_lookupable_number
        lookup = get_customer_lookup()
        if lookup and is_lookupable_number(form.get('From') or ''):
            name = (lookup.find_by_phone(form.get('From')) or {}).get('name')
    except Exception as e:
        logger.warning(f"Personal voicemail: caller lookup failed: {e}")

    pv.finish_message(db, tenant_id, msg_id, audio, caller_name=name,
                      transcription=text, transcription_problem=problem)

    if line.get('email_copies'):
        if text:
            words = text.strip()
        else:
            words = (f"(Couldn't write this one out - {problem}. Please play the recording.)"
                     if problem else "(Couldn't write this one out. Please play the recording.)")
        when = datetime.now(ZoneInfo('Australia/Sydney')).strftime('%a %-d %b, %-I:%M%p') \
            .replace('AM', 'am').replace('PM', 'pm')
        sent = pv.email_message(
            line['owner_email'], pv.caller_label(form.get('From'), name), when,
            form.get('RecordingDuration'), words,
            base64.b64encode(audio).decode() if audio else None, recording_sid,
            f"{config.webhook_base_url}/#voicemail")
        if sent:
            pv.finish_message(db, tenant_id, msg_id, None,
                              emailed_at=datetime.now(ZoneInfo('UTC')).isoformat())
        else:
            logger.error(f"Personal voicemail {msg_id}: email copy to {line['owner_email']} failed")


def register(bp):

    @bp.route('/voice/personal-voicemail/recorded', methods=['POST'])
    def personal_voicemail_recorded():
        """Twilio: the caller finished their message on a personal line."""
        from rinq.tenant.context import get_current_tenant
        db = get_db()
        tenant = get_current_tenant() or {}
        try:
            handle_recorded(db, tenant.get('id'), request.form)
        except Exception as e:
            logger.exception(f"Personal voicemail: failed to store message: {e}")
        return _EMPTY
