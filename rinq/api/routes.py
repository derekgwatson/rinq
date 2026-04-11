"""
API routes for Rinq (Cloud Phone System).

Provides endpoints for:
- Phone number management and forwarding
- User/extension management
- Call recording webhooks
- Voice webhooks (TwiML responses)
- Browser softphone (Twilio Client)
- Call flow handling (IVR, queues, hold music)
- Test calls
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote
from xml.sax.saxutils import escape as xml_escape

from flask import Blueprint, g, jsonify, request, Response, send_file

try:
    from shared.auth.bot_api import api_or_session_auth, get_api_caller, get_api_caller_email
except ImportError:
    from rinq.auth.decorators import api_or_session_auth, get_api_caller, get_api_caller_email
from rinq.services.twilio_service import get_twilio_service, twilio_list
from rinq.services.auth import login_required, get_current_user
from rinq.database.db import get_db
from rinq.config import config
from rinq.tenant.context import get_twilio_config

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)


from rinq.api.twiml import (
    get_full_audio_url as _get_full_audio_url,
    get_audio_url_by_type as _get_audio_url_by_type,
    say_or_play as _say_or_play_impl,
    build_reopen_twiml as _build_reopen_twiml_impl,
    build_closed_message_twiml as _build_closed_message_twiml_impl,
)
from rinq.api.identity import (
    email_to_browser_identity as _email_to_browser_identity,
    browser_identity_to_email as _browser_identity_to_email,
    normalize_staff_identifier as _normalize_staff_identifier,
)
from rinq.api.schedule import (
    get_next_open_time as _get_next_open_time,
    check_business_status as _check_business_status,
)


_build_reopen_twiml = _build_reopen_twiml_impl
_build_closed_message_twiml = _build_closed_message_twiml_impl
_say_or_play = _say_or_play_impl


def _get_ai_receptionist_url() -> str:
    """Get the base URL for AI receptionist voice/answer endpoint."""
    from rinq.integrations import get_ai_receptionist
    ai = get_ai_receptionist()
    return ai.get_answer_url() if ai else None


def _notify_ai_receptionist_call_ended(call_sid: str, call_status: str):
    """Notify the AI receptionist that a call has ended."""
    from rinq.integrations import get_ai_receptionist
    ai = get_ai_receptionist()
    if ai:
        if ai.notify_call_ended(call_sid, call_status):
            logger.info(f"Notified AI receptionist of call end: {call_sid} -> {call_status}")
        else:
            logger.warning(f"Failed to notify AI receptionist of call end {call_sid}")


def _redirect_to_ai_receptionist(response_parts, called_number, from_number, call_sid,
                        db, routing, reason="no_answer"):
    """Build TwiML that redirects the caller to AI receptionist (AI receptionist).

    Passes caller context as query params for enrichment data.
    Falls back to voicemail if the AI receptionist is not configured.
    """
    _ai_url = _get_ai_receptionist_url()
    if not _ai_url:
        logger.warning("AI receptionist not configured (AI_RECEPTIONIST_URL not set) - falling back to voicemail")
        call_flow = routing.get('call_flow') if routing else None
        return _go_to_voicemail(response_parts, call_flow, called_number, from_number, call_sid, db, routing)

    # Enrich caller data if not already done
    from rinq.services.caller_enrichment import get_enrichment_service
    enrichment = get_enrichment_service()
    caller_info = enrichment.enrich_caller(from_number)

    queue = routing.get('queue') if routing else None

    # Build query params with caller context for AI receptionist
    params = {
        'call_sid': call_sid,
        'caller_number': from_number,
        'called_number': called_number,
    }
    if queue:
        params['queue_id'] = queue.get('id', '')
        params['queue_name'] = queue.get('name', '')
    if caller_info.get('customer_id'):
        params['customer_id'] = caller_info['customer_id']
    if caller_info.get('customer_name'):
        params['customer_name'] = caller_info['customer_name']
    if caller_info.get('customer_email'):
        params['customer_email'] = caller_info['customer_email']
    if caller_info.get('order_data'):
        params['order_data'] = caller_info['order_data']
    if caller_info.get('priority'):
        params['priority'] = caller_info['priority']
    if caller_info.get('priority_reason'):
        params['priority_reason'] = caller_info['priority_reason']

    redirect_url = f"{_ai_url}?{urlencode(params)}"

    response_parts.append(f'    <Redirect method="POST">{xml_escape(redirect_url)}</Redirect>')
    response_parts.append('</Response>')

    # Update call log
    db.update_call_log(call_sid, {
        'call_type': 'ai_receptionist',
        'status': 'ai_answered',
    })

    db.log_activity(
        action="call_to_ai_receptionist",
        target=called_number,
        details=f"From: {from_number}, Reason: {reason}, Customer: {caller_info.get('customer_name') or 'Unknown'}",
        performed_by="twilio"
    )

    logger.info(f"Redirecting call {call_sid} to AI receptionist (reason: {reason})")
    return Response('\n'.join(response_parts), mimetype='application/xml')


# =============================================================================
# Account & Status
# =============================================================================

@api_bp.route('/status')
@api_or_session_auth
def status():
    """Get Twilio connection status."""
    service = get_twilio_service()

    if not service.is_configured:
        return jsonify({
            "configured": False,
            "message": "Twilio credentials not configured"
        })

    account_info = service.get_account_info()
    return jsonify({
        "configured": True,
        "account": account_info
    })


# =============================================================================
# Customer Lookup
# =============================================================================

@api_bp.route('/customers/lookup')
@api_or_session_auth
def lookup_customer():
    """Look up customer info by phone number.

    Used by the phone UI to show customer context when:
    - Receiving an inbound call
    - Dialing an outbound call

    Query params:
        phone: Phone number to look up (required)

    Returns:
        Enriched customer data including:
        - customer_id, customer_name, customer_email (from Clara)
        - order_data, priority, priority_reason (from Otto)
        - call_history with total_calls, recent_calls, last_call_date
    """
    phone = request.args.get('phone')
    if not phone:
        return jsonify({'error': 'phone parameter required'}), 400

    from rinq.services.caller_enrichment import get_enrichment_service
    enrichment = get_enrichment_service()
    result = enrichment.enrich_caller(phone)

    return jsonify(result)


# =============================================================================
# Phone Numbers
# =============================================================================

@api_bp.route('/phone-numbers')
@api_or_session_auth
def list_phone_numbers():
    """List all phone numbers from local database."""
    service = get_twilio_service()
    numbers = service.get_phone_numbers()
    return jsonify({"phone_numbers": numbers})


@api_bp.route('/phone-numbers/sync', methods=['POST'])
@api_or_session_auth
def sync_phone_numbers():
    """Sync phone numbers from Twilio to local database."""
    service = get_twilio_service()
    caller = get_api_caller()
    result = service.sync_phone_numbers(performed_by=caller)
    return jsonify(result)


@api_bp.route('/phone-numbers/configure-status-callbacks', methods=['POST'])
@api_or_session_auth
def configure_status_callbacks():
    """Set status callback URL on all Twilio phone numbers.

    Ensures Twilio sends call-ended events to Rinq so post-call processing
    works (e.g. notifying AI receptionist of call end).
    """
    service = get_twilio_service()
    caller = get_api_caller()
    result = service.configure_status_callbacks(performed_by=caller)
    return jsonify(result)


@api_bp.route('/phone-numbers/<sid>/forward', methods=['POST'])
@api_or_session_auth
def update_forwarding(sid):
    """Update forwarding number for a phone number.

    Body:
        forward_to: The phone number to forward calls to
    """
    data = request.get_json()
    forward_to = data.get('forward_to')

    if not forward_to:
        return jsonify({"error": "forward_to is required"}), 400

    service = get_twilio_service()
    caller = get_api_caller()
    result = service.update_forwarding(sid, forward_to, performed_by=caller)

    if result.get("success"):
        return jsonify(result)
    else:
        return jsonify(result), 400


# =============================================================================
# Voice Webhooks (called by Twilio - no auth required)
# =============================================================================



# Lock for all in-memory call tracking dicts (webhook threads are concurrent)
_call_tracking_lock = threading.Lock()


def _handle_participant_left(call_sid: str, db=None):
    """Mark a participant as left and auto-end lone calls.

    Called from every call-end path (status callbacks, browser signal, hangup).
    """
    if not db:
        db = get_db()
    participant = db.get_participant_by_sid(call_sid)
    db.remove_participant(call_sid)

    # Note: we don't auto-terminate lone calls server-side because
    # calls.update(status='completed') triggers after-dial TwiML which
    # can cause unexpected callbacks (e.g. blind transfer rejection flow).
    # The frontend handles auto-hangup via poll (sawMultipleParticipants).


# SIP helpers — delegated to services/sip.py
from rinq.services.sip import get_sip_domain as _get_sip_domain_impl, get_sip_uri_for_user as _get_sip_uri_for_user

def _cancel_agent_calls(customer_call_sid: str, except_call_sid: str = None):
    """Cancel all pending agent calls for a customer, optionally excluding one.

    Args:
        customer_call_sid: The customer's call SID
        except_call_sid: Optional agent call SID to NOT cancel (the one that answered)
    """
    db = get_db()
    agent_sids = db.pop_ring_attempts(customer_call_sid)
    if not agent_sids:
        logger.info(f"No agent calls found to cancel for {customer_call_sid}")
        return
    logger.info(f"Found {len(agent_sids)} agent calls to cancel for {customer_call_sid}")

    service = get_twilio_service()
    cancelled = 0

    for agent_sid in agent_sids:
        if agent_sid == except_call_sid:
            continue
        try:
            # Cancel the call by updating its status to 'completed'
            service.client.calls(agent_sid).update(status='completed')
            cancelled += 1
            logger.info(f"Cancelled agent call {agent_sid}")
        except Exception as e:
            # Call may have already ended or been answered
            logger.debug(f"Could not cancel agent call {agent_sid}: {e}")

    if cancelled > 0:
        db.log_activity(
            action="agent_calls_cancelled",
            target=customer_call_sid,
            details=f"Cancelled {cancelled} pending agent calls",
            performed_by="system"
        )


def _get_sip_domain() -> str | None:
    """Get the SIP domain. Delegates to services/sip.py."""
    return _get_sip_domain_impl()


def _ring_agents_for_queue(queue_id: int, queue_name: str, customer_caller_id: str, our_caller_id: str, customer_call_sid: str, base_url: str = None):
    """Initiate outbound calls to all agents when a caller enters the queue.

    This implements "queue with auto-ring" - callers wait in queue with hold music
    while agents' phones ring simultaneously. First agent to answer gets connected.

    Args:
        queue_id: The queue ID
        queue_name: The queue name (for logging)
        customer_caller_id: The customer's phone number (used for SIP so agents see who's calling)
        our_caller_id: Our Twilio number (required for mobile calls - can only use numbers we own)
        customer_call_sid: The call SID of the customer waiting in queue
    """
    import threading

    # Capture tenant-scoped resources while we still have Flask request context —
    # the background thread won't have access to flask.g
    sip_domain = _get_sip_domain()
    db = get_db()

    def ring_agents():
        try:
            service = get_twilio_service()

            # Get queue members
            members = db.get_queue_members(queue_id)
            active_members = [m for m in members if m.get('is_active')]

            if not active_members:
                logger.warning(f"No active members in queue {queue_name} to ring")
                return

            # URL that agent calls will hit when answered
            answer_url = f"{base_url}/api/voice/queue/{queue_id}/agent-answer?customer_call_sid={customer_call_sid}"

            # Status callback URL for rejection/no-answer handling
            status_callback_url = f"{base_url}/api/voice/queue/{queue_id}/agent-ring-status?customer_call_sid={customer_call_sid}"

            calls_initiated = 0
            agent_call_sids = []  # Track for cancellation
            metadata_by_sid = {}  # Reverse mapping metadata per SID

            for member in active_members:
                user_email = member['user_email']

                # Get user's ring settings (includes DND check)
                ring_settings = db.get_user_ring_settings(user_email)

                # SIP devices (desk phone, Zoiper, etc.)
                # Browser is handled via Twilio Client push notifications, not here
                if ring_settings.get('ring_sip', True) and sip_domain:
                    user = db.get_user_by_email(user_email)
                    sip_uri = f"sip:{user['username']}@{sip_domain}" if user and user.get('username') else None

                    if sip_uri:
                        try:
                            # For SIP (internal to Twilio), we can use the customer's
                            # number so the agent sees who's calling on their desk phone
                            # and mobile softphone (Zoiper)
                            call = service.client.calls.create(
                                to=sip_uri,
                                from_=customer_caller_id,
                                url=answer_url,
                                timeout=30,
                                status_callback=status_callback_url,
                                status_callback_event=['initiated', 'ringing', 'answered', 'completed']
                            )
                            logger.info(f"Initiated SIP call to {sip_uri} for queue {queue_name}: {call.sid}")
                            db.stamp_sip_activity(user_email)
                            agent_call_sids.append(call.sid)
                            # Store reverse mapping metadata for rejection handling
                            metadata_by_sid[call.sid] = json.dumps({
                                'customer_call_sid': customer_call_sid,
                                'queue_id': queue_id,
                                'user_email': user_email,
                                'device_type': 'sip'
                            })
                            calls_initiated += 1
                        except Exception as e:
                            logger.error(f"Failed to call SIP device for {user_email}: {e}")

            # Store agent call SIDs in DB for cancellation (shared across workers)
            if agent_call_sids:
                db.store_ring_attempts(customer_call_sid, agent_call_sids, 'queue',
                                       metadata_by_sid=metadata_by_sid)
                logger.info(f"Stored {len(agent_call_sids)} agent calls for customer {customer_call_sid}")

            db.log_activity(
                action="agents_ringing",
                target=f"queue_{queue_id}",
                details=f"Initiated {calls_initiated} outbound SIP calls for queue {queue_name}",
                performed_by="system"
            )

        except Exception as e:
            logger.exception(f"Error ringing agents for queue {queue_id}: {e}")

    # Run in background thread to not block the webhook response
    thread = threading.Thread(target=ring_agents, daemon=True)
    thread.start()


def _ring_targets_into_conference(dial_targets: list, conference_name: str,
                                   caller_id: str, caller_call_sid: str,
                                   base_url: str = None, caller_identity: str = None,
                                   db=None):
    """Ring multiple devices and have the first to answer join a conference.

    Similar to _ring_agents_for_queue but for conference-first direct
    inbound calls. Runs in a background thread.

    Args:
        dial_targets: List of TwiML target strings ('<Client>...', '<Sip>...', '<Number>...')
        conference_name: Conference room name to join
        caller_id: Caller ID to show on ringing phones
        caller_call_sid: The caller's call SID (for status callbacks)
        base_url: Webhook base URL (must be passed from request context)
        db: Database instance (must be captured from request context before spawning)
    """
    import threading

    def ring_targets():
        try:
            service = get_twilio_service()

            answer_url = f"{base_url}/api/voice/conference/join?room={conference_name}&role=agent"
            status_url = (
                f"{base_url}/api/voice/inbound/ring-status"
                f"?conference={conference_name}&caller_call_sid={caller_call_sid}"
            )

            call_sids = []

            for target in dial_targets:
                to_addr = None
                if '<Client>' in target:
                    # Extract identity: <Client>foo_bar</Client> -> client:foo_bar
                    identity = target.split('<Client>')[1].split('</Client>')[0].strip()
                    # Handle multi-line <Client> with <Identity>
                    if '<Identity>' in target:
                        identity = target.split('<Identity>')[1].split('</Identity>')[0].strip()
                    to_addr = f"client:{identity}"
                elif '<Sip>' in target:
                    to_addr = target.split('<Sip>')[1].split('</Sip>')[0].strip()
                elif '<Number>' in target:
                    to_addr = target.split('<Number>')[1].split('</Number>')[0].strip()

                if not to_addr:
                    continue

                try:
                    # For browser clients, use the caller's identity so
                    # resolveInternalCaller shows their name
                    # For PSTN/SIP, must use a number we own
                    call_from = caller_id
                    if to_addr.startswith('client:') and caller_identity:
                        call_from = caller_identity
                    elif to_addr.startswith('+') or to_addr[0].isdigit():
                        # PSTN call — use tenant's default or first owned number
                        call_from = get_twilio_config('twilio_default_caller_id')
                        if not call_from:
                            try:
                                owned = twilio_list(service.client.incoming_phone_numbers, limit=1)
                                if owned:
                                    call_from = owned[0].phone_number
                            except Exception as e:
                                logger.warning(f"Failed to list owned numbers for caller ID fallback: {e}")
                        if not call_from:
                            call_from = caller_id  # Last resort

                    call = service.client.calls.create(
                        to=to_addr,
                        from_=call_from,
                        url=answer_url,
                        timeout=30,
                        status_callback=status_url,
                        status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
                    )
                    call_sids.append(call.sid)
                    logger.info(f"Ringing {to_addr} for conference {conference_name}: {call.sid}")
                except Exception as e:
                    logger.error(f"Failed to ring {to_addr}: {e}")

            if call_sids:
                db.store_ring_attempts(conference_name, call_sids, 'conference')
                logger.info(f"Stored {len(call_sids)} ring calls for conference {conference_name}")
            else:
                # No targets could be rung — end the conference so caller falls through
                try:
                    confs = twilio_list(service.client.conferences,
                        friendly_name=conference_name, status='in-progress', limit=1
                    )
                    if confs:
                        service.client.conferences(confs[0].sid).update(status='completed')
                except Exception as e:
                    logger.warning(f"Failed to clean up empty conference {conference_name}: {e}")

        except Exception as e:
            logger.exception(f"Error ringing targets for conference {conference_name}: {e}")

    thread = threading.Thread(target=ring_targets, daemon=True)
    thread.start()




def _build_dial_targets(routing: dict) -> list[str]:
    """Build list of TwiML dial targets from routing info.

    Includes browser, SIP, and mobile forwarding targets for each user.
    Call forwarding (forward_to on staff_extensions) is always included
    when set — for simultaneous ring scenarios the 'always'/'no_answer'
    distinction doesn't apply; we want maximum reachability.

    Args:
        routing: Dict with 'queue' (containing 'members'), 'assignments' (list of emails),
                 and 'user_settings'

    Returns:
        List of TwiML elements like '<Client>identity</Client>', '<Sip>...</Sip>',
        or '<Number>+614...</Number>'
    """
    targets = []
    seen_emails = set()

    user_settings = routing.get('user_settings', {})
    db = get_db()

    # Get SIP domain once (cached)
    sip_domain = None

    def _add_user_targets(user_email):
        nonlocal sip_domain
        if user_email in seen_emails:
            return
        seen_emails.add(user_email)

        # Ring settings include DND check — if DND is on, both are False
        settings = db.get_user_ring_settings(user_email)
        if settings.get('dnd'):
            logger.info(f"Skipping {user_email} - DND enabled")
            return

        # Browser softphone
        if settings.get('ring_browser', True):
            identity = _email_to_browser_identity(user_email)
            targets.append(f'<Client>{identity}</Client>')

        # SIP devices (desk phone, Zoiper, etc.)
        if settings.get('ring_sip', True):
            if sip_domain is None:
                sip_domain = _get_sip_domain()

            if sip_domain:
                sip_uri = _get_sip_uri_for_user(user_email, sip_domain)
                if sip_uri:
                    targets.append(f'<Sip>{sip_uri}</Sip>')
                    logger.info(f"Added SIP target for {user_email}: {sip_uri}")
                    db.stamp_sip_activity(user_email)
                else:
                    logger.warning(f"Could not build SIP URI for {user_email} - no SIP credentials found")

        # Mobile forwarding (from staff_extensions)
        ext = db.get_staff_extension(user_email)
        if ext and ext.get('forward_to'):
            targets.append(f'<Number>{xml_escape(ext["forward_to"])}</Number>')
            logger.info(f"Added mobile forward target for {user_email}: {ext['forward_to']}")

    # Queue members (call flow routing)
    queue = routing.get('queue')
    if queue:
        for member in queue.get('members', []):
            if member.get('is_active'):
                _add_user_targets(member.get('user_email'))

    # Direct assignments (phone_assignments table)
    for email in routing.get('assignments', []):
        _add_user_targets(email)

    return targets


@api_bp.route('/voice/incoming', methods=['POST'])
def voice_incoming():
    """Handle incoming voice calls - returns TwiML.

    Twilio calls this webhook when a call comes in to one of our numbers.

    Call flow:
    1. Look up phone number and its call flow
    2. Play greeting if configured
    3. Check schedule (business hours, holidays)
    4. If OPEN: Route to queue or forward number
    5. If CLOSED: Play closed message or take voicemail

    No auth required - Twilio needs to call this directly.
    """
    called_number = request.form.get('To')
    from_number = request.form.get('From')
    call_sid = request.form.get('CallSid')

    db = get_db()

    # Get full routing info for this number
    routing = db.get_call_routing(called_number)

    if not routing:
        logger.warning(f"No routing found for {called_number}")
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, this number is not configured. Please try again later.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    phone = routing.get('phone', {})
    call_flow = routing.get('call_flow')
    schedule = routing.get('schedule')
    queue = routing.get('queue')

    # Log the inbound call immediately - captures ALL inbound calls
    db.log_call({
        'call_sid': call_sid,
        'direction': 'inbound',
        'from_number': from_number,
        'to_number': called_number,
        'phone_number_id': phone.get('sid'),
        'call_flow_id': call_flow.get('id') if call_flow else None,
        'queue_id': queue.get('id') if queue else None,
        'queue_name': queue.get('name') if queue else None,
        'status': 'ringing',
    })

    # Start building TwiML response
    response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']

    # Brief pause to allow audio path to establish (helps with VoIP forwarding)
    response_parts.append('    <Pause length="1"/>')

    # Play greeting if configured
    if call_flow and call_flow.get('greeting_audio_id'):
        greeting = db.get_audio_file(call_flow['greeting_audio_id'])
        if greeting and greeting.get('file_url'):
            audio_url = _get_full_audio_url(greeting['file_url'])
            response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')

    # Check business status (includes holiday detection)
    status = _check_business_status(schedule)
    is_open = status['is_open']
    matched_holiday = status.get('matched_holiday')

    if is_open:
        # === OPEN: Route to queue, forward, or direct assignments ===

        # If no call flow but has direct assignments, ring them via conference
        if not call_flow and routing.get('assignments'):
            dial_targets = _build_dial_targets(routing)
            if dial_targets:
                conference_name = f"call_{call_sid}"
                db.set_call_conference(call_sid, conference_name)

                no_answer_url = (
                    f"{config.webhook_base_url}/api/voice/inbound/no-answer"
                    f"?call_sid={call_sid}&called={quote(called_number, safe='')}&from={quote(from_number, safe='')}"
                )
                response_parts.append('    <Dial>')
                response_parts.append(f'        <Conference startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">{xml_escape(conference_name)}</Conference>')
                response_parts.append('    </Dial>')
                response_parts.append(f'    <Redirect>{xml_escape(no_answer_url)}</Redirect>')

                get_twilio_service().capture_for_thread()
                _ring_targets_into_conference(dial_targets, conference_name, called_number, call_sid, base_url=config.webhook_base_url, db=db)

                db.log_activity(
                    action="call_direct_assignment",
                    target=called_number,
                    details=f"From: {from_number}, Assigned users: {', '.join(routing['assignments'])}, Conference: {conference_name}",
                    performed_by="twilio"
                )
            else:
                response_parts.append('    <Say>Sorry, no one is available to take your call right now.</Say>')
                response_parts.append('    <Hangup/>')

        elif not call_flow:
            response_parts.append('    <Say>Sorry, no one is available to take your call right now.</Say>')
            response_parts.append('    <Hangup/>')

        elif call_flow.get('open_action') == 'forward' and call_flow.get('open_forward_number'):
            # Simple forward to a number
            forward_to = call_flow['open_forward_number']
            dial_status_url = f"{config.webhook_base_url}/api/voice/dial-status"
            response_parts.append(f'    <Dial callerId="{xml_escape(called_number)}" timeout="30" action="{xml_escape(dial_status_url)}">')
            response_parts.append(f'        <Number>{xml_escape(forward_to)}</Number>')
            response_parts.append('    </Dial>')

        elif call_flow.get('open_action') == 'message' and call_flow.get('open_audio_id'):
            # Play a message
            audio = db.get_audio_file(call_flow['open_audio_id'])
            if audio and audio.get('file_url'):
                audio_url = _get_full_audio_url(audio['file_url'])
                response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')
            response_parts.append('    <Hangup/>')

        elif call_flow.get('open_action') == 'extension_directory':
            # Auto-attendant: prompt caller to enter an extension
            call_flow_id = call_flow.get('id') or (routing.get('call_flow', {}).get('id') if routing.get('call_flow') else None)
            gather_url = f"{config.webhook_base_url}/api/voice/extension-dial?called={quote(called_number, safe='')}&from={quote(from_number, safe='')}&flow_id={call_flow_id}&attempt=1"
            response_parts.append(f'    <Gather numDigits="4" action="{xml_escape(gather_url)}" method="POST" timeout="10">')

            if call_flow.get('extension_prompt_audio_id'):
                audio = db.get_audio_file(call_flow['extension_prompt_audio_id'])
                if audio and audio.get('file_url'):
                    audio_url = _get_full_audio_url(audio['file_url'])
                    response_parts.append(f'        <Play>{xml_escape(audio_url)}</Play>')
                else:
                    response_parts.append(_say_or_play('ext_prompt', 'Please enter the extension of the person you are trying to reach.', indent='        '))
            else:
                response_parts.append(_say_or_play('ext_prompt', 'Please enter the extension of the person you are trying to reach.', indent='        '))

            response_parts.append('    </Gather>')
            # No input fallback
            response_parts.append('    <Say voice="Polly.Nicole">No extension entered. Goodbye.</Say>')
            response_parts.append('    <Hangup/>')

            db.log_activity(
                action="call_extension_directory",
                target=called_number,
                details=f"From: {from_number}, Prompting for extension",
                performed_by="twilio"
            )

        elif call_flow.get('open_action') == 'dial' and queue:
            # Simultaneous ring - dial all queue members' devices at once
            dial_targets = _build_dial_targets(routing)

            if dial_targets:
                conference_name = f"call_{call_sid}"
                db.set_call_conference(call_sid, conference_name)

                flow_id = call_flow.get('id', '') if call_flow else ''
                no_answer_url = (
                    f"{config.webhook_base_url}/api/voice/inbound/no-answer"
                    f"?call_sid={call_sid}&called={quote(called_number, safe='')}"
                    f"&from={quote(from_number, safe='')}&flow_id={flow_id}"
                )
                response_parts.append('    <Dial>')
                response_parts.append(f'        <Conference startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">{xml_escape(conference_name)}</Conference>')
                response_parts.append('    </Dial>')
                response_parts.append(f'    <Redirect>{xml_escape(no_answer_url)}</Redirect>')

                get_twilio_service().capture_for_thread()
                _ring_targets_into_conference(dial_targets, conference_name, called_number, call_sid, base_url=config.webhook_base_url, db=db)

                db.log_activity(
                    action="call_dialing_agents",
                    target=called_number,
                    details=f"From: {from_number}, Queue: {queue.get('name')}, Targets: {len(dial_targets)}, Conference: {conference_name}",
                    performed_by="twilio"
                )
            else:
                # No active devices - check call flow no-answer action
                no_answer = call_flow.get('open_no_answer_action', 'ai_receptionist') if call_flow else 'ai_receptionist'
                logger.warning(f"No dial targets for queue {queue.get('name')} - action: {no_answer}")
                if no_answer == 'ai_receptionist':
                    return _redirect_to_ai_receptionist(
                        response_parts, called_number, from_number, call_sid, db, routing,
                        reason="no_dial_targets"
                    )
                else:
                    return _go_to_voicemail(
                        response_parts, call_flow, called_number, from_number, call_sid, db, routing
                    )

        elif call_flow.get('open_action', 'queue') == 'queue' and queue:
            # Put caller directly in queue - agents pick up from dashboard
            # Check if any queue members are active AND reachable (not DND)
            members = db.get_queue_members(queue['id'])
            active_members = [m for m in members if m.get('is_active')]
            available_members = []
            for m in active_members:
                settings = db.get_user_ring_settings(m['user_email'])
                if settings.get('dnd'):
                    continue
                if settings.get('ring_browser', True) or settings.get('ring_sip', True):
                    available_members.append(m)

            if available_members:
                # Agents are on duty - put caller in queue for them to pick up
                # The caller will be added to DB when they enter the queue (in queue_wait)
                queue_name = f"queue_{queue['id']}"
                wait_url = f"{config.webhook_base_url}/api/voice/queue/{queue['id']}/wait"
                leave_url = f"{config.webhook_base_url}/api/voice/queue/{queue['id']}/leave"

                db.log_activity(
                    action="call_entering_queue",
                    target=called_number,
                    details=f"From: {from_number}, Queue: {queue.get('name')}",
                    performed_by="twilio"
                )

                # Go straight to hold music - no "busy" message
                response_parts.append(f'    <Enqueue waitUrl="{xml_escape(wait_url)}" waitUrlMethod="POST" action="{xml_escape(leave_url)}">{xml_escape(queue_name)}</Enqueue>')
            else:
                # No active members on duty - check call flow no-answer action
                no_answer = call_flow.get('open_no_answer_action', 'ai_receptionist') if call_flow else 'ai_receptionist'
                logger.warning(f"No active queue members for queue {queue.get('name')} - action: {no_answer}")
                db.log_activity(
                    action="call_no_agents",
                    target=called_number,
                    details=f"From: {from_number}, Queue: {queue.get('name')} has no active members",
                    performed_by="twilio"
                )
                if no_answer == 'ai_receptionist':
                    return _redirect_to_ai_receptionist(
                        response_parts, called_number, from_number, call_sid, db, routing,
                        reason="no_active_members"
                    )
                else:
                    return _go_to_voicemail(
                        response_parts, call_flow, called_number, from_number, call_sid, db, routing,
                        reason='no_answer'
                    )

        else:
            # Fallback: direct dial to queue members/legacy forward
            dial_targets = _build_dial_targets(routing)

            # Also check legacy forward_to
            if phone.get('forward_to'):
                dial_targets.append(f'<Number>{xml_escape(phone["forward_to"])}</Number>')

            if dial_targets:
                timeout = queue.get('ring_timeout', 30) if queue else 30
                dial_status_url = f"{config.webhook_base_url}/api/voice/dial-status"
                response_parts.append(f'    <Dial callerId="{xml_escape(called_number)}" timeout="{timeout}" action="{xml_escape(dial_status_url)}">')
                for target in dial_targets:
                    response_parts.append(f'        {target}')
                response_parts.append('    </Dial>')
            else:
                response_parts.append('    <Say>Sorry, no one is available to take your call. Please try again later.</Say>')
                response_parts.append('    <Hangup/>')
    else:
        # === CLOSED: Play message or take voicemail ===
        action = call_flow.get('closed_action', 'message') if call_flow else 'message'
        next_open = status.get('next_open')

        # Parse message parts if configured (JSON string from DB)
        message_parts = None
        if call_flow and call_flow.get('closed_message_parts'):
            try:
                raw = call_flow['closed_message_parts']
                message_parts = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Invalid closed_message_parts JSON for call flow {call_flow.get('id')}")

        # Helper to build the closed audio + reopen sequence
        # When message_parts is set (and no holiday override), use parts system.
        # Otherwise fall back to single audio + reopen appended at end.
        def _build_closed_sequence():
            """Return list of TwiML parts for the closed message."""
            # Holiday audio always overrides message parts
            if matched_holiday and matched_holiday.get('audio_id'):
                holiday_audio = db.get_audio_file(matched_holiday['audio_id'])
                if holiday_audio and holiday_audio.get('file_url'):
                    logger.info(f"Using holiday audio: {holiday_audio.get('name')} for {matched_holiday.get('name')}")
                    audio_url = _get_full_audio_url(holiday_audio['file_url'])
                    return [f'    <Play>{xml_escape(audio_url)}</Play>']

            # Message parts: full control over sequence including where reopen info appears
            if message_parts:
                parts = _build_closed_message_twiml(message_parts, next_open, db)
                if parts:
                    return parts

            # Fallback: single closed audio (reopen info only via message sequence)
            if call_flow and call_flow.get('closed_audio_id'):
                closed_audio = db.get_audio_file(call_flow['closed_audio_id'])
                if closed_audio and closed_audio.get('file_url'):
                    audio_url = _get_full_audio_url(closed_audio['file_url'])
                    return [f'    <Play>{xml_escape(audio_url)}</Play>']

            return None  # No custom audio at all

        if action == 'forward' and call_flow.get('closed_forward_number'):
            # After hours forward
            forward_to = call_flow['closed_forward_number']
            dial_status_url = f"{config.webhook_base_url}/api/voice/dial-status"
            response_parts.append(f'    <Dial callerId="{xml_escape(called_number)}" timeout="30" action="{xml_escape(dial_status_url)}">')
            response_parts.append(f'        <Number>{xml_escape(forward_to)}</Number>')
            response_parts.append('    </Dial>')

        elif action == 'ai_receptionist':
            # Play closed message, then redirect to AI receptionist
            closed_seq = _build_closed_sequence()
            if closed_seq:
                response_parts.extend(closed_seq)

            return _redirect_to_ai_receptionist(
                response_parts, called_number, from_number, call_sid, db, routing,
                reason="closed"
            )

        elif action == 'voicemail':
            # Play closed message, then record voicemail
            closed_seq = _build_closed_sequence()
            if closed_seq:
                response_parts.extend(closed_seq)
            else:
                response_parts.append('    <Say>We are currently closed. Please leave a message after the tone.</Say>')

            # Record voicemail
            voicemail_url = f"{config.webhook_base_url}/api/voice/voicemail"
            transcription_url = f"{config.webhook_base_url}/api/voice/transcription"
            response_parts.append(f'    <Record maxLength="120" action="{xml_escape(voicemail_url)}" transcribe="true" transcribeCallback="{xml_escape(transcription_url)}" />')

        else:
            # Just play closed message
            closed_seq = _build_closed_sequence()
            if closed_seq:
                response_parts.extend(closed_seq)
            else:
                response_parts.append('    <Say>We are currently closed. Please call back during business hours.</Say>')
            response_parts.append('    <Hangup/>')

    response_parts.append('</Response>')

    twiml = '\n'.join(response_parts)

    # Log the call
    details = f"From: {from_number}, Open: {is_open}, Flow: {call_flow.get('name') if call_flow else 'legacy'}"
    if matched_holiday:
        details += f", Holiday: {matched_holiday.get('name')}"
    db.log_activity(
        action="incoming_call",
        target=called_number,
        details=details,
        performed_by="twilio"
    )

    return Response(twiml, mimetype='application/xml')


# =============================================================================
# Test Line - Simulate calls to any number
# =============================================================================

def _format_phone_for_speech(phone_number: str) -> str:
    """Format a phone number for TTS. Delegates to phone utility."""
    from rinq.services.phone import format_for_speech
    return format_for_speech(phone_number)


@api_bp.route('/voice/test-menu', methods=['POST'])
def voice_test_menu():
    """Test line menu - simulate calls to different phone numbers.

    This endpoint is the webhook for a dedicated test phone number.
    It presents a menu of all configured phone numbers and lets the
    caller simulate calling any of them.

    The test number's webhook should be set to:
        {webhook_base_url}/api/voice/test-menu

    No auth required - Twilio calls this directly.
    """
    digits = request.form.get('Digits', '')
    from_number = request.form.get('From', '')
    call_sid = request.form.get('CallSid', '')

    db = get_db()

    # Get all phone numbers (show all, not just ones with call flows)
    phones = db.get_phone_numbers()

    # Build mapping: digit -> phone number
    phone_map = {}
    for i, phone in enumerate(phones[:9], start=1):  # Max 9 options
        phone_map[str(i)] = phone

    # Special options (only if we have phones to test with)
    if phones:
        phone_map['0'] = {'special': 'after_hours', 'label': 'Force after-hours mode'}

    if digits and digits in phone_map:
        selected = phone_map[digits]

        if selected.get('special') == 'after_hours':
            # Simulate after-hours - pick first number and force closed
            if phones:
                simulated_number = phones[0]['phone_number']
                logger.info(f"Test call: simulating after-hours for {simulated_number}")
                return _handle_incoming_call_internal(
                    called_number=simulated_number,
                    from_number=from_number,
                    call_sid=call_sid,
                    force_closed=True,
                    is_test=True
                )
            else:
                twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>No phone numbers are configured. Goodbye.</Say>
    <Hangup/>
</Response>'''
                return Response(twiml, mimetype='application/xml')
        else:
            # Simulate call to selected number
            simulated_number = selected['phone_number']
            logger.info(f"Test call: simulating call to {simulated_number} ({selected.get('friendly_name', 'Unknown')})")
            return _handle_incoming_call_internal(
                called_number=simulated_number,
                from_number=from_number,
                call_sid=call_sid,
                force_closed=False,
                is_test=True
            )

    # Build menu TwiML
    menu_options = []
    for digit, phone in sorted(phone_map.items()):
        if phone.get('special'):
            menu_options.append(f"Press {digit} for {phone['label']}.")
        else:
            # Use friendly name if available, otherwise format phone number for speech
            name = phone.get('friendly_name')
            if name:
                menu_options.append(f"Press {digit} for {name}.")
            else:
                spoken_number = _format_phone_for_speech(phone.get('phone_number', ''))
                menu_options.append(f"Press {digit} for {spoken_number}.")

    menu_text = " ".join(menu_options)

    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather numDigits="1" action="{config.webhook_base_url}/api/voice/test-menu" method="POST">
        <Say voice="alice">Welcome to the {config.product_name} test line. {menu_text}</Say>
    </Gather>
    <Say>No selection made. Goodbye.</Say>
    <Hangup/>
</Response>'''

    db.log_activity(
        action="test_call_menu",
        target=from_number,
        details=f"Test menu presented with {len(phones)} phone options",
        performed_by="twilio"
    )

    return Response(twiml, mimetype='application/xml')


def _handle_incoming_call_internal(called_number: str, from_number: str, call_sid: str,
                                    force_closed: bool = False, is_test: bool = False):
    """Internal handler for incoming calls - used by both real and test calls.

    Args:
        called_number: The number that was "called" (or simulated)
        from_number: The caller's number
        call_sid: Twilio call SID
        force_closed: If True, treat as after-hours regardless of schedule
        is_test: If True, this is a test call (for logging)

    Returns:
        TwiML Response
    """
    db = get_db()

    # Get full routing info for this number
    routing = db.get_call_routing(called_number)

    if not routing:
        logger.warning(f"No routing found for {called_number}")
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, this number is not configured. Please try again later.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    phone = routing.get('phone', {})
    call_flow = routing.get('call_flow')
    schedule = routing.get('schedule')
    queue = routing.get('queue')

    # Start building TwiML response
    response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']

    # For test calls, announce what we're simulating
    if is_test:
        name = phone.get('friendly_name') or called_number
        response_parts.append(f'    <Say voice="alice">Test mode: Simulating call to {xml_escape(name)}.</Say>')
        response_parts.append('    <Pause length="1"/>')

    # Play greeting if configured
    if call_flow and call_flow.get('greeting_audio_id'):
        greeting = db.get_audio_file(call_flow['greeting_audio_id'])
        if greeting and greeting.get('file_url'):
            audio_url = _get_full_audio_url(greeting['file_url'])
            response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')

    # Check business status (includes holiday detection)
    if force_closed:
        is_open = False
        matched_holiday = None
        status = {'is_open': False, 'reason': 'forced_closed_for_test'}
    else:
        status = _check_business_status(schedule)
        is_open = status['is_open']
        matched_holiday = status.get('matched_holiday')

    if is_open:
        # === OPEN: Route to queue, forward, or direct assignments ===

        # If no call flow but has direct assignments, dial them directly
        if not call_flow and routing.get('assignments'):
            dial_targets = _build_dial_targets(routing)
            if dial_targets:
                dial_status_url = f"{config.webhook_base_url}/api/voice/dial-status"
                status_callback = f"{config.webhook_base_url}/api/voice/agent-status"
                response_parts.append(f'    <Dial callerId="{xml_escape(called_number)}" timeout="30" action="{xml_escape(dial_status_url)}">')
                for target in dial_targets:
                    if '<Number>' in target:
                        target = target.replace('<Number>', f'<Number statusCallback="{xml_escape(status_callback)}" statusCallbackEvent="completed">')
                    response_parts.append(f'        {target}')
                response_parts.append('    </Dial>')

                db.log_activity(
                    action="call_direct_assignment" + ("_test" if is_test else ""),
                    target=called_number,
                    details=f"From: {from_number}, Assigned users: {', '.join(routing['assignments'])}",
                    performed_by="twilio"
                )
            else:
                response_parts.append('    <Say>Sorry, no one is available to take your call right now.</Say>')
                response_parts.append('    <Hangup/>')

        # Call flow actions
        elif not call_flow:
            response_parts.append('    <Say>Sorry, no one is available to take your call right now.</Say>')
            response_parts.append('    <Hangup/>')

        elif call_flow.get('open_action') == 'forward' and call_flow.get('open_forward_number'):
            forward_to = call_flow['open_forward_number']
            dial_status_url = f"{config.webhook_base_url}/api/voice/dial-status"
            response_parts.append(f'    <Dial callerId="{xml_escape(called_number)}" timeout="30" action="{xml_escape(dial_status_url)}">')
            response_parts.append(f'        <Number>{xml_escape(forward_to)}</Number>')
            response_parts.append('    </Dial>')

        elif call_flow.get('open_action') == 'message' and call_flow.get('open_audio_id'):
            audio = db.get_audio_file(call_flow['open_audio_id'])
            if audio and audio.get('file_url'):
                audio_url = _get_full_audio_url(audio['file_url'])
                response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')
            response_parts.append('    <Hangup/>')

        elif call_flow.get('open_action') == 'extension_directory':
            # Auto-attendant: prompt caller to enter an extension
            call_flow_id = call_flow.get('id') if call_flow else None
            gather_url = f"{config.webhook_base_url}/api/voice/extension-dial?called={quote(called_number, safe='')}&from={quote(from_number, safe='')}&flow_id={call_flow_id}&attempt=1"
            response_parts.append(f'    <Gather numDigits="4" action="{xml_escape(gather_url)}" method="POST" timeout="10">')

            if call_flow.get('extension_prompt_audio_id'):
                audio = db.get_audio_file(call_flow['extension_prompt_audio_id'])
                if audio and audio.get('file_url'):
                    audio_url = _get_full_audio_url(audio['file_url'])
                    response_parts.append(f'        <Play>{xml_escape(audio_url)}</Play>')
                else:
                    response_parts.append(_say_or_play('ext_prompt', 'Please enter the extension of the person you are trying to reach.', indent='        '))
            else:
                response_parts.append(_say_or_play('ext_prompt', 'Please enter the extension of the person you are trying to reach.', indent='        '))

            response_parts.append('    </Gather>')
            response_parts.append('    <Say voice="Polly.Nicole">No extension entered. Goodbye.</Say>')
            response_parts.append('    <Hangup/>')

            db.log_activity(
                action="call_extension_directory",
                target=called_number,
                details=f"From: {from_number}, Prompting for extension",
                performed_by="twilio"
            )

        elif call_flow.get('open_action', 'queue') == 'queue' and queue:
            dial_targets = _build_dial_targets(routing)

            if dial_targets:
                timeout = queue.get('ring_timeout', 30)
                no_answer_url = f"{config.webhook_base_url}/api/voice/queue/{queue['id']}/no-answer"
                status_callback = f"{config.webhook_base_url}/api/voice/agent-status"
                response_parts.append(f'    <Dial callerId="{xml_escape(called_number)}" timeout="{timeout}" action="{xml_escape(no_answer_url)}">')
                for target in dial_targets:
                    if '<Number>' in target:
                        target = target.replace('<Number>', f'<Number statusCallback="{xml_escape(status_callback)}" statusCallbackEvent="completed">')
                    response_parts.append(f'        {target}')
                response_parts.append('    </Dial>')

                db.log_activity(
                    action="call_dialing_queue" + ("_test" if is_test else ""),
                    target=called_number,
                    details=f"From: {from_number}, Queue: {queue.get('name')}, Members: {len(dial_targets)}",
                    performed_by="twilio"
                )
            else:
                no_answer = call_flow.get('open_no_answer_action', 'ai_receptionist') if call_flow else 'ai_receptionist'
                logger.warning(f"No active queue members for queue {queue.get('name')} - action: {no_answer}")
                if no_answer == 'ai_receptionist':
                    return _redirect_to_ai_receptionist(
                        response_parts, called_number, from_number, call_sid, db, routing,
                        reason="no_active_members"
                    )
                else:
                    return _go_to_voicemail(
                        response_parts, call_flow, called_number, from_number, call_sid, db, routing,
                        reason='no_answer'
                    )
        else:
            response_parts.append('    <Say>Sorry, no one is available to take your call right now.</Say>')
            response_parts.append('    <Hangup/>')
    else:
        # === CLOSED ===
        return _handle_closed_call(
            response_parts, call_flow, schedule, matched_holiday,
            called_number, from_number, call_sid, db, routing, is_test,
            next_open=status.get('next_open')
        )

    response_parts.append('</Response>')
    twiml = '\n'.join(response_parts)

    # Log the call
    details = f"From: {from_number}, Status: {'OPEN' if is_open else 'CLOSED'}"
    if is_test:
        details = f"[TEST] {details}"
    if matched_holiday:
        details += f", Holiday: {matched_holiday.get('name')}"
    db.log_activity(
        action="incoming_call" + ("_test" if is_test else ""),
        target=called_number,
        details=details,
        performed_by="twilio"
    )

    return Response(twiml, mimetype='application/xml')


def _go_to_voicemail(response_parts, call_flow, called_number, from_number, call_sid, db, routing,
                     reason='closed', audio_type=None):
    """Helper to route a call to voicemail.

    This is the single path all voicemail recordings go through.

    Args:
        reason: 'closed' (after hours) or 'no_answer' (open hours, no one available)
        audio_type: Override audio type for _say_or_play (e.g. 'queue_no_agents', 'voicemail_escape').
                    If None, uses call_flow-configured audio.
    """
    # Check if the call_flow has a voicemail destination configured
    vm_dest = None
    if call_flow:
        if call_flow.get('voicemail_destination_id'):
            vm_dest = db.get_voicemail_destination(call_flow['voicemail_destination_id'])
        elif call_flow.get('voicemail_email'):
            vm_dest = db.get_voicemail_destination_by_email(call_flow['voicemail_email'])

    if not vm_dest and not audio_type:
        # No voicemail configured and no fallback audio — can't record
        response_parts.append('    <Say voice="Polly.Nicole">Please try again later. Goodbye.</Say>')
        response_parts.append('    <Hangup/>')
        response_parts.append('</Response>')
        return Response('\n'.join(response_parts), mimetype='application/xml')

    # Play appropriate prompt
    prompt_added = False
    if reason == 'closed' and call_flow and call_flow.get('closed_audio_id'):
        audio = db.get_audio_file(call_flow['closed_audio_id'])
        if audio and audio.get('file_url'):
            audio_url = _get_full_audio_url(audio['file_url'])
            response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')
            prompt_added = True
    elif reason == 'no_answer':
        if call_flow and call_flow.get('no_answer_audio_id'):
            audio = db.get_audio_file(call_flow['no_answer_audio_id'])
            if audio and audio.get('file_url'):
                audio_url = _get_full_audio_url(audio['file_url'])
                response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')
                prompt_added = True

    if not prompt_added:
        # Fall through to generic prompt
        fallback_type = audio_type or 'voicemail_no_answer'
        response_parts.append(_say_or_play(fallback_type,
            'Sorry, no one is available to take your call right now. '
            'Please leave a message after the tone.'))

    record_url = f"{config.webhook_base_url}/api/voice/voicemail"
    transcription_url = f"{config.webhook_base_url}/api/voice/transcription"
    response_parts.append(f'    <Record maxLength="120" action="{xml_escape(record_url)}" recordingStatusCallback="{xml_escape(record_url)}" recordingStatusCallbackEvent="completed" transcribe="true" transcribeCallback="{xml_escape(transcription_url)}" />')
    response_parts.append('</Response>')
    return Response('\n'.join(response_parts), mimetype='application/xml')


def _handle_closed_call(response_parts, call_flow, schedule, matched_holiday,
                        called_number, from_number, call_sid, db, routing, is_test=False,
                        next_open=None):
    """Handle a call that arrives when business is closed.

    Action priority:
    1. Closure-specific action (matched_holiday.action)
    2. Schedule default action (schedule.default_closure_action)
    3. Call flow closed_action (call_flow.closed_action or 'voicemail')
    """
    # Determine effective action with priority: closure > schedule default > call flow
    closure_action = None
    if matched_holiday:
        closure_action = matched_holiday.get('action')
    if not closure_action and schedule:
        closure_action = schedule.get('default_closure_action')
    if not closure_action:
        closure_action = call_flow.get('closed_action', 'voicemail') if call_flow else 'voicemail'

    # Determine effective audio with priority: closure > schedule default > call flow
    closure_audio_id = None
    has_override_audio = False  # True if audio comes from closure/schedule (overrides message parts)
    if matched_holiday:
        closure_audio_id = matched_holiday.get('audio_id')
        if closure_audio_id:
            has_override_audio = True
    if not closure_audio_id and schedule:
        closure_audio_id = schedule.get('default_closure_audio_id')
        if closure_audio_id:
            has_override_audio = True
    if not closure_audio_id and call_flow:
        closure_audio_id = call_flow.get('closed_audio_id')

    # Determine effective forward_to with priority: closure > schedule default > call flow
    closure_forward_to = None
    if matched_holiday:
        closure_forward_to = matched_holiday.get('forward_to')
    if not closure_forward_to and schedule:
        closure_forward_to = schedule.get('default_closure_forward_to')
    if not closure_forward_to and call_flow:
        closure_forward_to = call_flow.get('closed_forward_number')

    # Parse message parts if configured — only skipped when closure/schedule audio overrides
    message_parts = None
    if not has_override_audio and call_flow and call_flow.get('closed_message_parts'):
        try:
            raw = call_flow['closed_message_parts']
            message_parts = json.loads(raw) if isinstance(raw, str) else raw
            logger.info(f"Closed message parts loaded: {len(message_parts)} segments")
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Invalid closed_message_parts JSON for call flow {call_flow.get('id')}")
    else:
        logger.info(f"No message parts: has_override_audio={has_override_audio}, "
                     f"call_flow={bool(call_flow)}, "
                     f"closed_message_parts={call_flow.get('closed_message_parts', 'NOT SET') if call_flow else 'NO FLOW'}")

    def _build_closed_sequence(default_tts=None):
        """Build the closed audio sequence. Returns list of TwiML parts."""
        # Message parts: full control over sequence including where reopen info appears
        if message_parts:
            parts = _build_closed_message_twiml(message_parts, next_open, db)
            if parts:
                return parts
        # Single audio (reopen info only via message sequence)
        audio = db.get_audio_file(closure_audio_id) if closure_audio_id else None
        if audio and audio.get('file_url'):
            audio_url = _get_full_audio_url(audio['file_url'])
            return [f'    <Play>{xml_escape(audio_url)}</Play>']
        # Default TTS fallback
        if default_tts:
            return [f'    <Say>{default_tts}</Say>']
        return []

    if closure_action == 'disconnect' or closure_action == 'message':
        # Play closed message and hang up (no voicemail)
        response_parts.extend(_build_closed_sequence(
            default_tts='We are currently closed. Please call back during business hours.'))
        response_parts.append('    <Hangup/>')
        response_parts.append('</Response>')

        details = f"From: {from_number}, Status: CLOSED, Action: {closure_action}"
        if is_test:
            details = f"[TEST] {details}"
        if matched_holiday:
            details += f", Closure: {matched_holiday.get('name')}"
        db.log_activity(
            action="incoming_call_closed" + ("_test" if is_test else ""),
            target=called_number,
            details=details,
            performed_by="twilio"
        )
        return Response('\n'.join(response_parts), mimetype='application/xml')

    if closure_action == 'forward' and closure_forward_to:
        # Forward call to another number
        audio = db.get_audio_file(closure_audio_id) if closure_audio_id else None
        if audio and audio.get('file_url'):
            audio_url = _get_full_audio_url(audio['file_url'])
            response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')
        response_parts.append(f'    <Dial>{xml_escape(closure_forward_to)}</Dial>')
        response_parts.append('</Response>')

        details = f"From: {from_number}, Status: CLOSED, Action: forward to {closure_forward_to}"
        if is_test:
            details = f"[TEST] {details}"
        if matched_holiday:
            details += f", Closure: {matched_holiday.get('name')}"
        db.log_activity(
            action="incoming_call_closed" + ("_test" if is_test else ""),
            target=called_number,
            details=details,
            performed_by="twilio"
        )
        return Response('\n'.join(response_parts), mimetype='application/xml')

    if closure_action == 'ai_receptionist' and not is_test:
        # Play closed message first, then redirect to AI receptionist
        closed_seq = _build_closed_sequence()
        if closed_seq:
            response_parts.extend(closed_seq)

        details = f"From: {from_number}, Status: CLOSED, Action: ai_receptionist"
        if matched_holiday:
            details += f", Closure: {matched_holiday.get('name')}"
        db.log_activity(
            action="incoming_call_closed",
            target=called_number,
            details=details,
            performed_by="twilio"
        )

        return _redirect_to_ai_receptionist(
            response_parts, called_number, from_number, call_sid, db, routing,
            reason="closed"
        )

    # Voicemail (default for closed hours)
    response_parts.extend(_build_closed_sequence(
        default_tts='We are currently closed. Please leave a message after the tone.'))

    record_url = f"{config.webhook_base_url}/api/voice/voicemail"
    transcription_url = f"{config.webhook_base_url}/api/voice/transcription"
    response_parts.append(f'    <Record maxLength="120" action="{xml_escape(record_url)}" recordingStatusCallback="{xml_escape(record_url)}" recordingStatusCallbackEvent="completed" transcribe="true" transcribeCallback="{xml_escape(transcription_url)}" />')
    response_parts.append('</Response>')

    details = f"From: {from_number}, Status: CLOSED, Action: voicemail"
    if is_test:
        details = f"[TEST] {details}"
    if matched_holiday:
        details += f", Closure: {matched_holiday.get('name')}"
    db.log_activity(
        action="incoming_call_closed" + ("_test" if is_test else ""),
        target=called_number,
        details=details,
        performed_by="twilio"
    )

    return Response('\n'.join(response_parts), mimetype='application/xml')


@api_bp.route('/voice/queue/<int:queue_id>/no-answer', methods=['POST'])
def queue_no_answer(queue_id):
    """Handle when no one answers - put caller in queue.

    Twilio calls this after a Dial attempt completes.
    If no one answered, we enqueue the caller with hold music.
    If someone answered (completed), the call is already connected.

    No auth required - Twilio calls this directly.
    """
    db = get_db()
    queue = db.get_queue(queue_id)

    # Check what happened with the dial attempt
    dial_status = request.form.get('DialCallStatus', 'no-answer')
    called_number = request.form.get('Called', '')
    from_number = request.form.get('From', '')

    logger.info(f"Queue {queue_id} dial result: {dial_status} for call from {from_number}")

    # Get call SID for tracking
    call_sid = request.form.get('CallSid', '')

    # If someone answered, the call is connected - return empty response
    if dial_status == 'completed':
        # Update call_log: direct answer (no queue)
        db.update_call_log(call_sid, {
            'call_type': 'direct',
            'status': 'answered',
            'answered_at': 'CURRENT_TIMESTAMP',
        })
        db.log_activity(
            action="call_answered",
            target=called_number,
            details=f"From: {from_number}, Queue: {queue.get('name') if queue else 'Unknown'}",
            performed_by="twilio"
        )
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', mimetype='application/xml')

    # No one answered - check if any agents are still active before queueing
    if queue:
        # Check if any queue members are active AND reachable (not DND, have a device)
        members = db.get_queue_members(queue_id)
        active_members = [m for m in members if m.get('is_active')]
        available_members = []
        for m in active_members:
            settings = db.get_user_ring_settings(m['user_email'])
            if settings.get('dnd'):
                continue
            if settings.get('ring_browser', True) or settings.get('ring_sip', True):
                available_members.append(m)

        if available_members:
            # Agents are on duty but busy - put caller in queue
            queue_name = f"queue_{queue['id']}"
            wait_url = f"{config.webhook_base_url}/api/voice/queue/{queue['id']}/wait"

            # Enrich caller data from Clara/Otto
            from rinq.services.caller_enrichment import get_enrichment_service
            enrichment = get_enrichment_service()
            caller_info = enrichment.enrich_caller(from_number)

            # Store enriched queue entry
            db.add_queued_call({
                'call_sid': call_sid,
                'queue_id': queue['id'],
                'queue_name': queue.get('name'),
                'caller_number': from_number,
                'called_number': called_number,
                'customer_id': caller_info.get('customer_id'),
                'customer_name': caller_info.get('customer_name'),
                'customer_email': caller_info.get('customer_email'),
                'order_data': caller_info.get('order_data'),
                'priority': caller_info.get('priority', 'unknown'),
                'priority_reason': caller_info.get('priority_reason'),
            })

            # Update call_log: call went to queue
            db.update_call_log(call_sid, {
                'call_type': 'queue',
                'status': 'queued',
                'customer_id': caller_info.get('customer_id'),
                'customer_name': caller_info.get('customer_name'),
                'customer_email': caller_info.get('customer_email'),
            })

            db.log_activity(
                action="call_queued",
                target=called_number,
                details=f"From: {from_number}, Queue: {queue.get('name')}, Customer: {caller_info.get('customer_name') or 'Unknown'}",
                performed_by="twilio"
            )

            # Action URL handles when caller leaves the queue (hangup, timeout, etc)
            leave_url = f"{config.webhook_base_url}/api/voice/queue/{queue['id']}/leave"

            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>All of our team members are currently busy. Please hold and we will be with you shortly.</Say>
    <Enqueue waitUrl="{xml_escape(wait_url)}" waitUrlMethod="POST" action="{xml_escape(leave_url)}">{xml_escape(queue_name)}</Enqueue>
</Response>'''
        else:
            # No agents on duty - check call flow no-answer action
            # Look up the call flow that uses this queue
            no_answer_action = 'ai_receptionist'  # default
            try:
                with db._get_conn() as conn:
                    row = conn.execute(
                        "SELECT open_no_answer_action FROM call_flows WHERE open_queue_id = ?",
                        (queue_id,)
                    ).fetchone()
                    if row:
                        no_answer_action = row['open_no_answer_action'] or 'ai_receptionist'
            except Exception:
                logger.warning(f"Could not look up call flow for queue {queue_id}, using default: ai_receptionist")

            logger.warning(f"No active agents in queue {queue.get('name')} - action: {no_answer_action}")

            if no_answer_action == 'ai_receptionist':
                # Build response_parts and routing to reuse _redirect_to_ai_receptionist
                response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']
                routing = {'queue': queue}
                return _redirect_to_ai_receptionist(
                    response_parts, called_number, from_number, call_sid, db, routing,
                    reason="no_active_agents"
                )

            # Voicemail fallback
            db.update_call_log(call_sid, {
                'call_type': 'voicemail',
                'status': 'voicemail',
            })
            db.log_activity(
                action="call_no_agents_voicemail",
                target=called_number,
                details=f"From: {from_number}, Queue: {queue.get('name')} has no active agents",
                performed_by="twilio"
            )

            return _build_voicemail_twiml(queue_id, call_sid, from_number=from_number)
    else:
        # Queue not found - apologize and hang up
        return Response('''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, we are unable to take your call at this time. Please try again later.</Say>
    <Hangup/>
</Response>''', mimetype='application/xml')


@api_bp.route('/voice/queue/<int:queue_id>/leave', methods=['POST'])
def queue_leave(queue_id):
    """Handle when a caller leaves the queue (hangup, bridged to agent, etc).

    Twilio calls this when the <Enqueue> action completes. This happens when:
    - Caller hangs up while waiting
    - Caller is bridged to an agent
    - Queue timeout occurs

    No auth required - Twilio calls this directly.
    """
    db = get_db()

    call_sid = request.form.get('CallSid', '')
    queue_result = request.form.get('QueueResult', '')  # bridged, hangup, error, etc.
    queue_time = request.form.get('QueueTime', '0')  # Seconds caller was in queue

    logger.info(f"Queue {queue_id} leave: call_sid={call_sid}, result={queue_result}, time={queue_time}s")

    # Parse queue time as integer for call_log
    ring_seconds = int(queue_time) if queue_time else 0

    # Update the queued call record based on what happened
    if queue_result == 'hangup':
        # Caller hung up while waiting - cancel any ringing agent calls
        logger.info(f"Caller hung up - cancelling agent calls for {call_sid}")
        import threading
        threading.Thread(
            target=_cancel_agent_calls,
            args=(call_sid,),
            daemon=True
        ).start()

        db.update_queued_call_status(call_sid, 'abandoned')
        db.update_call_log(call_sid, {
            'status': 'abandoned',
            'ring_seconds': ring_seconds,
            'ended_at': 'CURRENT_TIMESTAMP',
        })
        db.log_activity(
            action="call_abandoned",
            target=f"queue_{queue_id}",
            details=f"Caller hung up after {queue_time}s in queue",
            performed_by="twilio"
        )
    elif queue_result in ('bridged', 'redirected', 'leave'):
        # Cancel any remaining ringing agent calls
        import threading
        threading.Thread(
            target=_cancel_agent_calls,
            args=(call_sid,),
            daemon=True
        ).start()

        queued_call = db.get_queued_call_by_sid(call_sid)
        was_answered = queued_call and queued_call.get('answered_by')

        if was_answered:
            # Caller was connected to an agent
            # 'bridged' = connected via <Dial> from queue member
            # 'redirected' = we redirected caller to a conference (browser softphone answer)
            if queued_call.get('status') == 'waiting':
                db.update_queued_call_status(call_sid, 'answered')
            db.update_call_log(call_sid, {
                'status': 'answered',
                'ring_seconds': ring_seconds,
                'answered_at': 'CURRENT_TIMESTAMP',
                'agent_email': queued_call.get('answered_by'),
            })
            db.log_activity(
                action="call_bridged",
                target=f"queue_{queue_id}",
                details=f"Caller connected ({queue_result}) after {queue_time}s in queue",
                performed_by="twilio"
            )
        else:
            # Redirected but no agent answered — either voicemail escape or max wait timeout
            # Check if caller already chose voicemail (status set by queue-escape handler)
            chose_voicemail = queued_call and queued_call.get('status') == 'voicemail'
            chose_callback = queued_call and queued_call.get('status') == 'callback'

            if chose_voicemail:
                logger.info(f"Queue {queue_id} voicemail escape for {call_sid} after {queue_time}s")
                db.update_call_log(call_sid, {
                    'status': 'voicemail',
                    'ring_seconds': ring_seconds,
                })
                db.log_activity(
                    action="call_voicemail_escape",
                    target=f"queue_{queue_id}",
                    details=f"Caller chose voicemail after {queue_time}s in queue",
                    performed_by="twilio"
                )
            elif chose_callback:
                # Callback — already handled by queue-escape, just update call_log
                logger.info(f"Queue {queue_id} callback request for {call_sid} after {queue_time}s")
                db.update_call_log(call_sid, {
                    'status': 'callback',
                    'ring_seconds': ring_seconds,
                    'ended_at': 'CURRENT_TIMESTAMP',
                })
            else:
                logger.info(f"Queue {queue_id} max wait timeout for {call_sid} after {queue_time}s")
                db.update_queued_call_status(call_sid, 'timeout')
                db.update_call_log(call_sid, {
                    'status': 'voicemail',
                    'ring_seconds': ring_seconds,
                })
                db.log_activity(
                    action="call_queue_timeout",
                    target=f"queue_{queue_id}",
                    details=f"Max wait exceeded after {queue_time}s — sending to voicemail",
                    performed_by="twilio"
                )
            if chose_callback:
                # Play confirmation and hang up (now outside queue wait context, so these verbs work)
                queue = db.get_queue(queue_id)
                queue_name = queue.get('name', 'our team') if queue else 'our team'
                confirm_prompt = _say_or_play('callback_confirm', f'No problem. We have your number and someone from {xml_escape(queue_name)} will call you back shortly. Goodbye.')
                twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
{confirm_prompt}
    <Hangup />
</Response>'''
                return Response(twiml, mimetype='application/xml')
            else:
                # Send caller to voicemail — use voicemail_escape type if they chose it, queue_no_agents for timeout
                vm_audio_type = 'voicemail_escape' if chose_voicemail else 'queue_no_agents'
                return _build_voicemail_twiml(queue_id, call_sid, from_number=request.form.get('From', ''), audio_type=vm_audio_type)
    elif queue_result in ('error', 'system-error'):
        db.update_queued_call_status(call_sid, 'timeout')
        db.update_call_log(call_sid, {
            'status': 'failed',
            'ring_seconds': ring_seconds,
            'ended_at': 'CURRENT_TIMESTAMP',
            'notes': f'Queue error: {queue_result}',
        })
        db.log_activity(
            action="call_queue_error",
            target=f"queue_{queue_id}",
            details=f"Queue error: {queue_result} after {queue_time}s",
            performed_by="twilio"
        )
    else:
        # Other results (queue-full, etc)
        # But check if already answered (by browser) before marking abandoned
        queued_call = db.get_queued_call_by_sid(call_sid)
        if queued_call and queued_call.get('status') == 'answered':
            # Already answered via browser softphone - don't overwrite
            db.log_activity(
                action="call_left_queue_answered",
                target=f"queue_{queue_id}",
                details=f"Queue result '{queue_result}' but call already answered after {queue_time}s",
                performed_by="twilio"
            )
        else:
            # Actually abandoned
            db.update_queued_call_status(call_sid, 'abandoned')
            db.update_call_log(call_sid, {
                'status': 'abandoned',
                'ring_seconds': ring_seconds,
                'ended_at': 'CURRENT_TIMESTAMP',
                'notes': f'Left queue: {queue_result}',
            })
            db.log_activity(
                action="call_left_queue",
                target=f"queue_{queue_id}",
                details=f"Left queue: {queue_result} after {queue_time}s",
                performed_by="twilio"
            )

    # Return empty TwiML - call is ending
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', mimetype='application/xml')


@api_bp.route('/voice/agent-status', methods=['POST'])
def agent_status_callback():
    """Handle agent call status updates.

    Logs when agents finish calls for tracking purposes.
    Agents pick up queued callers via the queue dashboard UI.

    No auth required - Twilio calls this directly.
    """
    db = get_db()

    # Get status info from Twilio
    call_status = request.form.get('CallStatus', '')
    agent_number = request.form.get('To', '')  # The agent's phone number

    logger.info(f"Agent status callback: {agent_number} -> {call_status}")

    # Log completed calls for tracking
    if call_status == 'completed':
        user_device = db.get_device_by_number(agent_number)
        if user_device:
            db.log_activity(
                action="agent_call_completed",
                target=agent_number,
                details=f"Agent {user_device.get('user_email')} finished a call",
                performed_by="twilio"
            )

    return Response('OK', status=200)


@api_bp.route('/voice/call-status', methods=['POST'])
def call_status_callback():
    """Handle call status updates from Twilio.

    Twilio calls this when a call's status changes (ringing, answered, completed, etc.).
    Used to complete call_log entries with final status and duration.

    No auth required - Twilio calls this directly.
    """
    db = get_db()

    call_sid = request.form.get('CallSid', '')
    call_status = request.form.get('CallStatus', '')
    call_duration = request.form.get('CallDuration', '0')  # seconds

    logger.info(f"Call status callback: {call_sid} -> {call_status}, duration={call_duration}s")

    # Map Twilio status to our status
    status_map = {
        'completed': 'answered',
        'busy': 'busy',
        'no-answer': 'missed',
        'failed': 'failed',
        'canceled': 'abandoned',
    }

    if call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled'):
        duration = int(call_duration) if call_duration else 0

        mapped_status = status_map.get(call_status, call_status)

        # Don't downgrade an already-answered call to abandoned/missed
        # (e.g. agent hangup after failed transfer triggers 'canceled')
        if mapped_status in ('abandoned', 'missed'):
            existing = db.get_call_log_field(call_sid, 'status')
            if existing == 'answered':
                mapped_status = 'completed'

        # Complete the call in call_log
        db.complete_call(
            call_sid=call_sid,
            status=mapped_status,
            agent_email=None,  # Already set during call routing
            talk_seconds=duration if call_status == 'completed' else 0,
        )

        # Mark participant as left and end lone calls
        _handle_participant_left(call_sid, db)

        # Cancel any pending ring calls for conference-first inbound calls.
        # When the caller hangs up before an agent answers, the outbound
        # ring calls (SIP/browser) keep ringing because they're separate
        # calls not yet joined to the conference.
        conference_name = f"call_{call_sid}"
        ring_sids = db.pop_ring_attempts(conference_name)
        if ring_sids:
            service = get_twilio_service()
            for sid in ring_sids:
                try:
                    service.client.calls(sid).update(status='completed')
                except Exception as e:
                    logger.debug(f"Could not cancel ring call {sid}: {e}")
            logger.info(f"Caller {call_sid} disconnected — cancelled {len(ring_sids)} ringing agent calls")

        # If this was an AI receptionist call, notify AI receptionist so it can
        # run post-call processing (summary, Zendesk ticket, email)
        call_type = db.get_call_log_field(call_sid, 'call_type')
        if call_type == 'ai_receptionist':
            _notify_ai_receptionist_call_ended(call_sid, call_status)

    return Response('OK', status=200)


@api_bp.route('/voice/dial-status', methods=['POST'])
def dial_status_callback():
    """Handle dial completion from Twilio.

    Called when a <Dial> action completes. Updates call_log with final status.

    If a hold conference is pending (agent pressed hold during an outbound
    call), this redirects the agent into the conference instead of ending.

    No auth required - Twilio calls this directly.
    """
    db = get_db()

    call_sid = request.form.get('CallSid', '')
    dial_status = request.form.get('DialCallStatus', '')  # completed, busy, no-answer, failed, canceled
    dial_duration = request.form.get('DialCallDuration', '0')  # seconds if answered

    logger.info(f"Dial status callback: {call_sid} -> {dial_status}, duration={dial_duration}s")

    # Check if there's a pending hold conference — the hold handler set this
    # up before redirecting the child call, which broke the <Dial> bridge.
    conference_name = db.get_call_conference(call_sid)
    if conference_name:
        logger.info(f"Dial status: hold pending, redirecting agent {call_sid} to conference {conference_name}")
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">{xml_escape(conference_name)}</Conference>
    </Dial>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    # Map Twilio dial status to our status
    status_map = {
        'completed': 'answered',
        'answered': 'answered',
        'busy': 'busy',
        'no-answer': 'missed',
        'failed': 'failed',
        'canceled': 'abandoned',
    }

    duration = int(dial_duration) if dial_duration else 0
    final_status = status_map.get(dial_status, dial_status)

    # Don't downgrade an already-answered call to abandoned/missed
    if final_status in ('abandoned', 'missed'):
        existing = db.get_call_log_field(call_sid, 'status')
        if existing == 'answered':
            final_status = 'completed'

    # Update the call log
    updates = {
        'status': final_status,
        'ended_at': 'CURRENT_TIMESTAMP',
    }

    if final_status == 'answered':
        updates['answered_at'] = 'CURRENT_TIMESTAMP'
        updates['talk_seconds'] = duration

    db.update_call_log(call_sid, updates)

    # Return empty TwiML - call is ending
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', mimetype='application/xml')


@api_bp.route('/voice/queue/<int:queue_id>/connect-agent', methods=['POST'])
def queue_connect_agent(queue_id):
    """TwiML for connecting an agent to a queued caller.

    This is called when we dial an agent to connect them to a waiting caller.
    Returns TwiML that dials into the queue.

    No auth required - Twilio calls this directly.
    """
    db = get_db()
    queue = db.get_queue(queue_id)

    if not queue:
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, the queue is no longer available.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    queue_name = f"queue_{queue_id}"

    # Tell the agent they're being connected, then dial into the queue
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting you to a waiting caller.</Say>
    <Dial>
        <Queue>{xml_escape(queue_name)}</Queue>
    </Dial>
</Response>'''

    db.log_activity(
        action="agent_connected_to_queue",
        target=queue.get('name'),
        details=f"Agent connected to dequeue caller",
        performed_by="twilio"
    )

    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/queue/<int:queue_id>/agent-answer', methods=['POST'])
def queue_agent_answer(queue_id):
    """TwiML for when an agent answers an auto-ring call.

    This is called when the auto-ring system calls an agent's SIP phone or mobile,
    and they answer. It connects them to the queued caller via the <Queue> noun.

    Query params:
        customer_call_sid: The call SID of the customer waiting in queue

    No auth required - Twilio calls this directly.
    """
    db = get_db()
    queue = db.get_queue(queue_id)
    customer_call_sid = request.args.get('customer_call_sid', '')
    agent_call_sid = request.values.get('CallSid', '')

    if not queue:
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, the queue is no longer available.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    queue_name = f"queue_{queue_id}"

    # Get agent info from Twilio's request params and resolve to email
    called = request.values.get('Called', '')
    agent_email, _ = _normalize_staff_identifier(called)
    agent_info = agent_email or called or 'unknown'

    logger.info(f"Agent {agent_info} answered auto-ring call for queue {queue_name}")

    # Atomically claim the call — only the first agent to reach this wins
    claimed = db.claim_queued_call(customer_call_sid, answered_by=agent_info)

    if not claimed:
        # Another agent already answered, or customer hung up
        logger.info(f"Agent {agent_info} lost race for {customer_call_sid} — already claimed")
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>This call has already been answered by another agent.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    # Cancel other agent calls since this one won
    import threading
    def cancel_others():
        _cancel_agent_calls(customer_call_sid, except_call_sid=agent_call_sid)
    threading.Thread(target=cancel_others, daemon=True).start()

    # Conference-based answer: same pattern as browser queue answer.
    # Redirect customer from queue into conference, agent joins same conference.
    conference_name = f"hold_room_{customer_call_sid}"

    twilio_service = get_twilio_service()
    conference_url = f"{config.webhook_base_url}/api/voice/conference/join?room={conference_name}&role=caller"
    try:
        twilio_service.client.calls(customer_call_sid).update(
            url=conference_url, method='POST'
        )
    except Exception as e:
        logger.error(f"Auto-ring answer: redirect FAILED for {customer_call_sid}: {e}")
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, we could not connect to the caller. They may have hung up.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    db.update_queued_call_status(customer_call_sid, 'answered', answered_by=agent_info)
    db.update_call_log(customer_call_sid, {
        'status': 'answered',
        'agent_email': agent_email,
        'answered_at': 'CURRENT_TIMESTAMP',
    })
    db.set_call_conference(customer_call_sid, conference_name)

    # Record participants
    queued_call = db.get_queued_call_by_sid(customer_call_sid)
    agent_user = db.get_user_by_email(agent_email) if agent_email else None
    agent_name = (agent_user.get('friendly_name') if agent_user else None) or agent_info
    db.add_participant(conference_name, agent_call_sid, 'agent',
                       name=agent_name, email=agent_email)
    customer_number = (queued_call.get('caller_number') or queued_call.get('from_number')) if queued_call else None
    customer_name = (queued_call.get('customer_name') if queued_call else None) or customer_number
    db.add_participant(conference_name, customer_call_sid, 'customer',
                       name=customer_name, phone_number=customer_number)

    # Agent joins the conference — brief message gives redirect time
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting.</Say>
    <Dial>
        <Conference endConferenceOnExit="true" startConferenceOnEnter="true">{xml_escape(conference_name)}</Conference>
    </Dial>
</Response>'''

    db.log_activity(
        action="agent_answered_auto_ring",
        target=queue.get('name'),
        details=f"Agent {agent_info} answered, connecting to customer call {customer_call_sid} via conference {conference_name}",
        performed_by="twilio"
    )

    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/queue/<int:queue_id>/agent-ring-status', methods=['POST'])
def queue_agent_ring_status(queue_id):
    """Handle status callbacks for auto-ring outbound calls.

    This is called when an outbound call to an agent ends (busy, no-answer, failed, etc.).
    If the queue's reject_action is 'voicemail' and the agent rejected (busy), we
    redirect the customer to voicemail immediately.

    Query params:
        customer_call_sid: The call SID of the customer waiting in queue

    No auth required - Twilio calls this directly.
    """
    db = get_db()

    agent_call_sid = request.form.get('CallSid', '')
    call_status = request.form.get('CallStatus', '')
    customer_call_sid = request.args.get('customer_call_sid', '')

    logger.info(f"Agent ring status: {agent_call_sid} -> {call_status} (customer: {customer_call_sid})")

    # Look up reverse mapping from DB and clean up
    call_info = db.get_ring_attempt_metadata(agent_call_sid)
    db.remove_ring_attempt_by_sid(agent_call_sid)
    if not call_info:
        logger.debug(f"No tracking info for agent call {agent_call_sid}")
        return Response('OK', status=200)

    # Only handle rejection (busy) - other statuses (no-answer, failed) are normal
    # "busy" specifically means the agent rejected the call
    if call_status != 'busy':
        logger.debug(f"Agent call {agent_call_sid} ended with {call_status} - not a rejection")
        return Response('OK', status=200)

    # Get queue settings
    queue = db.get_queue(queue_id)
    if not queue:
        logger.warning(f"Queue {queue_id} not found for rejection handling")
        return Response('OK', status=200)

    reject_action = queue.get('reject_action', 'continue')
    logger.info(f"Queue {queue.get('name')} reject_action={reject_action}, agent {call_info.get('user_email')} rejected")

    if reject_action != 'voicemail':
        # Default behavior: continue - other devices may still ring
        db.log_activity(
            action="agent_rejected_call",
            target=f"queue_{queue_id}",
            details=f"Agent {call_info.get('user_email')} rejected on {call_info.get('device_type')}, continuing",
            performed_by="twilio"
        )
        return Response('OK', status=200)

    # Voicemail action: redirect customer to voicemail
    logger.info(f"Rejection triggers voicemail for customer {customer_call_sid}")

    # Cancel all other ringing agent calls
    import threading
    def cancel_all():
        _cancel_agent_calls(customer_call_sid)
    threading.Thread(target=cancel_all, daemon=True).start()

    # Check if customer is still in queue
    queued_call = db.get_queued_call_by_sid(customer_call_sid)
    if not queued_call or queued_call.get('status') != 'waiting':
        logger.info(f"Customer {customer_call_sid} no longer waiting - skipping voicemail redirect")
        return Response('OK', status=200)

    # Redirect the customer's call to voicemail
    try:
        service = get_twilio_service()
        voicemail_url = f"{config.webhook_base_url}/api/voice/queue/{queue_id}/rejected-voicemail"

        service.client.calls(customer_call_sid).update(
            url=voicemail_url,
            method='POST'
        )

        # Update queued call status
        db.update_queued_call_status(customer_call_sid, 'voicemail')

        db.log_activity(
            action="agent_rejected_to_voicemail",
            target=f"queue_{queue_id}",
            details=f"Agent {call_info.get('user_email')} rejected, redirecting to voicemail",
            performed_by="twilio"
        )

    except Exception as e:
        logger.exception(f"Failed to redirect customer {customer_call_sid} to voicemail: {e}")

    return Response('OK', status=200)


def _build_voicemail_twiml(queue_id, call_sid, from_number='', audio_type='queue_no_agents'):
    """Build voicemail TwiML for queue timeout, rejection, or voicemail escape.

    Delegates to _go_to_voicemail with audio_type override.
    """
    logger.info(f"Sending call {call_sid} to voicemail (queue {queue_id})")
    db = get_db()
    response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']
    return _go_to_voicemail(
        response_parts, call_flow=None,
        called_number='', from_number=from_number,
        call_sid=call_sid, db=db, routing=None,
        reason='no_answer', audio_type=audio_type,
    )


@api_bp.route('/voice/queue/<int:queue_id>/rejected-voicemail', methods=['POST'])
def queue_rejected_voicemail(queue_id):
    """TwiML for voicemail when a call is rejected.

    This is called when a customer is redirected to voicemail because an agent
    rejected their call (and the queue's reject_action is 'voicemail').

    No auth required - Twilio calls this directly.
    """
    call_sid = request.form.get('CallSid', '')
    from_number = request.form.get('From', '')
    logger.info(f"Playing rejection voicemail TwiML for queue {queue_id}")
    return _build_voicemail_twiml(queue_id, call_sid, from_number=from_number)


@api_bp.route('/voice/conference/join', methods=['POST'])
def conference_join():
    """Return TwiML to join a conference room.

    Query params:
        room: Conference room name
        role: 'caller' or 'agent' (affects conference settings)

    This is called when redirecting callers from the queue to a conference,
    and when agents answer queue calls via the browser softphone.

    No auth required - Twilio calls this directly.
    """
    room = request.args.get('room', '')
    role = request.args.get('role', 'caller')
    call_sid = request.form.get('CallSid', '?')
    logger.info(f"Conference join: {call_sid} -> room={room}, role={role}")

    # Track participant — conference_join is the central entry point for all
    # conference participants, so this is the DRY place to record them.
    if call_sid and call_sid != '?' and room:
        try:
            db = get_db()
            participant_role = 'customer' if role == 'caller' else 'agent'
            # Only add if not already tracked (avoid overwriting richer data
            # from the specific entry point that initiated this call)
            existing = db.get_participant_by_sid(call_sid)
            if not existing:
                # Try to resolve name from Twilio call details
                name, email = None, None
                resolved_role = participant_role
                try:
                    twilio_service = get_twilio_service()
                    call = twilio_service.client.calls(call_sid).fetch()
                    to = call.to or ''
                    if to.startswith('client:'):
                        identity = to[7:]
                        email = identity.replace('_at_', '@').replace('_', '.')
                        user = db.get_user_by_email(email)
                        if user:
                            name = user.get('friendly_name')
                        resolved_role = 'agent'  # Staff member, not customer
                    elif to.startswith('sip:'):
                        sip_user = to[4:].split('@')[0]
                        user = db.get_user_by_username(sip_user)
                        if user:
                            email = user.get('staff_email')
                            name = user.get('friendly_name')
                        resolved_role = 'agent'  # Staff member, not customer
                    elif to.startswith('+'):
                        # External number — use caller's number, not the number they dialled
                        caller_number = getattr(call, '_from', None) or to
                        name = caller_number
                except Exception:
                    pass
                db.add_participant(room, call_sid, resolved_role,
                                   name=name, email=email)
        except Exception as e:
            logger.debug(f"Could not track participant {call_sid} in {room}: {e}")

    if not room:
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, no conference room specified.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    # Different settings for caller vs agent
    if role == 'caller':
        # Caller: end conference when they hang up so agent side disconnects too
        # After-Dial TwiML handles the case where the caller ends up alone
        # (e.g. transfer gone wrong) — plays a message and hangs up
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference startConferenceOnEnter="false" endConferenceOnExit="true" waitUrl="{config.webhook_base_url}/api/voice/hold-music" waitMethod="POST">{xml_escape(room)}</Conference>
    </Dial>
    <Hangup/>
</Response>'''
    elif role == 'agent_no_exit':
        # Agent in 3-way mode: joins but doesn't end conference when leaving
        # This allows agent to drop out while customer + target continue
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference startConferenceOnEnter="true" endConferenceOnExit="false">{xml_escape(room)}</Conference>
    </Dial>
</Response>'''
    else:
        # Agent: start conference when they join, end when they leave
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference startConferenceOnEnter="true" endConferenceOnExit="true">{xml_escape(room)}</Conference>
    </Dial>
</Response>'''

    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/hold-music', methods=['GET', 'POST'])
def hold_music():
    """Return TwiML to play hold music.

    Used as the hold_url for conference participants on hold.
    Twilio makes a GET request to this URL.

    No auth required - Twilio calls this directly.
    """
    base = request.host_url.rstrip('/')
    music_url = f'{base}/static/clockwork_waltz_60s.mp3'

    # Try to find tenant-configured hold music
    try:
        db = get_db()
        # Use the first queue's hold music as the default for the tenant
        queues = db.get_queues()
        for q in queues:
            if q.get('hold_music_url'):
                music_url = _get_full_audio_url(q['hold_music_url'])
                break
    except Exception as e:
        logger.debug(f"No tenant hold music, using static default: {e}")

    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{xml_escape(music_url)}</Play>
</Response>'''

    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/ringback', methods=['GET', 'POST'])
def ringback():
    """Return TwiML playing a ringback tone.

    Used as the conference waitUrl so the agent hears ringing while
    the customer's phone rings for outbound calls.

    No auth required - Twilio calls this directly.
    """
    base = request.host_url.rstrip('/')
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play loop="0">{base}/static/au-ringback.mp3</Play>
</Response>'''
    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/inbound/ring-status', methods=['POST'])
def inbound_ring_status():
    """Handle status updates for conference-first inbound agent ring attempts.

    When an agent answers, cancels all other ringing legs and stores the
    agent's call SID. When all agents fail, ends the conference so the
    caller falls through to the no-answer TwiML.

    Query params:
        conference: Conference room name
        caller_call_sid: The caller's call SID

    No auth required - Twilio calls this directly.
    """
    conference_name = request.args.get('conference', '')
    caller_call_sid = request.args.get('caller_call_sid', '')
    agent_call_sid = request.form.get('CallSid', '')
    call_status = request.form.get('CallStatus', '')

    logger.info(f"Inbound ring status: {agent_call_sid} -> {call_status}, conference={conference_name}")

    db = get_db()

    if call_status == 'in-progress':
        # Agent answered — cancel all other ringing legs
        service = get_twilio_service()

        # Pop all ring attempts and cancel the others
        ring_sids = db.pop_ring_attempts(conference_name)

        # Store child SIDs both ways so either party can find the other
        db.set_call_child_sid(agent_call_sid, caller_call_sid)
        db.set_call_child_sid(caller_call_sid, agent_call_sid)
        db.update_call_log(caller_call_sid, {
            'status': 'answered',
            'answered_at': 'CURRENT_TIMESTAMP',
            'agent_email': _resolve_agent_email(agent_call_sid, service),
        })
        # Store conference name against both SIDs for hold
        db.set_call_conference(agent_call_sid, conference_name)
        db.set_call_conference(caller_call_sid, conference_name)

        # Record agent participant (customer/caller was added when the call started)
        agent_email = _resolve_agent_email(agent_call_sid, service)
        agent_user = db.get_user_by_email(agent_email) if agent_email else None
        agent_name = (agent_user.get('friendly_name') if agent_user else None) or agent_email
        db.add_participant(conference_name, agent_call_sid, 'agent',
                           name=agent_name, email=agent_email)

        # For inbound calls, also record the customer if not already present
        caller_number = db.get_call_log_field(caller_call_sid, 'from_number')
        if caller_number:
            customer_name = db.get_call_log_field(caller_call_sid, 'customer_name') or caller_number
            db.add_participant(conference_name, caller_call_sid, 'customer',
                               name=customer_name, phone_number=caller_number)

        for sid in ring_sids:
            if sid != agent_call_sid:
                try:
                    service.client.calls(sid).update(status='completed')
                except Exception as e:
                    logger.debug(f"Could not cancel ring leg {sid}: {e}")

        logger.info(f"Agent {agent_call_sid} answered, cancelled {len(ring_sids) - 1} other legs")

    elif call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled'):
        # This leg failed — remove it and check if all legs have failed
        db.remove_ring_attempt(conference_name, agent_call_sid)
        remaining = db.get_ring_attempts(conference_name)

        if not remaining:
            service = get_twilio_service()
            try:
                confs = twilio_list(service.client.conferences,
                    friendly_name=conference_name, status='in-progress', limit=1
                )
                if confs:
                    service.client.conferences(confs[0].sid).update(status='completed')
                    logger.info(f"All agents failed for {conference_name} — ended conference")
            except Exception as e:
                logger.warning(f"Could not end conference {conference_name}: {e}")

    return '', 204


def _resolve_agent_email(call_sid: str, service) -> str | None:
    """Try to resolve agent email from a call SID."""
    try:
        call = service.client.calls(call_sid).fetch()
        to = call.to or ''
        if to.startswith('client:'):
            identity = to[7:]
            return identity.replace('_at_', '@').replace('_', '.')
        if to.startswith('sip:'):
            sip_user = to[4:].split('@')[0]
            db = get_db()
            user = db.get_user_by_username(sip_user)
            if user:
                return user.get('staff_email')
    except Exception as e:
        logger.debug(f"Could not resolve agent email from call {call_sid}: {e}")
    return None


@api_bp.route('/voice/inbound/no-answer', methods=['POST'])
def inbound_no_answer():
    """Handle the case where no agent answered a conference-first inbound call.

    Called after the conference <Dial> ends because all agents failed or
    the ring timeout was reached. Routes to voicemail or AI receptionist
    based on the call flow configuration.

    Query params:
        call_sid: The caller's call SID
        called: The number that was called
        from: The caller's number
        flow_id: The call flow ID (for no-answer routing)

    No auth required - Twilio calls this directly.
    """
    call_sid = request.args.get('call_sid', '') or request.form.get('CallSid', '')
    called_number = request.args.get('called', '')
    from_number = request.args.get('from', '')
    flow_id = request.args.get('flow_id', '')

    db = get_db()

    # Look up the call flow for no-answer routing
    no_answer_action = 'voicemail'
    call_flow = None
    if flow_id:
        call_flow = db.get_call_flow(int(flow_id))
        if call_flow:
            no_answer_action = call_flow.get('open_no_answer_action', 'voicemail')

    logger.info(f"Inbound no-answer: {call_sid}, action={no_answer_action}")

    db.update_call_log(call_sid, {'status': 'missed'})

    response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']

    if no_answer_action == 'ai_receptionist':
        routing = db.get_call_routing(called_number)
        return _redirect_to_ai_receptionist(
            response_parts, called_number, from_number, call_sid, db,
            routing or {}, reason="no_answer"
        )

    # Default: voicemail
    prompt = _say_or_play('queue_no_agents',
        'Sorry, no one is available to take your call. Please leave a message after the tone.')
    voicemail_url = f"{config.webhook_base_url}/api/voice/voicemail"
    transcription_url = f"{config.webhook_base_url}/api/voice/transcription"
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
{prompt}
    <Record action="{xml_escape(voicemail_url)}" maxLength="120" transcribe="true" transcribeCallback="{xml_escape(transcription_url)}" />
</Response>'''
    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/outbound/customer-join', methods=['POST'])
def outbound_customer_join():
    """TwiML for when an outbound call customer answers.

    The customer joins the conference with startConferenceOnEnter=true,
    which stops the agent's ringback tone and connects both parties.

    Query params:
        conference: Conference room name

    No auth required - Twilio calls this directly.
    """
    conference_name = request.args.get('conference', '')
    customer_call_sid = request.form.get('CallSid', '')

    if not conference_name:
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, a system error occurred.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    db = get_db()

    # Update call log - customer answered
    agent_call_sid = conference_name.removeprefix('call_') if conference_name.startswith('call_') else None
    if agent_call_sid:
        db.update_call_log(agent_call_sid, {
            'status': 'answered',
            'answered_at': 'CURRENT_TIMESTAMP',
        })

    logger.info(f"Outbound customer answered: {customer_call_sid} joined conference {conference_name}")

    # Record customer participant
    customer_number = db.get_call_log_field(customer_call_sid, 'to_number') or ''
    customer_name = db.get_call_log_field(customer_call_sid, 'customer_name') or customer_number
    db.add_participant(conference_name, customer_call_sid, 'customer',
                       name=customer_name, phone_number=customer_number)

    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">{xml_escape(conference_name)}</Conference>
    </Dial>
</Response>'''
    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/outbound/customer-status', methods=['POST'])
def outbound_customer_status():
    """Handle status updates for the outbound customer call leg.

    When the customer doesn't answer (busy/no-answer/failed), ends the
    agent's conference so they don't sit hearing ringback forever.

    Query params:
        agent_call_sid: The agent's call SID

    No auth required - Twilio calls this directly.
    """
    agent_call_sid = request.args.get('agent_call_sid', '')
    customer_call_sid = request.form.get('CallSid', '')
    call_status = request.form.get('CallStatus', '')

    logger.info(f"Outbound customer status: {customer_call_sid} -> {call_status}, agent={agent_call_sid}")

    db = get_db()

    # Mark participant as left on any terminal state
    if call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled'):
        _handle_participant_left(customer_call_sid, db)

    # Terminal failure states — customer didn't answer
    if call_status in ('busy', 'no-answer', 'failed', 'canceled'):
        status_map = {
            'busy': 'busy',
            'no-answer': 'missed',
            'failed': 'failed',
            'canceled': 'abandoned',
        }
        db.update_call_log(agent_call_sid, {
            'status': status_map.get(call_status, call_status),
            'ended_at': 'CURRENT_TIMESTAMP',
        })

        # End the agent's conference so they aren't stuck hearing ringback
        conference_name = f"call_{agent_call_sid}"
        twilio_service = get_twilio_service()
        try:
            confs = twilio_list(twilio_service.client.conferences,
                friendly_name=conference_name, status='in-progress', limit=1
            )
            if confs:
                twilio_service.client.conferences(confs[0].sid).update(status='completed')
                logger.info(f"Ended conference {conference_name} — customer {call_status}")
        except Exception as e:
            logger.warning(f"Could not end conference {conference_name}: {e}")

    return '', 204


@api_bp.route('/voice/call/hold', methods=['POST'])
@api_or_session_auth
def call_hold():
    """Put a caller on hold or take them off hold.

    For queue calls (already in a conference): uses Conference Participant
    hold/unhold API directly.

    For outbound calls (not yet in a conference): creates a conference
    on-demand by redirecting the child call to hold music and the agent
    to a conference room.  Subsequent hold/unhold uses the conference API.

    Request body:
        call_sid: The call SID (queue caller SID or agent's parent SID)
        action: 'hold' or 'unhold'

    Returns:
        {"success": true} or {"error": "..."}
    """
    db = get_db()
    data = request.get_json() or {}

    call_sid = data.get('call_sid')
    action = data.get('action')

    if not call_sid or action not in ('hold', 'unhold'):
        return jsonify({"error": "call_sid and action (hold/unhold) required"}), 400

    twilio_service = get_twilio_service()
    hold_url = f"{config.webhook_base_url}/api/voice/hold-music"

    # Get the conference name for this call
    conference_name = db.get_call_conference(call_sid)
    logger.info(f"Hold request: call_sid={call_sid}, action={action}, conference={conference_name}")

    if not conference_name and action == 'hold':
        # No conference yet — this is an outbound call using simple <Dial>.
        # Create a conference on-the-fly: redirect the child call to hold
        # music, and the agent's <Dial> action will put them in a conference.
        return _hold_outbound_call(call_sid, db, twilio_service)

    if not conference_name and action == 'unhold':
        # DB lookup failed — try known conference name patterns as fallback.
        # _hold_outbound_call creates conferences named hold_{agent_call_sid}
        for pattern in [f"hold_{call_sid}", f"hold_room_{call_sid}"]:
            try:
                confs = twilio_list(twilio_service.client.conferences,
                    friendly_name=pattern, status='in-progress', limit=1
                )
                if confs:
                    conference_name = pattern
                    logger.info(f"Found conference by pattern: {pattern}")
                    break
            except Exception as e:
                logger.debug(f"Conference pattern {pattern} lookup failed: {e}")
        if not conference_name:
            logger.warning(f"Unhold failed: no conference for {call_sid}")
            return jsonify({"error": "No conference found for this call"}), 404

    try:
        # Find the conference SID — need it for the Twilio API
        conferences = twilio_list(twilio_service.client.conferences,
            friendly_name=conference_name,
            status='in-progress',
            limit=1
        )

        if not conferences:
            return jsonify({"error": "Conference not found or not active"}), 404

        conference = conferences[0]

        if action == 'hold':
            # Find the customer to hold from DB (fast) instead of listing
            # all Twilio participants (slow API call)
            customer_participants = db.get_participants(conference_name)
            customer_entry = next((p for p in customer_participants if p['role'] == 'customer'), None)

            if customer_entry:
                target_sid = customer_entry['call_sid']
            else:
                # Fallback: use child_sid or the call_sid itself
                child_sid = db.get_call_child_sid(call_sid)
                target_sid = child_sid or call_sid

            twilio_service.client.conferences(conference.sid).participants(target_sid).update(
                hold=True,
                hold_url=hold_url
            )
            db.log_activity(
                action="call_hold",
                target=conference_name,
                details=f"Caller {call_sid} put on hold",
                performed_by=get_api_caller()
            )
        else:
            # Unhold — find the customer and bring them into the conference
            child_sid = db.get_call_child_sid(call_sid)

            # If child_sid not in DB, find it via Twilio (fallback for older calls)
            if not child_sid:
                try:
                    child_calls = twilio_list(twilio_service.client.calls, 
                        parent_call_sid=call_sid, status='in-progress', limit=1
                    )
                    if child_calls:
                        child_sid = child_calls[0].sid
                    else:
                        # Agent might be the child (direct inbound) — check parent
                        call_info = twilio_service.client.calls(call_sid).fetch()
                        if call_info.parent_call_sid:
                            parent_status = twilio_service.client.calls(call_info.parent_call_sid).fetch().status
                            if parent_status == 'in-progress':
                                child_sid = call_info.parent_call_sid
                    if child_sid:
                        logger.info(f"Resolved child SID via Twilio: {child_sid}")
                except Exception as e:
                    logger.warning(f"Could not resolve child SID: {e}")

            # For queue calls, call_sid IS the customer — use it as fallback
            if not child_sid:
                child_sid = call_sid

            # Check if the customer is already a conference participant
            participants = twilio_list(twilio_service.client.conferences(conference.sid).participants)
            child_in_conference = any(p.call_sid == child_sid for p in participants)

            if child_sid and not child_in_conference:
                # Customer is playing hold music — redirect into conference
                conference_join_url = f"{config.webhook_base_url}/api/voice/conference/join?room={conference_name}&role=caller"
                twilio_service.client.calls(child_sid).update(
                    url=conference_join_url,
                    method='POST'
                )
                logger.info(f"Unhold: redirected {child_sid} into conference {conference_name}")
            elif child_in_conference:
                # Customer is in conference but on hold — unhold them
                twilio_service.client.conferences(conference.sid).participants(child_sid).update(
                    hold=False
                )
            else:
                return jsonify({"error": "Could not find the other party to resume"}), 404

            db.log_activity(
                action="call_unhold",
                target=conference_name,
                details=f"Caller {call_sid} taken off hold",
                performed_by=get_api_caller()
            )

        return jsonify({"success": True})

    except Exception as e:
        logger.error(f"Failed to {action} call {call_sid}: {e}")
        return jsonify({"error": str(e)}), 500


def _hold_outbound_call(agent_call_sid: str, db, twilio_service) -> tuple:
    """Create a conference on-demand for an outbound call and put caller on hold.

    When an agent is in a simple <Dial><Number> call (no conference), we:
    1. Find the child call (the external party)
    2. Redirect the child call to hold music
    3. The agent's <Dial> breaks → dial-status fires → agent joins conference
    4. On unhold, the child call is redirected into the conference

    Args:
        agent_call_sid: The agent's parent call SID
        db: Database instance
        twilio_service: Twilio service instance

    Returns:
        Flask response tuple
    """
    hold_url = f"{config.webhook_base_url}/api/voice/hold-music"

    try:
        # Find the other party: could be a child (outbound) or parent (direct inbound)
        child_calls = twilio_list(twilio_service.client.calls, parent_call_sid=agent_call_sid, limit=1)

        if child_calls:
            # Outbound: agent is parent, customer is child
            customer_call_sid = child_calls[0].sid
            if child_calls[0].status not in ('in-progress', 'ringing', 'queued'):
                return jsonify({"error": f"Call is {child_calls[0].status}, cannot hold"}), 400
        else:
            # Direct inbound: agent is child, customer is parent
            call_info = twilio_service.client.calls(agent_call_sid).fetch()
            if call_info.parent_call_sid:
                customer_call_sid = call_info.parent_call_sid
            else:
                return jsonify({"error": "No active call found to hold"}), 404

        # Create conference name and store it
        conference_name = f"hold_{agent_call_sid}"
        db.set_call_conference(agent_call_sid, conference_name)

        # Store the customer call SID for later unhold
        db.set_call_child_sid(agent_call_sid, customer_call_sid)

        agent_conf_url = f"{config.webhook_base_url}/api/voice/conference/join?room={conference_name}&role=agent"

        if child_calls:
            # Outbound: agent is parent, customer is child.
            # Redirect child (customer) to hold music first — this breaks the
            # Dial bridge.  The dial-status callback sees the pending conference
            # and redirects the agent into it automatically.
            twilio_service.client.calls(customer_call_sid).update(
                url=hold_url,
                method='POST'
            )
        else:
            # Direct inbound: agent is child, customer is parent.
            # Must redirect agent (child) first — redirecting parent would kill
            # the child call.  Then redirect customer to hold music.
            twilio_service.client.calls(agent_call_sid).update(url=agent_conf_url, method='POST')
            twilio_service.client.calls(customer_call_sid).update(
                url=hold_url,
                method='POST'
            )

        logger.info(f"Hold: redirected customer {customer_call_sid} to hold music, conference={conference_name}")

        db.log_activity(
            action="call_hold",
            target=conference_name,
            details=f"Agent {agent_call_sid}, customer {customer_call_sid} → hold music",
            performed_by=get_api_caller()
        )

        return jsonify({"success": True})

    except Exception as e:
        logger.error(f"Failed to hold outbound call {agent_call_sid}: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.route('/voice/queue/<int:queue_id>/wait', methods=['POST'])
def queue_wait(queue_id):
    """Handle queue wait - plays hold music and position announcements.

    Twilio calls this URL while caller is waiting in queue.
    We return TwiML to play hold music and optionally announce position.

    On first call (QueueTime=0), we add the caller to the database so they
    appear in the queue dashboard. This ensures callers only show up after
    any greeting has finished playing.

    No auth required - Twilio calls this directly.
    """
    db = get_db()
    queue = db.get_queue(queue_id)

    # Get call info from Twilio
    call_sid = request.form.get('CallSid', '')
    from_number = request.form.get('From', '')
    called_number = request.form.get('To', '')
    queue_time = request.form.get('QueueTime', '0')  # Seconds in queue
    queue_position = request.form.get('QueuePosition', '1')

    # On first entry to queue (QueueTime=0), add caller to database
    if queue_time == '0' and call_sid:
        # Check if already in DB (shouldn't be, but just in case)
        existing = db.get_queued_call_by_sid(call_sid)
        if not existing and queue:
            # Enrich caller data from Clara/Otto
            from rinq.services.caller_enrichment import get_enrichment_service
            enrichment = get_enrichment_service()
            caller_info = enrichment.enrich_caller(from_number)

            # Store enriched queue entry - NOW the caller appears in the dashboard
            db.add_queued_call({
                'call_sid': call_sid,
                'queue_id': queue_id,
                'queue_name': queue.get('name'),
                'caller_number': from_number,
                'called_number': called_number,
                'customer_id': caller_info.get('customer_id'),
                'customer_name': caller_info.get('customer_name'),
                'customer_email': caller_info.get('customer_email'),
                'order_data': caller_info.get('order_data'),
                'priority': caller_info.get('priority', 'unknown'),
                'priority_reason': caller_info.get('priority_reason'),
            })

            db.log_activity(
                action="call_queued",
                target=called_number,
                details=f"From: {from_number}, Queue: {queue.get('name')}, Customer: {caller_info.get('customer_name') or 'Unknown'}",
                performed_by="twilio"
            )

            # Auto-ring agents when caller enters queue
            # - SIP calls use customer's number so agents see who's calling on desk phone
            # - Mobile calls use our Twilio number (required - can only use numbers we own)
            get_twilio_service().capture_for_thread()
            _ring_agents_for_queue(queue_id, queue.get('name'), from_number, called_number, call_sid, base_url=config.webhook_base_url)

    if not queue:
        # Queue not found - play default hold music from static/ (safe from admin deletion)
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">Please hold, we will be with you shortly.</Say>
    <Play>{config.webhook_base_url}/static/clockwork_waltz_60s.mp3</Play>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    queue_time_int = int(queue_time) if queue_time else 0
    queue_position_int = int(queue_position) if queue_position else 1

    # Handle DTMF digits — Gather (no action URL) posts back to this waitUrl
    digits = request.form.get('Digits', '')
    if digits:
        return queue_escape(queue_id)

    # Check if anyone is actually available to answer — if not, go to voicemail
    # immediately rather than making the caller wait for nothing.
    # Skip this on first entry (QueueTime=0) to let the greeting play first.
    if queue_time_int > 0:
        members = db.get_queue_members(queue_id)
        anyone_available = False
        for m in members:
            if not m.get('is_active'):
                continue
            settings = db.get_user_ring_settings(m['user_email'])
            if settings.get('dnd'):
                continue
            if settings.get('ring_browser', True) or settings.get('ring_sip', True):
                anyone_available = True
                break
        if not anyone_available:
            logger.info(f"Queue {queue_id}: no available agents for {call_sid} — sending to voicemail")
            twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Leave/>
</Response>'''
            return Response(twiml, mimetype='application/xml')

    # Enforce max wait (in music plays) — redirect to voicemail if exceeded
    # Default to 10 plays (~10 min) as a safety net so callers never loop forever
    max_wait_plays = queue.get('max_wait_time') or 10
    if queue_time_int > 0:
        cycle = round(queue_time_int / 60)  # each waitUrl cycle ≈ hold music duration
        if cycle >= max_wait_plays:
            logger.info(f"Queue {queue_id} max wait ({max_wait_plays} plays) exceeded for {call_sid} (cycle {cycle}, waited {queue_time_int}s)")
            # Leave the queue — Twilio will call the action URL with result=redirected
            twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Leave/>
</Response>'''
            return Response(twiml, mimetype='application/xml')

    response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']

    is_first_entry = queue_time_int == 0
    allow_voicemail_escape = queue.get('allow_voicemail_escape')
    offer_callback = queue.get('offer_callback')
    callback_threshold = queue.get('callback_threshold', 60)
    callback_eligible = offer_callback and queue_time_int >= callback_threshold

    # Position announcement (if enabled) - but skip if they're position 1
    # (no point telling them they're first when no one is answering)
    if queue.get('position_announcement') and queue_position_int > 1:
        announcement_interval = queue.get('announcement_interval', 60)

        # Announce position periodically
        if is_first_entry or queue_time_int % announcement_interval < 10:
            response_parts.append(f'    <Say voice="Polly.Nicole">You are caller number {queue_position} in the queue.</Say>')

            # Estimated wait (if enabled and we have data)
            if queue.get('estimated_wait_announcement'):
                # Simple estimate: 2 minutes per caller ahead
                est_minutes = queue_position_int * 2
                if est_minutes > 0:
                    response_parts.append(f'    <Say voice="Polly.Nicole">Estimated wait time is approximately {est_minutes} minutes.</Say>')

    # Voicemail/callback escape announcements
    # Both settings are in music plays (waitUrl cycles). Each cycle ≈ hold music duration.
    # escape_delay: how many music plays before first announcement (1 = after first track)
    # escape_repeat: how many plays between repeats (2 = every other play, 0 = once only)
    escape_delay = queue.get('escape_announcement_delay', 1)
    escape_repeat = queue.get('escape_repeat_interval', 2)
    should_announce_escape = False
    if allow_voicemail_escape or offer_callback:
        # Estimate which music cycle we're on (each cycle ≈ 60s for recommended track length)
        cycle = round(queue_time_int / 60) if queue_time_int > 0 else 0
        if cycle >= escape_delay:
            cycles_since_first = cycle - escape_delay
            if cycles_since_first == 0:
                # First announcement
                should_announce_escape = True
            elif escape_repeat and escape_repeat > 0 and cycles_since_first % escape_repeat == 0:
                # Repeat cycle hit
                should_announce_escape = True
            # escape_repeat == 0 means announce once only
    if should_announce_escape:
        # Pick the right audio type based on queue config
        if allow_voicemail_escape and offer_callback:
            audio_type = 'queue_welcome_vm_cb'
            fallback_say = 'Press 1 at any time to leave a voicemail, or press 2 to request a callback instead of waiting.'
        elif allow_voicemail_escape:
            audio_type = 'queue_welcome_vm'
            fallback_say = 'Press 1 at any time to leave a voicemail instead of waiting.'
        else:
            audio_type = 'queue_welcome_cb'
            fallback_say = 'Press 2 at any time to request a callback instead of waiting.'

        # Look for recorded audio of this type
        type_audio = db.get_audio_files(file_type=audio_type)
        if type_audio and type_audio[0].get('file_url'):
            audio_url = _get_full_audio_url(type_audio[0]['file_url'])
            response_parts.append(f'    <Play>{xml_escape(audio_url)}</Play>')
        else:
            response_parts.append(f'    <Say voice="Polly.Nicole">{fallback_say}</Say>')

    # Note: callback_reminder audio type is still supported for the reference table
    # but repeating escape announcements now cover the reminder functionality

    # Build hold music URL — fall back to static/ file (safe from admin deletion)
    hold_music_url = None
    if queue.get('hold_music_id'):
        audio = db.get_audio_file(queue['hold_music_id'])
        if audio and audio.get('file_url'):
            hold_music_url = _get_full_audio_url(audio['file_url'])
    if not hold_music_url:
        hold_music_url = f'{config.webhook_base_url}/static/clockwork_waltz_60s.mp3'

    # Hold music — play once, then TwiML ends and Twilio re-calls waitUrl.
    # If escape options are enabled, use Gather to detect DTMF during music.
    # No action URL — on timeout (no digits), Gather falls through to end of
    # TwiML, and Twilio re-calls waitUrl. On digit press, Twilio posts back
    # to the current URL (waitUrl) with Digits param.
    needs_gather = allow_voicemail_escape or callback_eligible
    if needs_gather:
        response_parts.append('    <Gather input="dtmf" numDigits="1">')
        response_parts.append(f'        <Play>{xml_escape(hold_music_url)}</Play>')
        response_parts.append('    </Gather>')
    else:
        response_parts.append(f'    <Play>{xml_escape(hold_music_url)}</Play>')

    response_parts.append('</Response>')

    twiml = '\n'.join(response_parts)
    logger.info(f"Queue {queue_id} wait TwiML: QueueTime={queue_time}, escape_delay={escape_delay}, "
                f"allow_vm={allow_voicemail_escape}, should_announce={should_announce_escape}, "
                f"needs_gather={needs_gather}, TwiML={twiml[:500]}")
    return Response(twiml, mimetype='application/xml')


# =============================================================================
# Queue Escape (unified handler for voicemail + callback)
# =============================================================================

@api_bp.route('/voice/queue/<int:queue_id>/queue-escape', methods=['POST'])
def queue_escape(queue_id):
    """Unified handler for queue escape options (voicemail + callback).

    Called by the <Gather> in queue_wait when the caller presses a digit.
    - Press 1: Leave voicemail (same as voicemail-escape)
    - Press 2: Request callback (hang up, keep place in line)
    - Other digits: Return to hold music

    No auth required - Twilio calls this directly.
    """
    db = get_db()
    queue = db.get_queue(queue_id)

    digits = request.form.get('Digits', '')
    call_sid = request.form.get('CallSid', '')
    from_number = request.form.get('From', '')

    logger.info(f"Queue escape: queue={queue_id}, digits={digits}, call_sid={call_sid}")

    if digits == '1' and queue and queue.get('allow_voicemail_escape'):
        # Redirect to voicemail escape handler
        logger.info(f"Caller {from_number} chose voicemail escape from queue {queue.get('name') if queue else queue_id}")

        # Cancel any auto-ring calls for this customer
        import threading
        def cancel_calls():
            _cancel_agent_calls(call_sid)
        threading.Thread(target=cancel_calls, daemon=True).start()

        # Update queued call status
        queued_call = db.get_queued_call_by_sid(call_sid)
        if queued_call:
            db.update_queued_call_status(call_sid, 'voicemail')

        db.log_activity(
            action="caller_chose_voicemail",
            target=f"queue_{queue_id}",
            details=f"Caller {from_number} pressed 1 to leave voicemail instead of waiting",
            performed_by="twilio"
        )

        # <Record> is not valid in queue wait context — must <Leave/> first.
        # queue_leave handler checks for 'voicemail' status and routes accordingly.
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Leave/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    elif digits == '2' and queue and queue.get('offer_callback'):
        # Callback request
        logger.info(f"Caller {from_number} requested callback from queue {queue.get('name') if queue else queue_id}")

        # Cancel any auto-ring calls for this customer
        import threading
        def cancel_calls():
            _cancel_agent_calls(call_sid)
        threading.Thread(target=cancel_calls, daemon=True).start()

        # Get caller name from queued call data
        queued_call = db.get_queued_call_by_sid(call_sid)
        customer_name = queued_call.get('customer_name') if queued_call else None

        # Create callback request
        db.create_callback_request(
            queue_id=queue_id,
            customer_phone=from_number,
            customer_name=customer_name,
            call_sid=call_sid,
        )

        # Update queued call status
        if queued_call:
            db.update_queued_call_status(call_sid, 'callback')

        db.log_activity(
            action="caller_requested_callback",
            target=f"queue_{queue_id}",
            details=f"Caller {from_number} pressed 2 to request callback instead of waiting",
            performed_by="twilio"
        )

        # <Hangup> is not valid in queue wait context — must <Leave/> first.
        # queue_leave handler checks for 'callback' status and plays confirmation.
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Leave/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    else:
        # Unknown digit or feature not enabled - play hold music
        # TwiML ends after this, Twilio re-calls waitUrl
        if digits:
            logger.info(f"Caller pressed {digits} (invalid), returning to queue")
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{config.webhook_base_url}/static/clockwork_waltz_60s.mp3</Play>
</Response>'''
        return Response(twiml, mimetype='application/xml')


# =============================================================================
# Extension Directory (Auto-Attendant)
# =============================================================================

@api_bp.route('/voice/extension-dial', methods=['POST'])
def voice_extension_dial():
    """Handle extension input from the auto-attendant Gather.

    Looks up the 4-digit extension, builds dial targets for that user,
    and connects the call. Falls back on invalid extension or no answer.

    No auth required - Twilio calls this directly.
    """
    digits = request.form.get('Digits', '')
    call_sid = request.form.get('CallSid', '')
    called_number = request.args.get('called', '').strip()
    from_number = request.args.get('from', '').strip()
    flow_id = request.args.get('flow_id', '')
    attempt = int(request.args.get('attempt', '1'))

    # Restore + prefix if lost during URL decoding (+ decoded as space)
    from rinq.services.phone import ensure_plus
    called_number = ensure_plus(called_number)
    from_number = ensure_plus(from_number)

    logger.info(f"Extension dial: digits={digits}, from={from_number}, called={called_number}, attempt={attempt}")

    db = get_db()

    # Look up the extension
    ext_record = db.get_staff_extension_by_ext(digits)

    if not ext_record:
        logger.info(f"Invalid extension: {digits}")

        db.log_activity(
            action="extension_invalid",
            target=called_number,
            details=f"From: {from_number}, Invalid extension: {digits}, Attempt: {attempt}",
            performed_by="twilio"
        )

        if attempt < 2:
            # Re-prompt once
            call_flow = db.get_call_flow(int(flow_id)) if flow_id else None
            gather_url = f"{config.webhook_base_url}/api/voice/extension-dial?called={quote(called_number, safe='')}&from={quote(from_number, safe='')}&flow_id={flow_id}&attempt=2"
            response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']

            # Invalid extension message - custom audio or TTS
            invalid_audio = None
            if call_flow and call_flow.get('extension_invalid_audio_id'):
                invalid_audio = db.get_audio_file(call_flow['extension_invalid_audio_id'])
            if invalid_audio and invalid_audio.get('file_url'):
                response_parts.append(f'    <Play>{xml_escape(_get_full_audio_url(invalid_audio["file_url"]))}</Play>')
            else:
                response_parts.append('    <Say voice="Polly.Nicole">That extension is not valid. Please try again.</Say>')

            response_parts.append(f'    <Gather numDigits="4" action="{xml_escape(gather_url)}" method="POST" timeout="10">')

            if call_flow and call_flow.get('extension_prompt_audio_id'):
                audio = db.get_audio_file(call_flow['extension_prompt_audio_id'])
                if audio and audio.get('file_url'):
                    audio_url = _get_full_audio_url(audio['file_url'])
                    response_parts.append(f'        <Play>{xml_escape(audio_url)}</Play>')
                else:
                    response_parts.append(_say_or_play('ext_prompt', 'Please enter the extension of the person you are trying to reach.', indent='        '))
            else:
                response_parts.append(_say_or_play('ext_prompt', 'Please enter the extension of the person you are trying to reach.', indent='        '))

            response_parts.append('    </Gather>')
            response_parts.append('    <Say voice="Polly.Nicole">No extension entered. Goodbye.</Say>')
            response_parts.append('    <Hangup/>')
            response_parts.append('</Response>')
            return Response('\n'.join(response_parts), mimetype='application/xml')
        else:
            # Max retries - execute fallback
            return _extension_fallback(flow_id, called_number, from_number, call_sid, db)

    # Extension found - build dial targets for this user
    user_email = ext_record.get('email')
    # Get the user's friendly name from the users table
    user_record = db.get_user_by_email(user_email) if user_email else None
    staff_name = (user_record.get('friendly_name') if user_record else None) or user_email or digits
    logger.info(f"Extension {digits} matched: {user_email} ({staff_name})")

    # Lazy name-change detection: if the name has changed since audio was generated,
    # clear the audio so <Say> fallback is used until regeneration
    if (ext_record.get('name_audio_path')
            and ext_record.get('name_audio_text') != staff_name
            and staff_name != user_email):
        logger.info(f"Name mismatch for ext {digits}: audio='{ext_record.get('name_audio_text')}' vs current='{staff_name}' - clearing audio")
        db.clear_staff_name_audio(user_email, 'system:name_change_detected')
        ext_record['name_audio_path'] = None
        ext_record['name_audio_text'] = None

    if not user_email:
        logger.warning(f"Extension {digits} has no staff_email assigned")
        return _extension_fallback(flow_id, called_number, from_number, call_sid, db)

    # Check DND - tell the caller the person is unavailable, then fallback
    if ext_record.get('dnd_enabled'):
        logger.info(f"Extension {digits} ({user_email}) has DND enabled - going to fallback")
        db.log_activity(
            action="extension_dnd",
            target=called_number,
            details=f"From: {from_number}, Extension: {digits}, User: {user_email} has DND enabled",
            performed_by="twilio"
        )
        return _extension_fallback(flow_id, called_number, from_number, call_sid, db,
                                   say_first=f"{staff_name} is currently unavailable.")

    # Build routing dict for _build_dial_targets
    ring_settings = db.get_user_ring_settings(user_email)
    routing = {
        'assignments': [user_email],
        'user_settings': {user_email: ring_settings},
    }
    dial_targets = _build_dial_targets(routing)

    if not dial_targets:
        logger.warning(f"No dial targets for extension {digits} ({user_email})")
        return _extension_fallback(flow_id, called_number, from_number, call_sid, db)

    # Conference-first: caller joins conference, ring target devices via REST API
    conference_name = f"call_{call_sid}"
    db.set_call_conference(call_sid, conference_name)

    get_twilio_service().capture_for_thread()
    _ring_targets_into_conference(dial_targets, conference_name, from_number, call_sid, base_url=config.webhook_base_url, db=db)

    no_answer_url = f"{config.webhook_base_url}/api/voice/extension-no-answer?called={quote(called_number, safe='')}&from={quote(from_number, safe='')}&flow_id={flow_id}"
    ringback_url = f"{config.webhook_base_url}/api/voice/ringback"
    response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']
    # Use pre-generated audio if available, otherwise fall back to <Say>
    connecting_prefix = db.get_bot_setting('connecting_prefix_audio_path')
    name_audio = ext_record.get('name_audio_path')
    if connecting_prefix and name_audio:
        prefix_url = _get_full_audio_url(connecting_prefix)
        name_url = _get_full_audio_url(name_audio)
        response_parts.append(f'    <Play>{xml_escape(prefix_url)}</Play>')
        response_parts.append(f'    <Play>{xml_escape(name_url)}</Play>')
    else:
        response_parts.append(f'    <Say voice="Polly.Nicole">Connecting you to {xml_escape(staff_name)}.</Say>')
    response_parts.append('    <Dial>')
    response_parts.append(f'        <Conference startConferenceOnEnter="false" endConferenceOnExit="true" beep="false" waitUrl="{xml_escape(ringback_url)}" waitMethod="POST">{xml_escape(conference_name)}</Conference>')
    response_parts.append('    </Dial>')
    response_parts.append(f'    <Redirect>{xml_escape(no_answer_url)}</Redirect>')
    response_parts.append('</Response>')

    db.log_activity(
        action="extension_dialing",
        target=called_number,
        details=f"From: {from_number}, Extension: {digits}, User: {user_email}, Targets: {len(dial_targets)}, Conference: {conference_name}",
        performed_by="twilio"
    )

    return Response('\n'.join(response_parts), mimetype='application/xml')


@api_bp.route('/voice/extension-no-answer', methods=['POST'])
def voice_extension_no_answer():
    """Handle when an extension user doesn't answer.

    Checks DialCallStatus and executes the configured fallback action.

    No auth required - Twilio calls this directly.
    """
    try:
        dial_status = request.form.get('DialCallStatus', '')
        call_sid = request.form.get('CallSid', '')
        called_number = request.args.get('called', '').strip()
        from_number = request.args.get('from', '').strip()
        flow_id = request.args.get('flow_id', '')

        from rinq.services.phone import ensure_plus
        called_number = ensure_plus(called_number)
        from_number = ensure_plus(from_number)

        logger.info(f"Extension no-answer: status={dial_status}, from={from_number}, flow_id={flow_id}")

        if dial_status in ('completed', 'answered'):
            # Call was answered and completed normally
            twiml = '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Hangup/>\n</Response>'
            return Response(twiml, mimetype='application/xml')

        # For conference-first calls, DialCallStatus isn't set.
        # Check the DB to see if the call was actually answered.
        db = get_db()
        if not dial_status:
            call_status = db.get_call_log_field(call_sid, 'status')
            if call_status in ('answered', 'completed', 'transferred'):
                twiml = '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Hangup/>\n</Response>'
                return Response(twiml, mimetype='application/xml')

        # Not answered - execute fallback
        return _extension_fallback(flow_id, called_number, from_number, call_sid, db)
    except Exception as e:
        logger.exception(f"Error in extension-no-answer: {e}")
        ext_msg = _say_or_play('ext_unavailable', 'Sorry, that extension is not available right now. Please try again later.')
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
{ext_msg}
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')


def _extension_fallback(flow_id, called_number, from_number, call_sid, db, say_first=None):
    """Execute the configured fallback action for extension directory.

    Args:
        say_first: Optional message to say before the fallback action (e.g. DND message).
    """
    call_flow = db.get_call_flow(int(flow_id)) if flow_id and flow_id != 'None' else None
    fallback_action = (call_flow.get('extension_no_answer_action', 'voicemail')
                       if call_flow else 'voicemail')

    logger.info(f"Extension fallback: action={fallback_action}, flow_id={flow_id}")

    response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']

    if say_first:
        response_parts.append(f'    <Say voice="Polly.Nicole">{xml_escape(say_first)}</Say>')

    if fallback_action == 'ai_receptionist':
        routing = {'call_flow': call_flow} if call_flow else {}
        return _redirect_to_ai_receptionist(
            response_parts, called_number, from_number, call_sid, db, routing,
            reason="extension_no_answer"
        )
    elif fallback_action == 'queue' and call_flow and call_flow.get('open_queue_id'):
        # Route to the configured queue
        queue_id = call_flow['open_queue_id']
        queue = db.get_queue(queue_id)
        if queue:
            queue_name = f"queue_{queue_id}"
            wait_url = f"{config.webhook_base_url}/api/voice/queue/{queue_id}/wait"
            leave_url = f"{config.webhook_base_url}/api/voice/queue/{queue_id}/leave"
            if not say_first:
                response_parts.append(f'    <Say voice="Polly.Nicole">That extension is not available. Transferring you now.</Say>')
            response_parts.append(f'    <Enqueue waitUrl="{xml_escape(wait_url)}" waitUrlMethod="POST" action="{xml_escape(leave_url)}">{xml_escape(queue_name)}</Enqueue>')
            response_parts.append('</Response>')
            return Response('\n'.join(response_parts), mimetype='application/xml')

    # Default: voicemail (let _go_to_voicemail handle the prompt based on config)
    routing = {'call_flow': call_flow} if call_flow else {}
    if not say_first:
        response_parts.append('    <Say voice="Polly.Nicole">That extension is not available.</Say>')
    return _go_to_voicemail(response_parts, call_flow, called_number, from_number, call_sid, db, routing)


@api_bp.route('/voice/voicemail', methods=['POST'])
def voicemail_handler():
    """Handle voicemail recording completion.

    Twilio calls this when a voicemail recording is done.
    We log it and email it to the configured address via Mabel.

    No auth required - Twilio calls this directly.
    """
    import base64
    import requests

    recording_url = request.form.get('RecordingUrl')
    recording_sid = request.form.get('RecordingSid')
    recording_duration = request.form.get('RecordingDuration')
    from_number = request.form.get('From')
    to_number = request.form.get('To')
    transcription_text = request.form.get('TranscriptionText', '')
    call_sid = request.form.get('CallSid')

    db = get_db()

    # Log the voicemail
    db.log_activity(
        action="voicemail_received",
        target=to_number,
        details=f"From: {from_number}, Duration: {recording_duration}s, URL: {recording_url}",
        performed_by="twilio"
    )

    # Look up call flow and voicemail destination for this number
    phone_record = db.get_phone_number_by_number(to_number)
    call_flow = None
    voicemail_dest = None

    if phone_record and phone_record.get('call_flow_id'):
        call_flow = db.get_call_flow(phone_record['call_flow_id'])
        if call_flow:
            # Try new voicemail_destination_id first, fall back to legacy voicemail_email
            if call_flow.get('voicemail_destination_id'):
                voicemail_dest = db.get_voicemail_destination(call_flow['voicemail_destination_id'])
            elif call_flow.get('voicemail_email'):
                voicemail_dest = db.get_voicemail_destination_by_email(call_flow['voicemail_email'])

            if voicemail_dest:
                logger.info(f"Voicemail lookup: phone={to_number}, call_flow={call_flow.get('name')}, "
                           f"destination={voicemail_dest['name']} ({voicemail_dest.get('routing_type', 'email')})")
            else:
                logger.warning(f"Voicemail lookup: call_flow={call_flow.get('name')} has no valid voicemail destination")
        else:
            logger.warning(f"Voicemail lookup: call_flow_id={phone_record.get('call_flow_id')} not found")
    else:
        logger.warning(f"Voicemail lookup: phone={to_number}, call_flow_id={phone_record.get('call_flow_id') if phone_record else 'no phone record'}")

    # Log the recording with call_type = 'voicemail'
    # INSERT OR IGNORE handles duplicate Twilio webhooks — if the recording_sid
    # already exists, lastrowid will be 0 and we skip routing to avoid duplicate tickets.
    row_id = db.log_recording({
        "recording_sid": recording_sid,
        "call_sid": call_sid,
        "from_number": from_number,
        "to_number": to_number,
        "duration_seconds": int(recording_duration) if recording_duration else None,
        "recording_url": recording_url,
        "call_type": "voicemail",
        "emailed_to": voicemail_dest['name'] if voicemail_dest else None,
        "emailed_at": datetime.now(timezone.utc).isoformat() if voicemail_dest else None,
        "deleted_from_twilio": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    if not row_id:
        logger.info(f"Voicemail {recording_sid} already logged — duplicate webhook, skipping")
        return '<Response/>', 200

    # Route voicemail if destination configured
    if voicemail_dest:
        routing_type = voicemail_dest.get('routing_type', 'zendesk')

        # For email routing type without zendesk_group_id, log warning (not yet implemented)
        if routing_type == 'email' and not voicemail_dest.get('zendesk_group_id'):
            logger.warning(f"Email routing not yet implemented for destination '{voicemail_dest['name']}'")
            db.log_activity(
                action="voicemail_routing_skipped",
                target=to_number,
                details=f"Email routing not implemented for '{voicemail_dest['name']}'",
                performed_by="tina"
            )
        else:
            # Zendesk ticket routing
            try:
                import time

                # Download recording from Twilio (add .mp3 extension for audio format)
                audio_url = f"{recording_url}.mp3"

                # Twilio requires auth to download recordings
                # Recording might not be immediately available - retry with backoff
                auth = (get_twilio_config('twilio_account_sid'), get_twilio_config('twilio_auth_token'))
                audio_content = None
                max_retries = 5
                for attempt in range(max_retries):
                    audio_response = requests.get(audio_url, auth=auth, timeout=30)
                    if audio_response.status_code == 200:
                        audio_content = base64.b64encode(audio_response.content).decode('utf-8')
                        break
                    elif audio_response.status_code == 404 and attempt < max_retries - 1:
                        # Recording not ready yet - wait and retry
                        wait_time = 2 ** attempt  # 1, 2, 4, 8 seconds
                        logger.info(f"Recording not ready (attempt {attempt + 1}), waiting {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        audio_response.raise_for_status()

                if not audio_content:
                    raise Exception("Failed to download recording after retries")

                # Format the ticket
                flow_name = call_flow.get('name', 'Unknown') if call_flow else 'Unknown'
                friendly_name = phone_record.get('friendly_name', to_number) if phone_record else to_number

                subject = f"Voicemail from {from_number} ({friendly_name})"

                text_body = f"""New voicemail received:

From: {from_number}
To: {friendly_name} ({to_number})
Duration: {recording_duration} seconds
Call Flow: {flow_name}

"""
                if transcription_text:
                    text_body += f"Transcription:\n{transcription_text}\n\n"
                else:
                    text_body += "(Transcription pending or unavailable)\n\n"

                text_body += "The voicemail recording is attached.\n"

                zendesk_group_id = voicemail_dest.get('zendesk_group_id')
                logger.info(f"Voicemail routing: destination '{voicemail_dest['name']}' -> Zendesk group {zendesk_group_id}")

                # Create ticket via ticket service
                from rinq.integrations import get_ticket_service
                tickets = get_ticket_service()
                ticket_data = tickets.create_ticket(
                    subject=subject,
                    description=text_body,
                    priority='normal',
                    ticket_type='task',
                    tags=['voicemail', 'tina'],
                    requester_email='tina.bot@watsonblinds.com.au',
                    requester_name=f"{config.product_name} (Phone System)",
                    group_id=zendesk_group_id,
                    attachments=[{
                        'filename': f"voicemail_{recording_sid}.mp3",
                        'content_type': 'audio/mpeg',
                        'content_base64': audio_content,
                    }],
                ) if tickets else None

                if ticket_data:
                    ticket_id = ticket_data.get('id')
                    group_info = f", group_id={zendesk_group_id}" if zendesk_group_id else ""
                    logger.info(f"Voicemail ticket #{ticket_id} created for call {call_sid}{group_info}")

                    if ticket_id:
                        db.update_recording_ticket(recording_sid, ticket_id)

                    db.log_activity(
                        action="voicemail_ticket_created",
                        target=to_number,
                        details=f"Ticket #{ticket_id} created, recording {recording_sid}{group_info}",
                        performed_by="tina"
                    )
                else:
                    logger.error("Failed to create voicemail ticket")
                    db.log_activity(
                        action="voicemail_ticket_failed",
                        target=to_number,
                        details="Ticket service returned no data",
                        performed_by="tina"
                    )

            except Exception as e:
                logger.error(f"Error creating voicemail ticket: {e}")
                db.log_activity(
                    action="voicemail_ticket_failed",
                    target=to_number,
                    details=f"Error: {e}",
                    performed_by="tina"
                )
    else:
        # Log why voicemail wasn't routed
        if not phone_record:
            reason = "Phone number not found in database"
        elif not phone_record.get('call_flow_id'):
            reason = "No call flow assigned to phone number"
        elif not call_flow:
            reason = f"Call flow ID {phone_record.get('call_flow_id')} not found"
        else:
            reason = "No voicemail destination configured on call flow"
        logger.warning(f"Voicemail not routed: {reason}")
        db.log_activity(
            action="voicemail_no_routing",
            target=to_number,
            details=reason,
            performed_by="tina"
        )

    # Return empty response - call is done
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                   mimetype='application/xml')


@api_bp.route('/voice/transcription', methods=['POST'])
def transcription_handler():
    """Handle voicemail transcription callback from Twilio.

    Twilio calls this when transcription completes (async, after recording).
    We update the recording and the linked Zendesk ticket if present.

    No auth required - Twilio calls this directly.
    """
    recording_sid = request.form.get('RecordingSid')
    transcription_text = request.form.get('TranscriptionText', '')
    transcription_status = request.form.get('TranscriptionStatus', '')
    transcription_sid = request.form.get('TranscriptionSid')

    logger.info(f"Transcription callback: recording={recording_sid}, status={transcription_status}, "
                f"text_length={len(transcription_text) if transcription_text else 0}")

    db = get_db()

    # Update the recording with transcription
    if not recording_sid:
        logger.warning("Transcription callback missing RecordingSid")
        return jsonify({"error": "Missing RecordingSid"}), 400

    if transcription_status != 'completed':
        logger.warning(f"Transcription failed: {transcription_status}")
        db.log_activity(
            action="transcription_failed",
            target=recording_sid,
            details=f"Status: {transcription_status}",
            performed_by="twilio"
        )
        # Still delete the recording even if transcription failed
        try:
            twilio_service = get_twilio_service()
            twilio_service.delete_recording(recording_sid)
            db.mark_recording_deleted(recording_sid)
        except Exception as del_err:
            logger.warning(f"Failed to delete recording {recording_sid}: {del_err}")
        return jsonify({"status": "failed"}), 200

    # Save transcription to recording
    recording = db.update_recording_transcription(recording_sid, transcription_text)

    if not recording:
        logger.warning(f"Recording not found for transcription: {recording_sid}")
        return jsonify({"error": "Recording not found"}), 404

    db.log_activity(
        action="transcription_received",
        target=recording_sid,
        details=f"Length: {len(transcription_text)} chars",
        performed_by="twilio"
    )

    # If there's a linked Zendesk ticket, update it with the transcription
    ticket_id = recording.get('zendesk_ticket_id')
    if ticket_id and transcription_text:
        from rinq.integrations import get_ticket_service
        tickets = get_ticket_service()
        if tickets:
            comment_body = f"📝 **Voicemail Transcription:**\n\n{transcription_text}"
            if tickets.add_comment(str(ticket_id), comment_body, public=False):
                logger.info(f"Updated ticket #{ticket_id} with transcription")
                db.log_activity(
                    action="transcription_added_to_ticket",
                    target=str(ticket_id),
                    details=f"Recording {recording_sid}",
                    performed_by="tina"
                )
            else:
                logger.warning(f"Failed to update ticket {ticket_id} with transcription")

    # Only delete if ticket was already created (avoid race with voicemail handler)
    # If ticket doesn't exist yet, voicemail handler is still working - let it finish
    # The nightly cleanup job will catch any stragglers
    if ticket_id:
        try:
            twilio_service = get_twilio_service()
            twilio_service.delete_recording(recording_sid)
            db.mark_recording_deleted(recording_sid)
            db.log_activity(
                action="recording_deleted",
                target=recording_sid,
                details=f"Deleted from Twilio after transcription received",
                performed_by="tina"
            )
        except Exception as del_err:
            logger.warning(f"Failed to delete recording {recording_sid} from Twilio: {del_err}")
    else:
        logger.info(f"Skipping deletion of {recording_sid} - ticket not created yet, voicemail handler still working")

    return jsonify({"status": "ok"}), 200


# =============================================================================
# Browser Softphone (Twilio Client)
# =============================================================================

@api_bp.route('/voice/token', methods=['POST'])
@login_required
def get_voice_token():
    """Generate an access token for the browser softphone.

    The token allows the browser to connect to Twilio and make/receive calls.
    Requires user to be logged in.
    """
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant

    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    service = get_twilio_service()
    if not service.is_configured:
        return jsonify({"error": "Twilio not configured"}), 500

    # Get tenant-specific Twilio creds
    account_sid = get_twilio_config('twilio_account_sid')
    api_key = get_twilio_config('twilio_api_key')
    api_secret = get_twilio_config('twilio_api_secret')
    twiml_app_sid = get_twilio_config('twilio_twiml_app_sid')

    if not api_key or not api_secret:
        return jsonify({"error": "Twilio API Key not configured."}), 500

    logger.info(f"Token generation - Account: {account_sid[:10] if account_sid else 'None'}...")
    logger.info(f"Token generation - API Key: {api_key[:10] if api_key else 'None'}...")

    # Create a unique identity for this user
    identity = user.email.replace('@', '_at_').replace('.', '_')

    # Create access token
    token = AccessToken(
        account_sid,
        api_key,
        api_secret,
        identity=identity,
        ttl=3600
    )

    # Create Voice grant
    voice_grant = VoiceGrant(
        outgoing_application_sid=twiml_app_sid,
        incoming_allow=True
    )
    token.add_grant(voice_grant)

    db = get_db()
    db.log_activity(
        action="voice_token_generated",
        target=identity,
        details=f"Browser softphone token for {user.email}",
        performed_by=f"session:{user.email}"
    )

    return jsonify({
        "token": token.to_jwt(),
        "identity": identity
    })


@api_bp.route('/voice/call-outcome', methods=['GET'])
@login_required
def voice_call_outcome():
    """Get the final status of a call (for showing outcome to the user).

    Query params:
        call_sid: The call SID to look up

    Returns:
        {"status": "answered|busy|missed|failed|abandoned"}
    """
    call_sid = request.args.get('call_sid')
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    db = get_db()
    status = db.get_call_log_field(call_sid, 'status')
    return jsonify({"status": status or "unknown"})


@api_bp.route('/voice/call-ended', methods=['POST'])
@login_required
def voice_call_ended():
    """Mark a call as ended in call_log.

    Called by the browser when a call ends (any type). This is the reliable
    signal that the call is done — Twilio status callbacks don't always fire
    for conference-based calls.
    """
    data = request.get_json() or {}
    call_sid = data.get('call_sid')
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    db = get_db()
    db.complete_call(call_sid=call_sid, status='answered')
    _handle_participant_left(call_sid, db)
    logger.info(f"Call ended (browser signal): {call_sid}")
    return jsonify({"success": True})


@api_bp.route('/voice/hangup', methods=['POST'])
@login_required
def voice_hangup():
    """Server-side call termination fallback.

    Ensures both call legs are terminated via the Twilio REST API,
    even if the browser-side SDK disconnect doesn't propagate cleanly.
    Called automatically by the softphone when the user clicks hangup.
    """
    data = request.get_json() or {}
    call_sid = data.get('call_sid')

    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    service = get_twilio_service()
    if not service.is_configured:
        return jsonify({"error": "Twilio not configured"}), 500

    user = get_current_user()
    db = get_db()

    # If there's a hold conference, end it and hang up the other party
    conference_name = db.get_call_conference(call_sid)
    if not conference_name:
        # Try known patterns (same fallback as unhold)
        for pattern in [f"hold_{call_sid}", f"hold_room_{call_sid}"]:
            try:
                confs = twilio_list(service.client.conferences,
                    friendly_name=pattern, status='in-progress', limit=1
                )
                if confs:
                    conference_name = pattern
                    break
            except Exception as e:
                logger.debug(f"Hold conference pattern {pattern} lookup failed: {e}")

    if conference_name:
        try:
            confs = twilio_list(service.client.conferences,
                friendly_name=conference_name, status='in-progress', limit=1
            )
            if confs:
                participants = twilio_list(service.client.conferences(confs[0].sid).participants)
                if len(participants) > 2:
                    # Multi-party (3-way) — just remove this agent, keep conference alive
                    logger.info(f"Leaving multi-party conference {conference_name} on hangup")
                else:
                    # 2-party — end the conference
                    service.client.conferences(confs[0].sid).update(status='completed')
                    logger.info(f"Ended conference {conference_name} on hangup")
        except Exception as e:
            logger.warning(f"Could not end conference {conference_name}: {e}")

    # Hang up the other party (child call) — they may still be ringing
    # or in a different state than the conference
    child_sid = db.get_call_child_sid(call_sid)
    if child_sid:
        try:
            service.client.calls(child_sid).update(status='completed')
            logger.info(f"Hung up other party {child_sid}")
        except Exception as e:
            logger.debug(f"Could not hang up other party {child_sid}: {e}")

    try:
        service.client.calls(call_sid).update(status='completed')
        logger.info(f"Server-side hangup: {call_sid} by {user.email}")
    except Exception as e:
        # Call may already be completed — that's fine
        logger.warning(f"Server-side hangup for {call_sid}: {e}")

    _handle_participant_left(call_sid, db)

    return jsonify({"success": True})


def _handle_internal_extension_call(extension: str, from_identity: str, staff_email: str, call_sid: str, db) -> Response:
    """Handle an internal extension-to-extension call from the softphone.

    When a softphone user dials a 4-digit extension, route the call directly
    to the recipient's devices without going through the PSTN/IVR.
    """
    ext_record = db.get_staff_extension_by_ext(extension)

    if not ext_record:
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">Extension {" ".join(extension)} is not valid.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    recipient_email = ext_record.get('email')
    recipient_user = db.get_user_by_email(recipient_email) if recipient_email else None
    recipient_name = (recipient_user.get('friendly_name') if recipient_user else None) or recipient_email or extension

    if not recipient_email:
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">That extension is not configured.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    # Check DND
    if ext_record.get('dnd_enabled'):
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">{xml_escape(recipient_name)} is currently unavailable.</Say>
    <Hangup/>
</Response>'''

        db.log_activity(
            action="internal_call_dnd",
            target=extension,
            details=f"From: {staff_email}, To: {recipient_email} has DND enabled",
            performed_by=f"session:{staff_email}" if staff_email else "twilio"
        )
        return Response(twiml, mimetype='application/xml')

    # Build dial targets for recipient
    ring_settings = db.get_user_ring_settings(recipient_email)
    routing = {
        'assignments': [recipient_email],
        'user_settings': {recipient_email: ring_settings},
    }
    dial_targets = _build_dial_targets(routing)

    if not dial_targets:
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">{xml_escape(recipient_name)} has no devices available.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    # Determine caller ID - must be a valid phone number for Twilio's <Dial>
    # (client: identities are not valid callerIds and cause instant Dial failure)
    caller_identity = f"client:{_email_to_browser_identity(staff_email)}" if staff_email else ''
    dial_caller_id = None
    if staff_email:
        caller_ext = db.get_staff_extension(staff_email)
        if caller_ext and caller_ext.get('default_caller_id'):
            dial_caller_id = caller_ext['default_caller_id']
    if not dial_caller_id:
        dial_caller_id = get_twilio_config('twilio_default_caller_id') or ''

    # Log the internal call
    conference_name = f"call_{call_sid}"

    db.log_call({
        'call_sid': call_sid,
        'direction': 'internal',
        'from_number': staff_email or caller_identity,
        'to_number': f"ext:{extension}",
        'status': 'ringing',
        'agent_email': staff_email,
    })

    # Store conference name immediately for hold/hangup
    db.set_call_conference(call_sid, conference_name)

    # Record caller as participant
    caller_user = db.get_user_by_email(staff_email) if staff_email else None
    caller_name = (caller_user.get('friendly_name') if caller_user else None) or staff_email
    db.add_participant(conference_name, call_sid, 'agent',
                       name=caller_name, email=staff_email)

    # Ring recipient's devices via REST API into the conference
    get_twilio_service().capture_for_thread()
    _ring_targets_into_conference(dial_targets, conference_name, dial_caller_id, call_sid,
                                 base_url=config.webhook_base_url, caller_identity=caller_identity, db=db)

    # Caller joins conference — hears ringback until recipient answers
    ringback_url = f"{config.webhook_base_url}/api/voice/ringback"
    dial_action_url = f"{config.webhook_base_url}/api/voice/extension-dial-status"
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial action="{xml_escape(dial_action_url)}">
        <Conference startConferenceOnEnter="false" endConferenceOnExit="true" beep="false" waitUrl="{xml_escape(ringback_url)}" waitMethod="POST">{xml_escape(conference_name)}</Conference>
    </Dial>
</Response>'''

    db.log_activity(
        action="internal_extension_call",
        target=extension,
        details=f"From: {staff_email}, To: {recipient_email} ({recipient_name}), Targets: {len(dial_targets)}, Conference: {conference_name}",
        performed_by=f"session:{staff_email}" if staff_email else "twilio"
    )

    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/extension-dial-status', methods=['POST'])
def extension_dial_status():
    """Handle the end of an internal extension call's <Dial>.

    Checks whether the call was actually connected by looking for a
    stored child_sid (set when an agent answers in ring-status callback).
    """
    call_sid = request.form.get('CallSid', '')
    db = get_db()

    # If we stored a child_sid, the call was answered at some point
    child_sid = db.get_call_child_sid(call_sid)
    if child_sid:
        # Call was connected and ended normally
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response/>', mimetype='application/xml')

    # Not answered — the conference ended without anyone joining
    twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">The call was not answered.</Say>
</Response>'''
    return Response(twiml, mimetype='application/xml')


@api_bp.route('/voice/outbound', methods=['POST'])
def voice_outbound():
    """Handle outbound calls from the browser softphone.

    Twilio calls this when a browser client initiates a call.
    Returns TwiML to dial the requested number, or to connect to a queue caller.

    No auth required - Twilio calls this directly.
    """
    # Get the number to call (passed from browser or SIP device)
    to_number = request.form.get('To')
    from_identity = request.form.get('From')  # The browser client's identity
    caller_id = request.form.get('CallerId')  # Which Twilio number to show
    call_sid = request.form.get('CallSid')  # Twilio provides the call SID

    # Debug: log raw values to diagnose SIP number formatting issues
    logger.info(f"Outbound call - Raw To: {to_number}, From: {from_identity}, CallerId: {caller_id}")

    # Parse user email from client identity (format: client:user_at_domain_com)
    staff_email = None
    if from_identity and from_identity.startswith('client:'):
        identity = from_identity[7:]  # Remove 'client:' prefix
        # Convert back: user_at_domain_com -> user@domain.com
        staff_email = identity.replace('_at_', '@').replace('_', '.')

    # Check if this is answering a queue call
    answer_queue_id = request.form.get('AnswerQueueId')
    answer_call_sid = request.form.get('AnswerCallSid')

    db = get_db()

    # Handle queue answer - connect browser and caller via conference
    if answer_queue_id and answer_call_sid:
        # Use a unique conference name for this call
        conference_name = f"hold_room_{answer_call_sid}"

        # Get the queued call to verify it exists
        queued_call = db.get_queued_call_by_sid(answer_call_sid)
        if not queued_call:
            twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, that caller is no longer in the queue.</Say>
    <Hangup/>
</Response>'''
            return Response(twiml, mimetype='application/xml')

        # Redirect the caller from the queue to the conference
        # This pulls them out of the Twilio queue and into our conference
        twilio_service = get_twilio_service()
        conference_url = f"{config.webhook_base_url}/api/voice/conference/join?room={conference_name}&role=caller"

        try:
            logger.info(f"Queue answer: redirecting caller {answer_call_sid} to {conference_name}, agent={staff_email}")
            twilio_service.client.calls(answer_call_sid).update(
                url=conference_url,
                method='POST'
            )
            logger.info(f"Queue answer: redirect succeeded for {answer_call_sid}")
        except Exception as e:
            logger.error(f"Queue answer: redirect FAILED for {answer_call_sid}: {e}")
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, we could not connect to the caller. They may have hung up.</Say>
    <Hangup/>
</Response>'''
            return Response(twiml, mimetype='application/xml')

        # Mark the call as being answered and store the conference name
        # Use staff_email (properly converted) not from_identity (raw Twilio format)
        db.update_queued_call_status(answer_call_sid, 'answered', answered_by=staff_email)

        # Update call_log with agent who answered
        db.update_call_log(answer_call_sid, {
            'status': 'answered',
            'agent_email': staff_email,
            'answered_at': 'CURRENT_TIMESTAMP',
        })

        # Store conference name for hold/unhold functionality
        db.set_call_conference(answer_call_sid, conference_name)

        # Record participants
        agent_user = db.get_user_by_email(staff_email) if staff_email else None
        agent_name = (agent_user.get('friendly_name') if agent_user else None) or staff_email
        db.add_participant(conference_name, call_sid, 'agent',
                           name=agent_name, email=staff_email)
        customer_number = queued_call.get('caller_number') or queued_call.get('from_number')
        customer_name = queued_call.get('customer_name') or customer_number
        db.add_participant(conference_name, answer_call_sid, 'customer',
                           name=customer_name, phone_number=customer_number)

        db.log_activity(
            action="browser_answering_queue",
            target=conference_name,
            details=f"Agent {from_identity} answering call {answer_call_sid} via conference",
            performed_by=from_identity or "browser"
        )

        # Return TwiML for agent to join the same conference.
        # Brief message gives the caller redirect time to complete —
        # without it the agent joins the conference before the caller
        # arrives and hears hold music until the redirect finishes.
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting.</Say>
    <Dial>
        <Conference endConferenceOnExit="true" startConferenceOnEnter="true">{xml_escape(conference_name)}</Conference>
    </Dial>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    if not to_number:
        twiml = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>No number specified.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    # Check if this is a 4-digit extension (internal call)
    stripped = to_number.strip()
    if stripped.isdigit() and len(stripped) == 4:
        return _handle_internal_extension_call(stripped, from_identity, staff_email, call_sid, db)

    # Format the number
    service = get_twilio_service()
    to_e164 = service._format_phone_number(to_number)

    # Determine caller ID - use provided, or look up user's default, or fall back
    if not caller_id:
        # Look up user's default caller ID from staff extension
        lookup_email = staff_email
        if not lookup_email and from_identity and from_identity.startswith('sip:'):
            # Parse SIP identity to email: sip:chris_savage@watsonblinds.sip.twilio.com
            sip_part = from_identity[4:]
            if '@' in sip_part:
                sip_username = sip_part.split('@')[0]
                sip_user = db.get_user_by_username(sip_username)
                if sip_user:
                    lookup_email = sip_user.get('staff_email')
                    # Record SIP device activity for presence tracking
                    if lookup_email:
                        db.stamp_sip_activity(lookup_email)

        if lookup_email:
            staff_ext = db.get_staff_extension(lookup_email)
            if staff_ext and staff_ext.get('default_caller_id'):
                caller_id = staff_ext['default_caller_id']
                logger.info(f"Using user's default caller ID: {caller_id}")

        if not caller_id:
            # Fall back to first available number
            numbers = db.get_phone_numbers()
            if numbers:
                caller_id = numbers[0]['phone_number']
            logger.info(f"No default caller ID for user, using fallback: {caller_id}")

    # Log the outbound call
    conference_name = f"call_{call_sid}"

    db.log_call({
        'call_sid': call_sid,
        'direction': 'outbound',
        'from_number': caller_id or '',
        'to_number': to_e164,
        'status': 'ringing',
        'agent_email': staff_email,
    })

    # Store conference name immediately so hold/hangup can find it
    db.set_call_conference(call_sid, conference_name)

    # Dial the customer into the conference via REST API
    customer_join_url = f"{config.webhook_base_url}/api/voice/outbound/customer-join?conference={conference_name}"
    customer_status_url = f"{config.webhook_base_url}/api/voice/outbound/customer-status?agent_call_sid={call_sid}"

    try:
        customer_call = service.client.calls.create(
            to=to_e164,
            from_=caller_id or get_twilio_config('twilio_default_caller_id') or '',
            url=customer_join_url,
            status_callback=customer_status_url,
            status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
            timeout=30,
        )
        # Create a proper call_log record for the customer leg
        db.log_call({
            'call_sid': customer_call.sid,
            'direction': 'outbound',
            'from_number': caller_id or '',
            'to_number': to_e164,
            'status': 'ringing',
            'conference_name': conference_name,
        })
        db.set_call_child_sid(call_sid, customer_call.sid)
        # Record participants — agent now, customer when they answer
        agent_user = db.get_user_by_email(staff_email) if staff_email else None
        agent_name = (agent_user.get('friendly_name') if agent_user else None) or staff_email
        db.add_participant(conference_name, call_sid, 'agent',
                           name=agent_name, email=staff_email)
        logger.info(f"Outbound: dialed {to_e164} as {customer_call.sid}, conference={conference_name}")
    except Exception as e:
        logger.error(f"Failed to dial customer {to_e164}: {e}")
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">Sorry, we could not place that call.</Say>
    <Hangup/>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    # Agent joins conference — hears ringback until customer answers
    ringback_url = f"{config.webhook_base_url}/api/voice/ringback"
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference startConferenceOnEnter="false" endConferenceOnExit="true" beep="false" waitUrl="{xml_escape(ringback_url)}" waitMethod="POST">{xml_escape(conference_name)}</Conference>
    </Dial>
</Response>'''

    db.log_activity(
        action="outbound_call",
        target=to_e164,
        details=f"CallSid: {call_sid}, Customer: {customer_call.sid}, Conference: {conference_name}, Caller ID: {caller_id}",
        performed_by=f"session:{staff_email}" if staff_email else "twilio"
    )

    return Response(twiml, mimetype='application/xml')


# =============================================================================
# Users / SIP Credentials
# =============================================================================

@api_bp.route('/sip-domains')
@api_or_session_auth
def list_sip_domains():
    """List all SIP domains."""
    service = get_twilio_service()
    domains = service.get_sip_domains()
    return jsonify({"sip_domains": domains})


@api_bp.route('/credential-lists')
@api_or_session_auth
def list_credential_lists():
    """List all credential lists."""
    service = get_twilio_service()
    cred_lists = service.get_credential_lists()
    return jsonify({"credential_lists": cred_lists})


@api_bp.route('/users', methods=['POST'])
@api_or_session_auth
def create_user():
    """Create a new SIP user (for onboarding).

    Body:
        credential_list_sid: The credential list to add the user to
        username: SIP username
        password: SIP password
        friendly_name: Optional display name
        staff_email: Optional email to link to staff record
    """
    data = request.get_json()

    required = ['credential_list_sid', 'username', 'password']
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"{field} is required"}), 400

    service = get_twilio_service()
    result = service.create_user_credential(
        credential_list_sid=data['credential_list_sid'],
        username=data['username'],
        password=data['password'],
        friendly_name=data.get('friendly_name')
    )

    if result.get("success"):
        return jsonify(result), 201
    else:
        return jsonify(result), 400


@api_bp.route('/users/<credential_list_sid>/<credential_sid>', methods=['DELETE'])
@api_or_session_auth
def delete_user(credential_list_sid, credential_sid):
    """Delete a SIP user (for offboarding)."""
    service = get_twilio_service()
    result = service.delete_user_credential(credential_list_sid, credential_sid)

    if result.get("success"):
        return jsonify(result)
    else:
        return jsonify(result), 400




# =============================================================================
# Test Call
# =============================================================================

@api_bp.route('/test-call', methods=['POST'])
@api_or_session_auth
def make_test_call():
    """Make a test call to verify the phone system is working.

    Body:
        from_number: Twilio number to call from (E.164 format)
        to_number: Number to call (will be formatted to E.164)
    """
    data = request.get_json()

    if not data.get('from_number') or not data.get('to_number'):
        return jsonify({"error": "from_number and to_number are required"}), 400

    service = get_twilio_service()
    result = service.make_test_call(
        from_number=data['from_number'],
        to_number=data['to_number']
    )

    if result.get("success"):
        return jsonify(result)
    else:
        return jsonify(result), 400


# =============================================================================
# Do Not Disturb
# =============================================================================

@api_bp.route('/dnd', methods=['POST'])
@api_or_session_auth
def toggle_dnd():
    """Toggle do-not-disturb for the current user.

    POST /api/dnd
    Body: {"enabled": true/false}

    When DND is on, all incoming calls skip this user and go to voicemail
    or the next fallback action. The user is also excluded from queue rings.

    Returns:
        {"success": true, "dnd_enabled": true/false}
    """
    email = get_api_caller_email()

    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if enabled is None:
        return jsonify({'error': 'enabled field required'}), 400

    db = get_db()

    # Ensure the user has a staff extension
    ext = db.get_staff_extension(email)
    if not ext:
        return jsonify({'error': 'No staff extension found'}), 404

    performed_by = get_api_caller()
    db.set_dnd(email, bool(enabled), performed_by)
    db.log_activity(
        action="dnd_toggled",
        target=email,
        details=f"DND {'enabled' if enabled else 'disabled'}",
        performed_by=performed_by
    )

    return jsonify({'success': True, 'dnd_enabled': bool(enabled)})


@api_bp.route('/dnd')
@api_or_session_auth
def get_dnd():
    """Get DND status for the current user.

    GET /api/dnd

    Returns:
        {"dnd_enabled": true/false}
    """
    email = get_api_caller_email()

    db = get_db()
    ext = db.get_staff_extension(email)
    if not ext:
        return jsonify({'dnd_enabled': False})

    return jsonify({'dnd_enabled': bool(ext.get('dnd_enabled'))})


# =============================================================================
# Presence / Heartbeat
# =============================================================================

@api_bp.route('/heartbeat', methods=['POST'])
@login_required
def heartbeat():
    """Update the current user's online presence heartbeat.

    POST /api/heartbeat

    Called periodically by the softphone to indicate the user is online.

    Returns:
        {"success": true}
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    db = get_db()
    ext = db.get_staff_extension(user.email)
    if ext:
        db.update_heartbeat(user.email)

    return jsonify({"success": True})


@api_bp.route('/presence', methods=['GET'])
@login_required
def get_presence():
    """Get online/DND status for all staff with extensions.

    GET /api/presence

    Returns a lightweight status map for polling.

    Returns:
        {
            "presence": {
                "user@example.com": {"online": true, "dnd": false},
                ...
            }
        }
    """
    db = get_db()
    extensions = db.get_all_staff_extensions()
    now = datetime.now(timezone.utc)

    # Get active calls to show "on call" status
    active_calls = _get_active_calls_from_twilio()
    on_call_emails = {c['agent_email'].lower() for c in active_calls if c.get('agent_email')}

    presence = {}
    for ext in extensions:
        email = ext.get('email', '')
        last_hb = ext.get('last_heartbeat')
        is_online = False
        if last_hb:
            try:
                hb_time = datetime.fromisoformat(last_hb)
                is_online = (now - hb_time).total_seconds() < 60
            except (ValueError, TypeError):
                pass

        # SIP device recently used (stamped when we ring or see a SIP call)
        sip_active = False
        sip_ts = ext.get('sip_registered_at')
        if sip_ts:
            try:
                sip_time = datetime.fromisoformat(sip_ts)
                sip_active = (now - sip_time).total_seconds() < 86400  # 24 hours
            except (ValueError, TypeError):
                pass

        presence[email] = {
            'online': is_online,
            'dnd': bool(ext.get('dnd_enabled')),
            'on_call': email.lower() in on_call_emails,
            'has_sip_device': sip_active,
        }

    return jsonify({"presence": presence})

# PAM, Staff, and Contacts routes extracted to staff_routes.py
# Queue Dashboard stays here (depends on internal helpers)


# Queue Dashboard API - For agent queue UI
# =============================================================================

@api_bp.route('/queue/callers', methods=['GET'])
@api_or_session_auth
def get_queued_callers():
    """
    Get callers waiting in queue(s) with enriched data.

    Query params:
        queue_id: Filter to specific queue (optional)

    Returns:
        {
            "callers": [...],
            "count": N,
            "stats": {"waiting": N, "avg_wait_seconds": N}
        }
    """
    db = get_db()
    queue_id = request.args.get('queue_id', type=int)

    # For session-authenticated users, only show callers from their queues
    # unless a specific queue_id is requested
    user = get_current_user()
    if user and not queue_id:
        memberships = db.get_user_queue_memberships(user.email)
        user_queue_ids = [m['queue_id'] for m in memberships]
        if user_queue_ids:
            all_callers = []
            all_stats = {'waiting': 0, 'avg_wait_seconds': 0}
            for qid in user_queue_ids:
                all_callers.extend(db.get_queued_calls(queue_id=qid, status='waiting'))
                q_stats = db.get_queue_stats(queue_id=qid)
                all_stats['waiting'] += q_stats.get('waiting', 0)
            callers = all_callers
            stats = all_stats
        else:
            callers = []
            stats = {'waiting': 0, 'avg_wait_seconds': 0}
    else:
        callers = db.get_queued_calls(queue_id=queue_id, status='waiting')
        stats = db.get_queue_stats(queue_id=queue_id)

    # Verify callers against Twilio queues — the DB may have stale 'waiting'
    # records from missed queue_leave webhooks. Fetch actual Twilio queue
    # members (one API call per queue) and remove any DB records not in Twilio.
    now = datetime.now(timezone.utc)

    if callers:
        # Collect unique queue IDs we need to check
        queue_ids_to_check = {c.get('queue_id') for c in callers if c.get('queue_id')}
        twilio_waiting_sids = set()

        twilio_service = get_twilio_service()
        for qid in queue_ids_to_check:
            twilio_queue_name = f"queue_{qid}"
            try:
                twilio_queue = twilio_service.get_queue_by_name(twilio_queue_name)
                if twilio_queue:
                    members = twilio_list(twilio_queue.members)
                    for member in members:
                        twilio_waiting_sids.add(member.call_sid)
            except Exception as e:
                logger.warning(f"Could not check Twilio queue {twilio_queue_name}: {e}")
                # On error, keep all callers for this queue (don't clean what we can't verify)
                for c in callers:
                    if c.get('queue_id') == qid:
                        twilio_waiting_sids.add(c.get('call_sid'))

        # Filter to only callers actually in Twilio, auto-clean the rest
        live_callers = []
        for caller in callers:
            call_sid = caller.get('call_sid')
            if call_sid in twilio_waiting_sids:
                live_callers.append(caller)
            else:
                logger.info(f"Auto-cleaning stale queued call {call_sid} (not in Twilio queue)")
                db.update_queued_call_status(call_sid, 'abandoned')
        callers = live_callers

    # Calculate wait time for each caller
    for caller in callers:
        if caller.get('enqueued_at'):
            try:
                enqueued = datetime.fromisoformat(caller['enqueued_at'])
                if enqueued.tzinfo is None:
                    enqueued = enqueued.replace(tzinfo=timezone.utc)
                wait_seconds = int((now - enqueued).total_seconds())
                caller['wait_seconds'] = wait_seconds
                caller['wait_display'] = f"{wait_seconds // 60}:{wait_seconds % 60:02d}"
            except (ValueError, TypeError):
                caller['wait_seconds'] = 0
                caller['wait_display'] = "0:00"

        # Parse order_data JSON if present
        if caller.get('order_data'):
            try:
                caller['order_info'] = json.loads(caller['order_data'])
            except json.JSONDecodeError:
                caller['order_info'] = None

    return jsonify({
        "callers": callers,
        "count": len(callers),
        "stats": stats
    })


# =============================================================================
# Callback Queue Management
# =============================================================================

@api_bp.route('/queue/callbacks', methods=['GET'])
@api_or_session_auth
def get_pending_callbacks():
    """Get pending callback requests.

    Query params:
        queue_id: Filter to specific queue (optional)

    Returns:
        {"callbacks": [...], "count": N}
    """
    db = get_db()
    queue_id = request.args.get('queue_id', type=int)
    callbacks = db.get_pending_callbacks(queue_id=queue_id)

    # Calculate wait time since request
    now = datetime.now(timezone.utc)
    for cb in callbacks:
        if cb.get('requested_at'):
            try:
                requested = datetime.fromisoformat(cb['requested_at'])
                wait_seconds = int((now - requested).total_seconds())
                cb['wait_seconds'] = wait_seconds
                cb['wait_display'] = f"{wait_seconds // 60}:{wait_seconds % 60:02d}"
            except (ValueError, TypeError):
                cb['wait_seconds'] = 0
                cb['wait_display'] = "0:00"

    return jsonify({
        "callbacks": callbacks,
        "count": len(callbacks)
    })


@api_bp.route('/queue/callbacks/<int:callback_id>/claim', methods=['POST'])
@api_or_session_auth
def claim_callback_request(callback_id):
    """Claim a callback request so the agent can call the customer back.

    Returns:
        {"success": true, "callback": {...}} or {"error": "..."}
    """
    db = get_db()
    caller = get_api_caller()

    success = db.claim_callback(callback_id, caller)
    if not success:
        return jsonify({"error": "Callback already claimed or not found"}), 409

    db.log_activity(
        action="callback_claimed",
        target=str(callback_id),
        details=f"Agent claimed callback {callback_id}",
        performed_by=caller
    )

    return jsonify({"success": True})


@api_bp.route('/queue/callbacks/<int:callback_id>/complete', methods=['POST'])
@api_or_session_auth
def complete_callback_request(callback_id):
    """Mark a callback as completed after the agent called the customer.

    Body (optional):
        call_sid: The Twilio call SID of the callback call
    """
    db = get_db()
    caller = get_api_caller()
    data = request.get_json(silent=True) or {}

    db.complete_callback(callback_id, call_sid=data.get('call_sid'))

    db.log_activity(
        action="callback_completed",
        target=str(callback_id),
        details=f"Agent completed callback {callback_id}",
        performed_by=caller
    )

    return jsonify({"success": True})


@api_bp.route('/queue/callbacks/<int:callback_id>/fail', methods=['POST'])
@api_or_session_auth
def fail_callback_request(callback_id):
    """Mark a callback as failed (customer didn't answer, etc).

    Body (optional):
        notes: Reason for failure
    """
    db = get_db()
    caller = get_api_caller()
    data = request.get_json(silent=True) or {}

    db.fail_callback(callback_id, notes=data.get('notes'))

    db.log_activity(
        action="callback_failed",
        target=str(callback_id),
        details=f"Agent marked callback {callback_id} as failed: {data.get('notes', '')}",
        performed_by=caller
    )

    return jsonify({"success": True})


@api_bp.route('/queue/callers/<call_sid>/answer', methods=['POST'])
@api_or_session_auth
def answer_queued_caller(call_sid):
    """
    Initiate a call to the agent to connect them to a queued caller.

    The agent's phone will ring, and when they answer, they'll be
    connected to the waiting caller.

    Request body:
        agent_number: Phone number to call the agent on (optional, uses user's device)

    Returns:
        {"success": true, "call_sid": "..."}
    """
    db = get_db()

    # Get the queued call
    queued_call = db.get_queued_call_by_sid(call_sid)
    if not queued_call:
        return jsonify({"error": "Queued call not found"}), 404

    if queued_call.get('status') != 'waiting':
        return jsonify({"error": "Call is no longer waiting"}), 400

    # Get agent info
    current_user = get_current_user()
    agent_email = current_user.email if current_user else get_api_caller_email()

    # Get target to dial - either explicit number or agent's SIP URI
    data = request.get_json() or {}
    agent_number = data.get('agent_number')

    if not agent_number:
        # Use agent's SIP URI (for desk phone/Zoiper)
        ring_settings = db.get_user_ring_settings(agent_email)
        if ring_settings.get('ring_sip', True):
            sip_domain = _get_sip_domain()
            if sip_domain:
                agent_number = _get_sip_uri_for_user(agent_email, sip_domain)

    if not agent_number:
        return jsonify({"error": "No SIP credentials found for agent"}), 400

    # Get caller ID for the outbound call
    caller_id = get_twilio_config('twilio_default_caller_id')
    if not caller_id:
        phones = db.get_all_phones()
        if phones:
            caller_id = phones[0].get('phone_number')

    if not caller_id:
        return jsonify({"error": "No caller ID configured"}), 500

    # Initiate call to agent
    queue_id = queued_call.get('queue_id')
    connect_url = f"{config.webhook_base_url}/api/voice/queue/{queue_id}/connect-agent?call_sid={call_sid}"

    twilio_service = get_twilio_service()
    result = twilio_service.initiate_call(
        to=agent_number,
        from_number=caller_id,
        url=connect_url
    )

    if result.get('success'):
        # Mark the queued call as being answered
        db.update_queued_call_status(call_sid, 'answered', answered_by=agent_email)

        db.log_activity(
            action="agent_answering_queue",
            target=agent_number,
            details=f"Agent {agent_email} answering call from {queued_call.get('caller_number')}",
            performed_by=agent_email
        )

        return jsonify({
            "success": True,
            "call_sid": result.get('call_sid'),
            "message": "Calling your phone to connect you to the caller"
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get('error', 'Failed to initiate call')
        }), 500


@api_bp.route('/queue/callers/<call_sid>/status', methods=['POST'])
@api_or_session_auth
def update_queued_caller_status(call_sid):
    """
    Update the status of a queued call (e.g., mark as abandoned).

    Request body:
        status: New status (abandoned, timeout)

    Returns:
        {"success": true}
    """
    db = get_db()
    data = request.get_json() or {}
    status = data.get('status')

    if status not in ('abandoned', 'timeout'):
        return jsonify({"error": "Invalid status"}), 400

    db.update_queued_call_status(call_sid, status)

    return jsonify({"success": True})

# =============================================================================
# Active Calls (Twilio-backed)
# =============================================================================

# In-memory cache for Twilio active calls (avoids hammering the API)
# Keyed by tenant ID to prevent cross-tenant data leakage
_active_calls_cache = {}  # {tenant_id: {'calls': [], 'fetched_at': 0}}
_ACTIVE_CALLS_TTL = 5  # seconds


def _get_active_calls_from_twilio() -> list[dict]:
    """Get active calls from Twilio, enriched with call_log details.

    Uses a 5-second cache to avoid redundant Twilio API calls when
    multiple phone clients are polling simultaneously.
    """
    from flask import g
    tenant_id = getattr(g, 'tenant', {}).get('id', '_none') if hasattr(g, 'tenant') and g.tenant else '_none'
    now = time.time()

    # Return cached result if fresh (per-tenant)
    with _call_tracking_lock:
        tenant_cache = _active_calls_cache.get(tenant_id, {'calls': [], 'fetched_at': 0})
    if now - tenant_cache['fetched_at'] < _ACTIVE_CALLS_TTL:
        return tenant_cache['calls']

    twilio = get_twilio_service()
    db = get_db()

    # Get ground truth from Twilio
    twilio_calls = twilio.list_in_progress_calls()
    active_sids = {c['call_sid'] for c in twilio_calls}

    # Clean up stale call_log entries: if call_log says active but Twilio
    # doesn't have it, the call has ended
    db.close_stale_calls(active_sids)

    # Enrich with call_log data (agent, customer, queue, etc.)
    result = []
    if active_sids:
        call_log_map = db.get_call_log_by_sids(active_sids)

        for tc in twilio_calls:
            sid = tc['call_sid']
            log = call_log_map.get(sid, {})

            # Skip calls with no agent (e.g. IVR, queue hold music)
            agent = log.get('agent_email')
            if not agent:
                continue

            result.append({
                'call_sid': sid,
                'direction': log.get('direction', tc.get('direction', '')),
                'from_number': log.get('from_number') or tc.get('from_number', ''),
                'to_number': log.get('to_number') or tc.get('to_number', ''),
                'agent_email': agent,
                'status': 'answered',
                'started_at': log.get('started_at') or tc.get('start_time'),
                'answered_at': log.get('answered_at'),
                'customer_name': log.get('customer_name'),
                'queue_name': log.get('queue_name'),
            })

    with _call_tracking_lock:
        _active_calls_cache[tenant_id] = {'calls': result, 'fetched_at': now}
    return result


@api_bp.route('/active-calls', methods=['GET'])
@api_or_session_auth
def get_active_calls():
    """Get all currently active calls from Twilio.

    GET /api/active-calls

    Uses Twilio's call list as the source of truth, enriched with
    call_log data for agent/customer details. Cached for 5 seconds.
    """
    calls = _get_active_calls_from_twilio()
    return jsonify({'calls': calls})


@api_bp.route('/my-call-state', methods=['GET'])
@api_or_session_auth
def get_my_call_state():
    """Get complete call state for the current agent.

    Query params:
        call_sid: The agent's current Twilio call SID

    Returns the full picture: conference, participants, transfer state.
    The frontend polls this every 2-3 seconds during an active call.
    """
    agent_call_sid = request.args.get('call_sid')
    if not agent_call_sid:
        return jsonify({"in_call": False})

    # We know who the current user is from the session
    current_user = get_current_user()
    caller_email = current_user.email if current_user else get_api_caller_email()

    try:
        return _get_call_state_inner(agent_call_sid, caller_email)
    except Exception as e:
        logger.exception(f"Error getting call state for {agent_call_sid}")
        return jsonify({"in_call": False, "error": "Failed to get call state"}), 500


def _get_call_state_inner(agent_call_sid, caller_email=None):
    from rinq.api.call_state import get_call_state
    return jsonify(get_call_state(agent_call_sid, caller_email))


@api_bp.route('/conference/participants', methods=['GET'])
@api_or_session_auth
def get_conference_participants():
    """Get live participant list for a conference.

    Query params:
        conference: Conference friendly name

    Returns:
        {"participants": [{"call_sid": "...", "hold": false, "role": "caller|agent", "name": "..."}]}
    """
    conference_name = request.args.get('conference')
    if not conference_name:
        return jsonify({"participants": []})

    db = get_db()
    participants = db.get_participants(conference_name)
    if not participants:
        return jsonify({"participants": [], "conference_ended": True})

    result = []
    for p in participants:
        result.append({
            'call_sid': p['call_sid'],
            'name': p['name'] or 'Unknown',
            'role': p['role'],
            'hold': False,
            'muted': False,
        })
    return jsonify({"participants": result})


# Call History
# =============================================================================

@api_bp.route('/my-call-history', methods=['GET'])
@api_or_session_auth
def get_my_call_history():
    """Get the current user's recent call history.

    Query params:
        limit: Max calls to return (default 50, max 200)

    Returns:
        {"calls": [{call_sid, direction, status, from_number, to_number, ...}]}
    """
    db = get_db()
    current_user = get_current_user()
    agent_email = current_user.email if current_user else get_api_caller_email()
    if not agent_email:
        return jsonify({"error": "Could not determine user"}), 401

    limit = min(int(request.args.get('limit', 50)), 200)
    calls = db.get_my_call_history(agent_email, limit=limit)
    return jsonify({"calls": calls})


# =============================================================================
# Call Transfer (extracted to transfer_routes.py)
# =============================================================================
from rinq.api.transfer_routes import register as _register_transfer_routes
_register_transfer_routes(api_bp)

# =============================================================================
# Call Recording (extracted to recording_routes.py)
# =============================================================================
from rinq.api.recording_routes import register as _register_recording_routes
_register_recording_routes(api_bp)

# =============================================================================
# Stats, Reporting, Cleanup & Number Provisioning (extracted to stats_routes.py)
# =============================================================================
from rinq.api.stats_routes import register as _register_stats_routes
_register_stats_routes(api_bp)

# =============================================================================
# Staff, PAM & Contacts (extracted to staff_routes.py)
# =============================================================================
from rinq.api.staff_routes import register as _register_staff_routes
_register_staff_routes(api_bp)
