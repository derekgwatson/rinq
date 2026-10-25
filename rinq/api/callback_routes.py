"""Callbacks — "Derek tried to call you, press 1 to be put through".

Staff call customers from a shared number, so a customer calling back lands
on that number's normal flow (a showroom queue, or the "enter an extension"
menu) and has to be passed along by hand. When an inbound caller's number was
rung by a staff member in the last CALLBACK_WINDOW_DAYS, voice_incoming offers
them that person first:

    offer (voice_incoming) --1--> /voice/callback/choice --> ring the person
          |                              |                        |
          no key / other key       person now unavailable     no answer
          v                              v                        v
    the number's normal flow  <----------+------------------------+

The normal flow is reached by redirecting back to /voice/incoming with
?callback=done, so a callback can never leave a caller worse off than not
being offered one: whatever happens, they end up where they would have.

The offer is only made while the number is open, and only when the person
could actually take the call (not DND, has a device, not already on a call).
Nobody is offered a person who can't answer.

Registered via register(api_bp) at import time.
"""

import logging
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from flask import request, Response

from rinq.config import config
from rinq.database.db import get_db
from rinq.services.person_devices import person_ring_targets

logger = logging.getLogger(__name__)

# How far back an outbound call still makes an inbound call a callback.
CALLBACK_WINDOW_DAYS = 14

# Seconds the caller has to press 1 once the offer finishes.
OFFER_TIMEOUT_SECONDS = 4

VOICE = 'Polly.Nicole'


def _twiml(*lines: str) -> Response:
    body = '\n'.join(['<?xml version="1.0" encoding="UTF-8"?>', '<Response>', *lines, '</Response>'])
    return Response(body, mimetype='application/xml')


def _normal_flow_url() -> str:
    """voice_incoming again, skipping the offer. Twilio's <Redirect> re-posts
    To/From/CallSid, so the normal flow sees the call exactly as before."""
    return f"{config.webhook_base_url}/api/voice/incoming?callback=done"


def _to_normal_flow(say: str | None = None) -> Response:
    lines = []
    if say:
        lines.append(f'    <Say voice="{VOICE}">{xml_escape(say)}</Say>')
    lines.append(f'    <Redirect method="POST">{xml_escape(_normal_flow_url())}</Redirect>')
    return _twiml(*lines)


def _unavailable(name: str) -> str:
    who = f"{name} isn't" if name else "they're not"
    return f"Sorry, {who} available right now. I'll put you through to the team."


def _first_name(email: str, db) -> str:
    user = db.get_user_by_email(email)
    name = (user.get('friendly_name') if user else None) or ''
    return name.split()[0] if name.strip() else ''


def _is_on_a_call(email: str, db) -> bool:
    try:
        return any((c.get('agent_email') or '').lower() == email.lower()
                   for c in db.get_active_calls())
    except Exception as e:
        # Can't tell — don't offer. The caller still gets the normal flow.
        logger.warning(f"Callback: couldn't check whether {email} is on a call: {e}")
        return True


def find_callback_person(from_number: str, db) -> dict | None:
    """The staff member this caller is probably calling back, if they can answer.

    Returns {'email', 'name'} or None. None means "don't offer", which is
    always safe — the caller just gets the number's normal flow.
    """
    # Withheld / anonymous / SIP callers have nothing to match on
    if not from_number or not from_number.startswith('+'):
        return None

    email = db.get_recent_outbound_caller(from_number, days=CALLBACK_WINDOW_DAYS)
    if not email:
        return None

    # Same import the ring path uses; lazy because routes.py imports this module
    from rinq.api.routes import _get_sip_domain
    if not person_ring_targets(email, db, _get_sip_domain()):
        logger.info(f"Callback from {from_number}: {email} has DND on or no device — not offering")
        return None
    if _is_on_a_call(email, db):
        logger.info(f"Callback from {from_number}: {email} is on a call — not offering")
        return None

    return {'email': email, 'name': _first_name(email, db)}


def last_called_by(number: str, db, viewer_email: str | None = None) -> dict | None:
    """Who rang this number recently, for the phone screen.

    Whoever answers a callback (often not the person offered — they may have
    been busy, or the caller didn't press 1) sees whose customer it is.
    Shown whether or not the offer was made. Same window as the offer.

    Returns {'email', 'name', 'label', 'when'} or None, e.g.
    label "Suzette's customer" / "Your customer", when "called 3 days ago".
    """
    from datetime import datetime
    import pytz
    from rinq.database.db import _parse_dt
    from rinq.services.phone import to_e164

    call = db.get_recent_outbound_call(to_e164(number or ''), days=CALLBACK_WINDOW_DAYS)
    if not call:
        return None

    email = call['agent_email']
    name = _first_name(email, db) or email.split('@')[0]
    if viewer_email and viewer_email.lower() == email.lower():
        label = 'Your customer'
    else:
        label = f"{name}'s customer"

    when = ''
    try:
        tz = pytz.timezone('Australia/Sydney')
        days = (datetime.now(tz).date() - _parse_dt(call['started_at']).astimezone(tz).date()).days
        when = 'called today' if days <= 0 else 'called yesterday' if days == 1 else f'called {days} days ago'
    except Exception as e:
        logger.debug(f"last_called_by: couldn't date {call.get('started_at')}: {e}")

    return {'email': email, 'name': name, 'label': label, 'when': when}


def offer_twiml(person: dict, called_number: str, from_number: str) -> Response:
    """The "press 1" offer. No key falls through to the normal flow."""
    choice_url = (
        f"{config.webhook_base_url}/api/voice/callback/choice"
        f"?called={quote(called_number, safe='')}&from={quote(from_number, safe='')}"
    )
    name = xml_escape(person['name'])
    if name:
        offer = (f'Hi, it looks like {name} from Watson Blinds tried to call you. '
                 f'To speak with {name}, press 1. Otherwise, please stay on the line.')
    else:
        offer = ('Hi, it looks like someone from Watson Blinds tried to call you. '
                 'To speak with them, press 1. Otherwise, please stay on the line.')
    return _twiml(
        '    <Pause length="1"/>',
        f'    <Gather numDigits="1" action="{xml_escape(choice_url)}" method="POST" timeout="{OFFER_TIMEOUT_SECONDS}">',
        f'        <Say voice="{VOICE}">{offer}</Say>',
        '    </Gather>',
        f'    <Redirect method="POST">{xml_escape(_normal_flow_url())}</Redirect>',
    )


def register(bp):

    @bp.route('/voice/callback/choice', methods=['POST'])
    def voice_callback_choice():
        """The caller answered the offer. 1 rings the person; anything else
        goes to the normal flow.

        The person is looked up again from the caller's number rather than
        trusted from the URL — this endpoint is open to Twilio, and it must
        only ever ring someone who actually called this number.

        No auth required - Twilio calls this directly.
        """
        from rinq.api.routes import _build_dial_targets, _ring_targets_into_conference
        from rinq.services.phone import ensure_plus

        digits = request.form.get('Digits', '')
        call_sid = request.form.get('CallSid', '')
        called_number = ensure_plus(request.args.get('called', '').strip())
        from_number = ensure_plus(request.args.get('from', '').strip())

        db = get_db()

        if digits != '1':
            db.log_activity(action="callback_declined", target=called_number,
                            details=f"From: {from_number}, pressed: {digits or 'nothing'}",
                            performed_by="twilio")
            return _to_normal_flow()

        person = find_callback_person(from_number, db)
        if not person:
            # They became unavailable while the offer was playing
            return _to_normal_flow(_unavailable(''))

        email, name = person['email'], person['name']
        dial_targets = _build_dial_targets({'assignments': [email], 'user_settings': {}})
        if not dial_targets:
            return _to_normal_flow(_unavailable(name))

        # Same conference-first ring as an extension call. call_<sid> keeps
        # recordings and the call log tied to the caller's call.
        conference_name = f"call_{call_sid}"
        db.set_call_conference(call_sid, conference_name)
        _ring_targets_into_conference(dial_targets, conference_name, from_number, call_sid,
                                      base_url=config.webhook_base_url, db=db)

        no_answer_url = (
            f"{config.webhook_base_url}/api/voice/callback/no-answer"
            f"?name={quote(name, safe='')}"
        )
        ringback_url = f"{config.webhook_base_url}/api/voice/ringback"
        connecting = f"Connecting you to {xml_escape(name)}." if name else "Connecting you now."

        db.log_activity(action="callback_ringing", target=called_number,
                        details=f"From: {from_number}, User: {email}, Targets: {len(dial_targets)}, "
                                f"Conference: {conference_name}",
                        performed_by="twilio")

        return _twiml(
            f'    <Say voice="{VOICE}">{connecting}</Say>',
            '    <Dial>',
            f'        <Conference startConferenceOnEnter="false" endConferenceOnExit="true" beep="false" '
            f'waitUrl="{xml_escape(ringback_url)}" waitMethod="POST">{xml_escape(conference_name)}</Conference>',
            '    </Dial>',
            f'    <Redirect method="POST">{xml_escape(no_answer_url)}</Redirect>',
        )

    @bp.route('/voice/callback/no-answer', methods=['POST'])
    def voice_callback_no_answer():
        """The ring ended. Answered → the call is over. Not answered → the
        number's normal flow (queue, extension menu…), so the caller still
        gets through to someone.

        No auth required - Twilio calls this directly.
        """
        call_sid = request.form.get('CallSid', '')
        name = request.args.get('name', '')

        db = get_db()
        if db.get_call_log_field(call_sid, 'status') in ('answered', 'completed', 'transferred'):
            return _twiml('    <Hangup/>')

        db.log_activity(action="callback_no_answer", target=request.form.get('To', ''),
                        details=f"From: {request.form.get('From', '')}, {name or 'callback'} not answered — normal flow",
                        performed_by="twilio")
        return _to_normal_flow(_unavailable(name))
