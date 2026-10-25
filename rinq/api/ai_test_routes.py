"""AI receptionist test line — extension 9000 on the extension directory.

Lets an admin hear the AI receptionist (bot-team's Rosie, "Stuart") as a
customer would, without switching it on for any real call flow:

    central number -> enter 9000 -> "enter the caller ID to pretend to be,
    then hash" -> Rosie, as the Shoalhaven store, in TEST mode

The typed-in number is looked up exactly as a real caller's would be (Clara
customer + Otto orders), so the test shows how much Rosie works out about who
is calling. Rosie is told ?test=1: it writes its summary but files no ticket
and emails no one — the typed-in number may be a real customer's.

Registered via register(api_bp) at import time.
"""

import logging
import os
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

from flask import request, Response

from rinq.config import config
from rinq.database.db import get_db

logger = logging.getLogger(__name__)

AI_TEST_EXTENSION = '9000'

# The store Rosie plays on a test call (Shoalhaven is the pilot candidate)
AI_TEST_STORE = 'Shoalhaven'

VOICE = 'Polly.Nicole'


def _twiml(*lines: str) -> Response:
    body = '\n'.join(['<?xml version="1.0" encoding="UTF-8"?>', '<Response>', *lines, '</Response>'])
    return Response(body, mimetype='application/xml')


def rosie_answer_url() -> str | None:
    """Rosie's answer webhook — from the AI receptionist integration when one is
    configured, else straight from WATSON_ROSIE_URL (the watson tenant runs with
    RINQ_INTEGRATIONS=none, so the integration isn't loaded)."""
    from rinq.integrations import get_ai_receptionist
    ai = get_ai_receptionist()
    url = ai.get_answer_url() if ai else None
    if url:
        return url
    base = (os.environ.get('ROSIE_WEBHOOK_URL') or os.environ.get('WATSON_ROSIE_URL') or '').rstrip('/')
    return f"{base}/api/voice/answer" if base else None


def prompt_twiml(called_number: str, from_number: str) -> Response:
    """Ask for the caller ID to pretend to be. Called from voice_extension_dial."""
    start_url = (
        f"{config.webhook_base_url}/api/voice/ai-test/start"
        f"?called={quote(called_number, safe='')}&from={quote(from_number, safe='')}"
    )
    return _twiml(
        f'    <Gather numDigits="12" finishOnKey="#" timeout="15" action="{xml_escape(start_url)}" method="POST">',
        f'        <Say voice="{VOICE}">AI receptionist test. Enter the phone number you want to pretend '
        f'to be calling from, then press hash. Or just press hash to use your own number.</Say>',
        '    </Gather>',
        # No input at all: carry on with their own number
        f'    <Redirect method="POST">{xml_escape(start_url)}</Redirect>',
    )


def register(bp):

    @bp.route('/voice/ai-test/start', methods=['POST'])
    def voice_ai_test_start():
        """Hand the call to Rosie in test mode, as the typed-in caller ID.

        No auth required - Twilio calls this directly.
        """
        from rinq.services.phone import ensure_plus, to_e164
        from rinq.services.caller_enrichment import get_enrichment_service

        digits = (request.form.get('Digits') or '').strip()
        call_sid = request.form.get('CallSid', '')
        called_number = ensure_plus(request.args.get('called', '').strip())
        own_number = ensure_plus(request.args.get('from', '').strip())
        pretend = to_e164(digits) if digits else own_number

        answer_url = rosie_answer_url()
        if not answer_url:
            logger.error("AI test: no Rosie URL configured (WATSON_ROSIE_URL)")
            return _twiml(f'    <Say voice="{VOICE}">The AI receptionist isn\'t set up on this system.</Say>',
                          '    <Hangup/>')

        db = get_db()

        # Look the pretend caller up exactly as a real inbound call would be
        try:
            caller_info = get_enrichment_service().enrich_caller(pretend) or {}
        except Exception as e:
            logger.warning(f"AI test: enrichment failed for {pretend}: {e}")
            caller_info = {}

        params = {
            'call_sid': call_sid,
            'caller_number': pretend,
            'called_number': called_number,
            'queue_name': AI_TEST_STORE,
            'test': '1',
        }
        for key in ('customer_id', 'customer_name', 'customer_email', 'order_data',
                    'priority', 'priority_reason'):
            if caller_info.get(key):
                params[key] = caller_info[key]

        # call_type tells the call-status callback to tell Rosie the call ended,
        # which is what makes Rosie write its summary
        db.update_call_log(call_sid, {'call_type': 'ai_receptionist', 'status': 'ai_answered'})

        # Rosie has no Twilio account of its own — record from here (best-effort)
        try:
            from rinq.services.recording_service import recording_service
            recording_service.start_recording(call_sid)
        except Exception as e:
            logger.warning(f"AI test: couldn't start recording for {call_sid}: {e}")

        db.log_activity(
            action="ai_test_call",
            target=called_number,
            details=f"From: {own_number}, pretending to be: {pretend}, "
                    f"customer: {caller_info.get('customer_name') or 'not found'}",
            performed_by="twilio"
        )

        found = caller_info.get('customer_name')
        say = (f"Test call as {found}. Connecting you to the receptionist."
               if found else "That number isn't a known customer. Connecting you to the receptionist.")
        return _twiml(
            f'    <Say voice="{VOICE}">{xml_escape(say)}</Say>',
            f'    <Redirect method="POST">{xml_escape(answer_url + "?" + urlencode(params))}</Redirect>',
        )
