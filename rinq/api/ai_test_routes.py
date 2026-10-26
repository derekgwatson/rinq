"""AI receptionist test line — extension 9000 on the extension directory.

Lets an admin hear the AI receptionist (bot-team's Rosie, "Stuart") as a
customer would, without switching it on for any real call flow:

    central number -> enter 9000 -> "enter the caller ID to pretend to be,
    then hash" -> Rosie, as the Shoalhaven store, in TEST mode

The typed-in number is looked up exactly as a real caller's would be (Clara
customer + Otto orders), so the test shows how much Rosie works out about who
is calling. Rosie is told ?test=1: it writes its summary but files no ticket
and emails no one — the typed-in number may be a real customer's.

ANSWER-FIRST (2026-10-26): Stuart answers at once with the store's own
welcome while the tester's Tina (playing the store's staff member, e.g. Phil)
rings for RING_SECONDS. Their phone screen shows the live transcript; if they
answer, the caller is moved off Stuart into a conference with them
(/voice/ai-takeover/*). The tester is found from the number they rang 9000
from (address book), so it rings the person testing, never a real store.

Registered via register(api_bp) at import time.
"""

import logging
import os
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

from flask import jsonify, request, Response

from rinq.config import config
from rinq.database.db import get_db

try:
    from shared.auth.bot_api import api_or_session_auth as _session_or_api
except ImportError:
    from rinq.auth.decorators import api_or_session_auth as _session_or_api

logger = logging.getLogger(__name__)

AI_TEST_EXTENSION = '9000'

# The store Rosie plays on a test call (Shoalhaven is the pilot candidate)
AI_TEST_STORE = 'Shoalhaven'

VOICE = 'Polly.Olivia-Neural'

# How long the staff member's phone rings while Stuart talks
RING_SECONDS = 30

TAKEOVER_PREFIX = 'aitake_'


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


def store_welcome(db) -> str:
    """The test store's own welcome greeting (Admin -> Audio), for Stuart to say."""
    try:
        for f in db.get_audio_files(file_type='greeting') or []:
            if AI_TEST_STORE.lower() in (f.get('name') or '').lower() and f.get('tts_text'):
                return f['tts_text'].strip()
    except Exception as e:
        logger.warning(f"AI test: couldn't read the {AI_TEST_STORE} welcome: {e}")
    return ''


def ring_for_takeover(staff: dict, customer_sid: str, show_as: str, db) -> None:
    """Ring the staff member's Tina (browser) for RING_SECONDS while Stuart
    talks. Answering runs /voice/ai-takeover/answer, which moves the caller
    over. The ring leg shows the caller's (pretend) number, so the banner
    names the customer."""
    from rinq.api.identity import email_to_browser_identity
    from rinq.services.twilio_service import get_twilio_service, start_twilio_thread

    base_url = config.webhook_base_url
    room = f"{TAKEOVER_PREFIX}{customer_sid}"
    answer_url = f"{base_url}/api/voice/ai-takeover/answer?" + urlencode({
        'customer': customer_sid, 'room': room,
        'email': staff['email'], 'name': staff.get('name') or '',
    })
    to = f"client:{email_to_browser_identity(staff['email'])}"

    def ring():
        try:
            call = get_twilio_service().client.calls.create(
                to=to, from_=show_as, url=answer_url, method='POST', timeout=RING_SECONDS)
            db.store_ring_attempts(room, [call.sid], 'ai_takeover')
            logger.info(f"AI test: ringing {staff['email']} for takeover of {customer_sid}: {call.sid}")
        except Exception as e:
            logger.warning(f"AI test: couldn't ring {staff['email']}: {e}")

    start_twilio_thread(ring)


def takeover_room_for(call_sid: str, db) -> str | None:
    """The takeover room a staff call leg belongs to - from its ring attempt
    while ringing, then from the conference it joined (ring attempts are
    purged after 10 minutes)."""
    meta = db.get_ring_attempt_metadata(call_sid) if call_sid else None
    room = (meta or {}).get('group_key')
    if not room:
        participant = db.get_participant_by_sid(call_sid) if call_sid else None
        room = (participant or {}).get('conference_name')
    return room if room and room.startswith(TAKEOVER_PREFIX) else None


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

        # Answer-first: Stuart says the store's own welcome, and the person
        # testing (found by the number they rang from) is rung as the store
        welcome = store_welcome(db)
        if welcome:
            params['welcome'] = welcome
        tester = None
        try:
            tester = db.get_address_book_by_mobile(own_number) if own_number else None
        except Exception as e:
            logger.warning(f"AI test: tester lookup failed for {own_number}: {e}")
        if tester and tester.get('email'):
            params['ringing'] = '1'
            ring_for_takeover(tester, call_sid, pretend, db)

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

    @bp.route('/voice/ai-takeover/answer', methods=['POST'])
    def voice_ai_takeover_answer():
        """The staff member answered while Stuart was talking: move the caller
        off Stuart into a conference with them, then join it.

        No auth required - Twilio calls this directly.
        """
        from rinq.services.twilio_service import get_twilio_service

        customer = request.args.get('customer', '')
        room = request.args.get('room', '')
        email = request.args.get('email', '')
        name = request.args.get('name', '')
        first = (name.split() or ['the team'])[0]
        base = config.webhook_base_url
        db = get_db()

        if not room.startswith(TAKEOVER_PREFIX) or room != f"{TAKEOVER_PREFIX}{customer}":
            return _twiml('    <Hangup/>')
        try:
            get_twilio_service().client.calls(customer).update(
                url=f"{base}/api/voice/ai-takeover/customer-join?"
                    + urlencode({'room': room, 'name': first}),
                method='POST')
        except Exception as e:
            logger.warning(f"AI takeover: couldn't move {customer} (hung up?): {e}")
            return _twiml(f'    <Say voice="{VOICE}">Sorry, that caller has already hung up.</Say>',
                          '    <Hangup/>')

        db.update_call_log(customer, {'agent_email': email, 'status': 'answered',
                                      'answered_at': 'CURRENT_TIMESTAMP'})
        db.log_activity(action="ai_takeover", target=customer,
                        details=f"{email} took over from the AI receptionist", performed_by="twilio")
        _tell_rosie_handover(customer, name)

        join = f"{base}/api/voice/conference/join?" + urlencode({'room': room, 'role': 'agent'})
        return _twiml(f'    <Redirect method="POST">{xml_escape(join)}</Redirect>')

    @bp.route('/voice/ai-takeover/customer-join', methods=['POST'])
    def voice_ai_takeover_customer_join():
        """The caller's side of a takeover: a one-line heads-up in Tina's voice,
        then into the room with the staff member.

        No auth required - Twilio calls this directly.
        """
        from rinq.api.callback_routes import _speak
        room = request.args.get('room', '')
        name = request.args.get('name', '') or 'someone from the team'
        join = f"{config.webhook_base_url}/api/voice/conference/join?" + urlencode(
            {'room': room, 'role': 'caller'})
        return _twiml(_speak(f"{name} has just picked up. Putting you through now."),
                      f'    <Redirect method="POST">{xml_escape(join)}</Redirect>')

    @bp.route('/ai-transcript', methods=['GET'])
    @_session_or_api
    def ai_transcript():
        """Live transcript of Stuart's conversation, for the staff member's
        phone screen while it rings and after they take over.

        ?call_sid= is the staff member's own leg (the browser's CallSid).
        """
        room = takeover_room_for(request.args.get('call_sid', ''), get_db())
        if not room:
            return jsonify({'found': False})
        try:
            from rinq.integrations.watson.ai_receptionist import WatsonAIReceptionist
            resp = WatsonAIReceptionist().client.get(
                f"/api/conversations/by-call/{quote(room[len(TAKEOVER_PREFIX):], safe='')}", timeout=4)
            if resp.ok:
                return jsonify(resp.json())
        except Exception as e:
            logger.debug(f"AI transcript fetch failed: {e}")
        return jsonify({'found': False})


def _tell_rosie_handover(call_sid: str, name: str) -> None:
    """Best effort: note in Stuart's transcript that a person took over, so
    his summary says so."""
    try:
        from rinq.integrations.watson.ai_receptionist import WatsonAIReceptionist
        WatsonAIReceptionist().client.post('/api/voice/handover',
                                           json={'call_sid': call_sid, 'name': name}, timeout=3)
    except Exception as e:
        logger.debug(f"AI takeover: couldn't tell Rosie about {call_sid}: {e}")

