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

from flask import jsonify, request, Response

from rinq.config import config
from rinq.database.db import get_db
from rinq.services.person_devices import person_ring_targets
from rinq.services.auth import login_required
try:
    from shared.auth.bot_api import api_or_session_auth
except ImportError:
    from rinq.auth.decorators import api_or_session_auth
from rinq.auth.decorators import bot_tenant

logger = logging.getLogger(__name__)

# How far back an outbound call still makes an inbound call a callback.
CALLBACK_WINDOW_DAYS = 14

# Seconds the caller has to press 1 once the offer finishes.
OFFER_TIMEOUT_SECONDS = 4

VOICE = 'Polly.Olivia-Neural'


def _twiml(*lines: str) -> Response:
    body = '\n'.join(['<?xml version="1.0" encoding="UTF-8"?>', '<Response>', *lines, '</Response>'])
    return Response(body, mimetype='application/xml')


def _speak(text: str, indent: str = '    ') -> str:
    """Tina's own voice (Admin -> Audio default), else the stock voice."""
    from rinq.services.tts_service import phrase_audio_url
    from rinq.api.twiml import get_full_audio_url
    url = phrase_audio_url(text)
    if url:
        return f'{indent}<Play>{xml_escape(get_full_audio_url(url))}</Play>'
    return f'{indent}<Say voice="{VOICE}">{xml_escape(text)}</Say>'


def _normal_flow_url() -> str:
    """voice_incoming again, skipping the offer. Twilio's <Redirect> re-posts
    To/From/CallSid, so the normal flow sees the call exactly as before."""
    return f"{config.webhook_base_url}/api/voice/incoming?callback=done"


def _to_normal_flow(say: str | None = None) -> Response:
    lines = []
    if say:
        lines.append(_speak(say))
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


def find_callback_person(from_number: str, db, first_offer_only: bool = True) -> dict | None:
    """The staff member this caller is probably calling back, if they can answer.

    Returns {'email', 'name'} or None. None means "don't offer", which is
    always safe — the caller just gets the number's normal flow.

    One offer per staff call: once the caller has heard it (pressed 1 or
    not), they aren't offered again until a staff member rings them again.
    The press-1 step passes first_offer_only=False, since the offer it is
    answering has just been logged.
    """
    # Withheld / anonymous / SIP callers have nothing to match on
    if not from_number or not from_number.startswith('+'):
        return None

    call = db.get_recent_outbound_call(from_number, days=CALLBACK_WINDOW_DAYS)
    if not call:
        return None
    email = call['agent_email']

    # A staff member who rang their own mobile isn't calling themselves back
    own = db.get_address_book_by_mobile(from_number)
    if own and (own.get('email') or '').lower() == email.lower():
        return None

    if first_offer_only and db.callback_offered_since(from_number, call['started_at']):
        logger.info(f"Callback from {from_number}: already offered {email} since their call — not offering")
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


def busy_callback_person(from_number: str, db) -> str | None:
    """Email of the staff member who rang this caller recently, if they are
    on another call right now - the case the press-1 offer has to skip.
    None when nobody rang them, or when we can't tell (never guess busy)."""
    if not from_number or not from_number.startswith('+'):
        return None
    call = db.get_recent_outbound_call(from_number, days=CALLBACK_WINDOW_DAYS)
    if not call:
        return None
    email = call['agent_email']
    own = db.get_address_book_by_mobile(from_number)
    if own and (own.get('email') or '').lower() == email.lower():
        return None
    try:
        busy = any((c.get('agent_email') or '').lower() == email.lower()
                   for c in db.get_active_calls())
    except Exception as e:
        logger.warning(f"Callback: couldn't check whether {email} is on a call: {e}")
        return None
    return email if busy else None


def note_if_busy(from_number: str, db) -> str | None:
    """No offer was made. If that's because the person who rang this caller
    is on another call, note it for their phone page ("rang while you were
    on a call"), shown once their call ends. Returns their email, or None."""
    email = busy_callback_person(from_number, db)
    if email:
        db.note_rang_while_busy(email, from_number)
        logger.info(f"Callback from {from_number}: noted for {email} (rang while on a call)")
    return email


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


def called_by_for_stuart(from_number: str, db) -> dict:
    """Who from the team rang this caller recently, as query params for Stuart.

    About 1 in 8 Shoalhaven callers open with "I had a missed call from you"
    (call scan, 2026-10-29), and Stuart could only say he didn't know. Same
    record and window as the press-1 offer. Tina builds Stuart's URL and
    Twilio signs it, so Rosie can trust these like staff_*. {} when nobody did.
    """
    from datetime import datetime
    import pytz
    from rinq.database.db import _parse_dt

    if not from_number or not from_number.startswith('+'):
        return {}
    call = db.get_recent_outbound_call(from_number, days=CALLBACK_WINDOW_DAYS)
    if not call:
        return {}
    email = call['agent_email']
    # A staff member who rang their own mobile isn't calling themselves back
    own = db.get_address_book_by_mobile(from_number)
    if own and (own.get('email') or '').lower() == email.lower():
        return {}

    when = ''
    try:
        tz = pytz.timezone('Australia/Sydney')
        at = _parse_dt(call['started_at']).astimezone(tz)
        days = (datetime.now(tz).date() - at.date()).days
        h = at.hour % 12 or 12
        clock = f"{h}:{at.minute:02d}{'am' if at.hour < 12 else 'pm'}"
        day = ('today' if days <= 0 else 'yesterday' if days == 1
               else f"on {at:%A} {at.day} {at:%B}")
        when = f"{day} at {clock}"
    except Exception as e:
        logger.debug(f"called_by_for_stuart: couldn't date {call.get('started_at')}: {e}")

    params = {'called_by_email': email,
              'called_by_name': _first_name(email, db) or email.split('@')[0].split('.')[0].title(),
              'called_by_when': when}
    # On another call right now: Stuart says so, rather than a bare "she'll
    # call you back". Only when we know - an unknown reads as not busy.
    if busy_callback_person(from_number, db):
        params['called_by_busy'] = '1'
    return params


def offer_twiml(person: dict, called_number: str, from_number: str) -> Response:
    """The "press 1" offer. No key falls through to the normal flow."""
    choice_url = (
        f"{config.webhook_base_url}/api/voice/callback/choice"
        f"?called={quote(called_number, safe='')}&from={quote(from_number, safe='')}"
    )
    name = person['name']
    if name:
        offer = (f'Hi, {name} from Watson Blinds tried to call you. '
                 f'Press 1 to speak to {name}, or stay on the line.')
    else:
        offer = ('Hi, someone from Watson Blinds tried to call you. '
                 'Press 1 to speak to them, or stay on the line.')
    return _twiml(
        '    <Pause length="1"/>',
        f'    <Gather numDigits="1" action="{xml_escape(choice_url)}" method="POST" timeout="{OFFER_TIMEOUT_SECONDS}">',
        _speak(offer, '        '),
        '    </Gather>',
        f'    <Redirect method="POST">{xml_escape(_normal_flow_url())}</Redirect>',
    )


def register(bp):

    @bp.route('/stuart/called-by', methods=['GET'])
    @api_or_session_auth
    def stuart_called_by():
        """Who from the team rang ?number= lately - the same answer Stuart is
        handed when a call comes in (called_by_for_stuart), for a number the
        caller GAVE him because their caller ID was hidden (#325, Derek
        2026-11-18: wherever the number came from, Stuart knows the same).
        Bot key only; {} when nobody did."""
        from rinq.services.phone import to_e164
        _, error = bot_tenant()
        if error:
            return error
        number = to_e164((request.args.get('number') or '').strip())
        return jsonify(called_by_for_stuart(number, get_db()) if number else {})

    @bp.route('/stuart/reached-since', methods=['GET'])
    @api_or_session_auth
    def stuart_reached_since():
        """Has anyone on staff spoken to or rung ?number= since ?since= (ISO,
        any offset)? Rosie asks before raising a ring-back ticket for a caller
        who hung up on Stuart's greeting (Derek 2026-11-23: "every lead is
        gold") - one who has since got through needs none.
        Bot key only. {"reached": false} or {"reached": true, "how":
        "answered"|"rang", "staff_email", "at"}."""
        from datetime import datetime, timezone
        from rinq.services.phone import to_e164
        _, error = bot_tenant()
        if error:
            return error
        number = to_e164((request.args.get('number') or '').strip())
        try:
            since = datetime.fromisoformat((request.args.get('since') or '').strip())
        except ValueError:
            return jsonify({'error': 'since must be an ISO date-time'}), 400
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if not number:
            return jsonify({'error': 'number is required'}), 400
        contact = get_db().staff_contact_since(
            number, since.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
        if not contact:
            return jsonify({'reached': False})
        return jsonify({'reached': True,
                        'how': 'answered' if contact['direction'] == 'inbound' else 'rang',
                        'staff_email': contact['agent_email'], 'at': contact['started_at']})

    @bp.route('/rang-while-busy', methods=['GET'])
    @login_required
    def rang_while_busy():
        """Callers returning MY call who rang while I was on another call,
        for the phone page. Each disappears once someone has spoken to them."""
        from rinq.services.auth import get_current_user
        user = get_current_user()
        email = getattr(user, 'email', None) if user else None
        if not email:
            return jsonify({'notes': []})
        return jsonify({'notes': get_db().get_rang_while_busy(email)})

    @bp.route('/rang-while-busy/<int:note_id>/dismiss', methods=['POST'])
    @login_required
    def rang_while_busy_dismiss(note_id):
        from rinq.services.auth import get_current_user
        user = get_current_user()
        email = getattr(user, 'email', None) if user else None
        ok = bool(email) and get_db().dismiss_rang_while_busy(note_id, email)
        return jsonify({'success': ok}), (200 if ok else 404)

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

        person = find_callback_person(from_number, db, first_offer_only=False)
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
        connecting = f"Connecting you to {name}." if name else "Connecting you now."

        db.log_activity(action="callback_ringing", target=called_number,
                        details=f"From: {from_number}, User: {email}, Targets: {len(dial_targets)}, "
                                f"Conference: {conference_name}",
                        performed_by="twilio")

        return _twiml(
            _speak(connecting),
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
