"""Transfer API routes — blind, warm, 3-way, and transfer webhooks.

Extracted from routes.py. Registered via register(api_bp) at import time.
"""

import json
import logging
from xml.sax.saxutils import escape as xml_escape

from flask import jsonify, request, Response

from rinq.api.identity import email_to_browser_identity as _email_to_browser_identity
from rinq.config import config
from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service, twilio_list
from rinq.tenant.context import get_twilio_config

try:
    from shared.auth.bot_api import api_or_session_auth, get_api_caller_email
except ImportError:
    from rinq.auth.decorators import api_or_session_auth, get_api_caller_email

logger = logging.getLogger(__name__)


def _resolve_transfer_target_email(call_sid: str) -> str | None:
    """Try to resolve a transfer target's email from their call SID."""
    try:
        service = get_twilio_service()
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
        logger.debug(f"Could not resolve transfer target email from call {call_sid}: {e}")
    return None


def register(bp):
    """Register all transfer routes on the given blueprint."""

    @bp.route('/voice/transfer/targets', methods=['GET'])
    @api_or_session_auth
    def get_transfer_targets():
        """Get list of available transfer targets (team members)."""
        from rinq.services.transfer_service import get_transfer_service
        transfer_service = get_transfer_service()
        transfer_service._capture_base_url()
        targets = transfer_service.get_transfer_targets()
        return jsonify({"targets": targets})

    @bp.route('/voice/transfer/blind', methods=['POST'])
    @api_or_session_auth
    def blind_transfer():
        """Execute a blind (cold) transfer."""
        from rinq.services.transfer_service import get_transfer_service

        data = request.get_json() or {}
        call_sid = data.get('call_sid')
        target = data.get('target')
        target_name = data.get('target_name', 'Unknown')

        if not call_sid or not target:
            return jsonify({"error": "call_sid and target required"}), 400

        transferred_by = get_api_caller_email()
        transfer_service = get_transfer_service()
        transfer_service._capture_base_url()

        result = transfer_service.blind_transfer(call_sid, target, target_name, transferred_by)

        # Agent is done with the call after a blind transfer — mark them as left
        if result.get('success'):
            db = get_db()
            agent_call_sid = data.get('agent_call_sid')
            if agent_call_sid:
                db.remove_participant(agent_call_sid)

        return jsonify(result) if result.get('success') else (jsonify(result), 400)

    @bp.route('/voice/transfer/blind-direct', methods=['POST'])
    @api_or_session_auth
    def blind_transfer_direct():
        """Execute a blind transfer on a direct (non-conference) call."""
        from rinq.services.transfer_service import get_transfer_service

        data = request.get_json() or {}
        call_sid = data.get('call_sid')
        target = data.get('target')
        target_name = data.get('target_name', 'Unknown')
        caller_id = data.get('caller_id')

        if not call_sid or not target:
            return jsonify({"error": "call_sid and target required"}), 400

        transferred_by = get_api_caller_email()
        transfer_service = get_transfer_service()
        transfer_service._capture_base_url()

        db = get_db()
        conference_name = db.get_call_conference(call_sid)
        if conference_name:
            child_sid = db.get_call_child_sid(call_sid)
            if child_sid:
                result = transfer_service.blind_transfer(
                    child_sid, target, target_name, transferred_by,
                    conference_name_override=conference_name
                )
            else:
                result = {'success': False, 'error': 'Could not identify customer call'}
        else:
            result = transfer_service.blind_transfer_direct(
                call_sid, target, target_name, transferred_by, caller_id
            )

        # Agent is done with the call after a blind transfer — mark them as left
        if result.get('success'):
            db.remove_participant(call_sid)

        return jsonify(result) if result.get('success') else (jsonify(result), 400)

    @bp.route('/voice/transfer/warm/start', methods=['POST'])
    @api_or_session_auth
    def warm_transfer_start():
        """Start a warm (attended) transfer or 3-way call."""
        from rinq.services.transfer_service import get_transfer_service

        data = request.get_json() or {}
        call_sid = data.get('call_sid')
        target = data.get('target')
        target_name = data.get('target_name', 'Unknown')
        agent_call_sid = data.get('agent_call_sid')
        three_way = data.get('three_way', False)

        transferred_by = get_api_caller_email()
        transfer_service = get_transfer_service()
        transfer_service._capture_base_url()

        db = get_db()
        conf_name = db.get_call_conference(agent_call_sid) if agent_call_sid else None
        child_sid = db.get_call_child_sid(agent_call_sid) if agent_call_sid else None
        if not conf_name and call_sid:
            conf_name = db.get_call_conference(call_sid)
        customer_sid = child_sid or call_sid

        if not conf_name or not customer_sid:
            return jsonify({"error": "Could not locate active conference for this call. "
                            "Ensure the call is connected before transferring."}), 400

        if not target or not agent_call_sid:
            return jsonify({"error": "agent_call_sid and target required"}), 400

        result = transfer_service.warm_transfer_start(
            customer_sid, target, target_name, transferred_by, agent_call_sid,
            conference_name_override=conf_name,
            three_way=three_way
        )
        if result.get('success'):
            result['transfer_key'] = customer_sid

        return jsonify(result) if result.get('success') else (jsonify(result), 400)

    @bp.route('/voice/transfer/warm/complete', methods=['POST'])
    @api_or_session_auth
    def warm_transfer_complete():
        """Complete a warm transfer or 3-way call."""
        from rinq.services.transfer_service import get_transfer_service

        data = request.get_json() or {}
        transferred_by = get_api_caller_email()
        transfer_service = get_transfer_service()
        transfer_service._capture_base_url()

        call_sid = data.get('call_sid') or data.get('transfer_key')
        if not call_sid:
            return jsonify({"error": "call_sid or transfer_key required"}), 400

        agent_call_sid = data.get('agent_call_sid')
        result = transfer_service.warm_transfer_complete(call_sid, transferred_by, agent_call_sid=agent_call_sid)

        return jsonify(result) if result.get('success') else (jsonify(result), 400)

    @bp.route('/voice/transfer/cancel', methods=['POST'])
    @api_or_session_auth
    def transfer_cancel():
        """Cancel a pending or in-progress transfer."""
        from rinq.services.transfer_service import get_transfer_service

        data = request.get_json() or {}
        cancelled_by = get_api_caller_email()
        transfer_service = get_transfer_service()
        transfer_service._capture_base_url()

        call_sid = data.get('call_sid') or data.get('transfer_key')
        if not call_sid:
            return jsonify({"error": "call_sid or transfer_key required"}), 400

        result = transfer_service.warm_transfer_cancel(call_sid, cancelled_by)

        return jsonify(result) if result.get('success') else (jsonify(result), 400)

    @bp.route('/voice/transfer/status', methods=['GET'])
    @api_or_session_auth
    def get_transfer_status():
        """Get the current transfer status for a call."""
        db = get_db()
        source = request.args.get('source', 'queued_calls')
        call_sid = request.args.get('call_sid') or request.args.get('transfer_key')

        if not call_sid:
            return jsonify({"error": "call_sid or transfer_key required"}), 400

        if source == 'call_log':
            transfer_state = db.get_transfer_state_log(call_sid)
        else:
            transfer_state = db.get_transfer_state(call_sid)

        return jsonify({"transfer": transfer_state})

    # =========================================================================
    # Transfer TwiML Webhooks (called by Twilio during transfer)
    # =========================================================================

    @bp.route('/voice/transfer/consult-join', methods=['POST'])
    def transfer_consult_join():
        """TwiML when transfer target answers consultation call."""
        conference = request.args.get('conference')
        if not conference:
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response><Say>Sorry, an error occurred.</Say><Hangup/></Response>', mimetype='application/xml')

        # Record transfer target as participant
        target_call_sid = request.form.get('CallSid', '')
        if target_call_sid:
            db = get_db()
            target_email = _resolve_transfer_target_email(target_call_sid)
            target_user = db.get_user_by_email(target_email) if target_email else None
            target_name = (target_user.get('friendly_name') if target_user else None) or target_email
            db.add_participant(conference, target_call_sid, 'transfer_target',
                               name=target_name, email=target_email)

        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting you with the caller's agent.</Say>
    <Dial>
        <Conference beep="false" startConferenceOnEnter="true" endConferenceOnExit="false">
            {xml_escape(conference)}
        </Conference>
    </Dial>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    @bp.route('/voice/transfer/agent-consult', methods=['POST'])
    def transfer_agent_consult():
        """TwiML to move agent to consultation conference.

        Agent joins with startConferenceOnEnter=false and hears ringback
        until the transfer target answers and starts the conference.
        """
        conference = request.args.get('conference')
        if not conference:
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response><Say>Sorry, an error occurred.</Say><Hangup/></Response>', mimetype='application/xml')

        # Record agent in the consult conference
        agent_call_sid = request.form.get('CallSid', '')
        if agent_call_sid:
            db = get_db()
            agent_participant = db.get_participant_by_sid(agent_call_sid)
            agent_name = agent_participant['name'] if agent_participant else None
            agent_email = agent_participant['email'] if agent_participant else None
            db.add_participant(conference, agent_call_sid, 'agent',
                               name=agent_name, email=agent_email)

        ringback_url = f"{config.webhook_base_url}/api/voice/ringback"
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference beep="false" startConferenceOnEnter="false" endConferenceOnExit="true" waitUrl="{xml_escape(ringback_url)}" waitMethod="POST">
            {xml_escape(conference)}
        </Conference>
    </Dial>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    @bp.route('/voice/transfer/target-join', methods=['POST'])
    def transfer_target_join():
        """TwiML to move transfer target to original conference."""
        conference = request.args.get('conference')
        if not conference:
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response><Say>Sorry, an error occurred.</Say><Hangup/></Response>', mimetype='application/xml')

        # Update participant: target now joins main conference as agent
        target_call_sid = request.form.get('CallSid', '')
        if target_call_sid:
            db = get_db()
            db.add_participant(conference, target_call_sid, 'agent')

        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference beep="false" startConferenceOnEnter="true" endConferenceOnExit="true">
            {xml_escape(conference)}
        </Conference>
    </Dial>
</Response>'''
        return Response(twiml, mimetype='application/xml')

    @bp.route('/voice/transfer/direct-dial-status', methods=['POST'])
    def transfer_direct_dial_status():
        """Handle the result of a blind-direct transfer's <Dial>."""
        dial_status = request.form.get('DialCallStatus', '')
        transferred_by = request.args.get('transferred_by', '')
        customer_call_sid = request.args.get('customer_call_sid', '')

        logger.info(f"Direct dial status: {dial_status}, transferred_by={transferred_by}, customer={customer_call_sid}")

        if dial_status in ('completed', 'answered'):
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response/>', mimetype='application/xml')

        if transferred_by:
            agent_identity = _email_to_browser_identity(transferred_by)
            caller_id = get_twilio_config('twilio_default_caller_id') or ''

            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">The transfer was not successful. Reconnecting you.</Say>
    <Dial callerId="{xml_escape(caller_id)}" timeout="15">
        <Client>{xml_escape(agent_identity)}</Client>
    </Dial>
    <Say voice="Polly.Nicole">Sorry, we were unable to reconnect your call. Goodbye.</Say>
</Response>'''
            return Response(twiml, mimetype='application/xml')

        return Response('''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">Sorry, we were unable to connect your call. Please try again later. Goodbye.</Say>
</Response>''', mimetype='application/xml')

    @bp.route('/voice/transfer/callback-status', methods=['POST'])
    def transfer_callback_status():
        """Status callback for agent callback after failed blind transfer."""
        call_status = request.form.get('CallStatus', '')
        conference = request.args.get('conference', '')
        customer_call = request.args.get('customer_call', '')
        callback_call_sid = request.form.get('CallSid', '')

        # Always clean up the participant entry — the callback call is now done
        if callback_call_sid:
            from rinq.api.routes import _handle_participant_left
            db = get_db()
            _handle_participant_left(callback_call_sid, db)

        if call_status in ('busy', 'no-answer', 'failed', 'canceled'):
            logger.info(f"Agent callback failed ({call_status}) — redirecting customer to voicemail")
            try:
                twilio_service = get_twilio_service()
                confs = twilio_list(twilio_service.client.conferences,
                    friendly_name=conference, status='in-progress', limit=1
                )
                if confs:
                    for p in twilio_list(twilio_service.client.conferences(confs[0].sid).participants):
                        fail_url = f"{config.webhook_base_url}/api/voice/transfer/failed-message"
                        twilio_service.client.calls(p.call_sid).update(url=fail_url, method='POST')
            except Exception as e:
                logger.warning(f"Could not redirect customer after agent callback failed: {e}")

        return '', 204

    @bp.route('/voice/transfer/failed-message', methods=['POST'])
    def transfer_failed_message():
        """TwiML played to the customer when a blind transfer target doesn't answer."""
        from rinq.api.routes import _go_to_voicemail

        db = get_db()
        call_sid = request.form.get('CallSid', '')
        to_number = request.form.get('To', '')
        from_number = request.form.get('From', '')

        call_log = db.get_call_log_by_sid(call_sid) if hasattr(db, 'get_call_log_by_sid') else None
        called_number = to_number
        if call_log:
            called_number = call_log.get('to_number') or to_number

        routing = db.get_call_routing(called_number) if called_number and called_number.startswith('+') else None
        call_flow = routing.get('call_flow') if routing else None

        if call_flow:
            response_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<Response>']
            result = _go_to_voicemail(response_parts, call_flow, called_number, from_number, call_sid, db, routing,
                                      reason='no_answer')
            if result:
                return result

        return Response('''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Nicole">Sorry, we were unable to connect your call. Please try again later. Goodbye.</Say>
    <Hangup/>
</Response>''', mimetype='application/xml')

    @bp.route('/voice/transfer/context', methods=['GET'])
    @api_or_session_auth
    def transfer_context():
        """Check if an incoming call is a transfer and return context."""
        call_sid = request.args.get('call_sid')
        if not call_sid:
            return jsonify({"is_transfer": False})

        db = get_db()
        transfer = db.get_transfer_by_consult_sid(call_sid)
        if transfer:
            transferred_by = transfer['transferred_by'] or ''
            clean_email = transferred_by.replace('session:', '').replace('api:', '')  # legacy cleanup
            friendly_name = clean_email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
            is_callback = transfer.get('transfer_status') == 'callback'
            result = {
                "is_transfer": True,
                "transferred_by": friendly_name,
                "transfer_type": transfer.get('transfer_type', 'warm'),
                "is_callback": is_callback,
            }
            customer_name = transfer.get('customer_name')
            # The customer's number lives in from_number for inbound calls
            # and to_number for outbound calls. Using from_number blindly
            # would display our own tenant number for outbound transfers.
            if transfer.get('direction') == 'outbound':
                customer_number = transfer.get('to_number') or transfer.get('from_number')
            else:
                customer_number = transfer.get('from_number')
            if customer_name or customer_number:
                result['customer'] = customer_name or customer_number
            return jsonify(result)

        return jsonify({"is_transfer": False})

    @bp.route('/voice/transfer/consult-status', methods=['POST'])
    def transfer_consult_status():
        """Status callback for consultation call during warm transfer."""
        original_call = request.args.get('original_call')
        source = request.args.get('source', 'queued_calls')
        call_status = request.form.get('CallStatus', '')
        logger.info(f"Transfer consult-status callback: original={original_call}, status={call_status}, source={source}")

        db = get_db()

        if not original_call:
            return '', 200

        # Mark the consult call participant as left
        consult_call_sid = request.form.get('CallSid', '')
        if call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled') and consult_call_sid:
            from rinq.api.routes import _handle_participant_left
            _handle_participant_left(consult_call_sid, db)

        # Fetch transfer state up front — we need it to distinguish a real
        # successful transfer (warm_transfer_complete() already ran and
        # marked transfer_status='completed') from a mid-consult hangup
        # that never went through the Complete Transfer button.
        if source == 'call_log':
            transfer_state = db.get_transfer_state_log(original_call)
        else:
            transfer_state = db.get_transfer_state(original_call)
        transfer_status_db = transfer_state.get('transfer_status') if transfer_state else None

        # Consult call ended with a real conversation (completed + duration).
        # For blind/three-way transfers this is always success — target
        # answered and the call ran to completion. For warm transfers it's
        # ambiguous: it could be the natural end of the whole call after
        # Complete Transfer was clicked, or it could be the agent hanging
        # up mid-consult without completing. warm_transfer_complete() sets
        # transfer_status='completed' in the first case, so we can
        # discriminate on that.
        call_duration = int(request.form.get('CallDuration', '0') or '0')
        transfer_type_db = transfer_state.get('transfer_type') if transfer_state else None
        if call_status == 'completed' and call_duration > 0:
            is_warm_mid_consult = (
                transfer_type_db == 'warm'
                and transfer_status_db != 'completed'
            )
            if not is_warm_mid_consult:
                # Blind, three-way, or warm that was properly completed.
                logger.info(
                    f"Transfer consult call ended normally for {original_call} "
                    f"(duration={call_duration}s, type={transfer_type_db})"
                )
                # warm_transfer_complete() already marked it — don't double-set.
                if transfer_status_db != 'completed':
                    if source == 'call_log':
                        db.complete_transfer_log(original_call)
                    else:
                        db.complete_transfer(original_call)
                return '', 200
            # else: fall through to salvage below

        if call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled'):
            # Warm transfer that never saw a Complete Transfer click, or a
            # failed consult (target didn't answer / errored). Either way,
            # the customer is orphaned on hold in the main conference —
            # salvage by running the rejoin flow.
            if call_status == 'completed' and call_duration > 0:
                logger.warning(
                    f"Warm transfer consult ended mid-consult for {original_call} "
                    f"(duration={call_duration}s, transfer_status={transfer_status_db}) "
                    "— customer was not explicitly transferred, attempting rejoin"
                )
            else:
                logger.info(f"Consultation call failed ({call_status}) for transfer {original_call} (source={source})")

            if transfer_state:
                transfer_type = transfer_state.get('transfer_type')
                is_three_way = transfer_type == 'three_way'
                is_blind = transfer_type == 'blind'
                conference_name = transfer_state.get('conference_name')
                consult_conference = transfer_state.get('transfer_consult_conference')

                if is_blind:
                    _handle_failed_blind_transfer(
                        original_call, transfer_state, db
                    )
                elif not is_three_way and conference_name:
                    # Only auto-unhold the customer for a mid-consult disconnect
                    # (target answered, conversation happened, then the consult
                    # leg dropped without Complete Transfer). For pure ring
                    # failures (no-answer/busy/failed/canceled), keep the
                    # customer on hold — the agent can pick another target
                    # without the customer overhearing the apology dance.
                    # Cancel button (warm_transfer_cancel) handles the explicit
                    # "give up and resume" path.
                    is_mid_consult_disconnect = (call_status == 'completed' and call_duration > 0)

                    # Auto-reconnect (flagged): if the consult target's leg
                    # DROPPED mid-consult — not a deliberate hangup — re-ring them
                    # back into the conference instead of failing. The customer
                    # stays held and the originating agent keeps their line; only
                    # the dropped target is missing while we reconnect. Scoped to
                    # our own softphone legs (client:/sip:), where the intent
                    # beacon lets us tell a drop from a hangup.
                    # Gate on transfer_status still 'consulting': a cancel or
                    # complete sets a different status and also ends the consult
                    # leg (server-side, with no intent beacon) — we must NOT treat
                    # those as drops to reconnect.
                    # Wrapped so ANY failure in the reconnect path falls through
                    # to the normal salvage below — reconnect must never be able to
                    # break the existing mid-consult handling.
                    try:
                        if (is_mid_consult_disconnect
                                and transfer_status_db == 'consulting'
                                and _auto_reconnect_enabled(db)
                                and not db.get_leg_intent(consult_call_sid)
                                and not db.get_reconnect_attempt(conference_name)
                                and _others_present(conference_name, db, exclude_sid=consult_call_sid)):
                            target_to = _resolve_consult_target_to(transfer_state, db)
                            if target_to and (target_to.startswith('client:')
                                              or target_to.startswith('sip:')):
                                started = start_leg_reconnect(
                                    conference_name, target_to,
                                    from_number=_consult_caller_id(original_call, db),
                                    role='agent_no_exit',
                                    name=transfer_state.get('transfer_target_name'),
                                    original_call_sid=original_call,
                                    context=json.dumps({'kind': 'transfer', 'source': source}),
                                    db=db,
                                )
                                if started:
                                    db.log_activity(
                                        action="call_transfer_reconnect",
                                        target=original_call,
                                        details=f"Transfer target {transfer_state.get('transfer_target_name')} "
                                                f"dropped mid-consult — reconnecting",
                                        performed_by="twilio",
                                    )
                                    logger.info(f"Mid-consult drop for {original_call} — "
                                                f"reconnecting target instead of failing")
                                    return '', 200
                    except Exception as e:
                        logger.warning(f"Auto-reconnect attempt failed for {original_call}, "
                                       f"falling back to salvage: {e}")

                    if is_mid_consult_disconnect:
                        _unhold_and_rejoin_agent(conference_name, consult_conference)

                    # warm_transfer_start flipped endConferenceOnExit=False on
                    # all existing participants so the consult target could
                    # join without killing the conference. The consult never
                    # joined (or has now left), so restore =True on whoever's
                    # still in there — otherwise the agent hanging up doesn't
                    # end the conference and the customer's leg dangles on
                    # hold music. warm_transfer_complete does this on the
                    # success path; the failure path was missing it.
                    _restore_end_conference_on_exit(conference_name)

            if source == 'call_log':
                db.fail_transfer_log(original_call, call_status)
            else:
                db.fail_transfer(original_call, call_status)

            db.log_activity(
                action="call_transfer_failed",
                target=original_call,
                details=f"Transfer target did not answer: {call_status}",
                performed_by="twilio"
            )

        return '', 200

    @bp.route('/voice/leg-intent', methods=['POST'])
    def leg_intent():
        """Record that a call leg ended on purpose (user pressed End / Go back).

        Posted by the softphone via navigator.sendBeacon just before it
        disconnects, so the server can tell a deliberate hangup apart from a
        network drop. No auth: it only records a marker keyed by a Twilio call
        SID and is harmless. Body may arrive as JSON or text/plain (sendBeacon).
        """
        call_sid = None
        try:
            data = request.get_json(silent=True)
            if not data:
                import json as _json
                raw = request.get_data(as_text=True) or ''
                data = _json.loads(raw) if raw else {}
            call_sid = (data or {}).get('call_sid')
        except Exception:
            call_sid = None
        if call_sid:
            try:
                get_db().record_leg_intent(call_sid, (data or {}).get('intent', 'hangup'))
            except Exception as e:
                logger.warning(f"Could not record leg intent for {call_sid}: {e}")
        return '', 204

    @bp.route('/voice/reconnect-status', methods=['POST'])
    def reconnect_status():
        """Status callback for a leg we're re-ringing back into a conference.

        Drives the reconnect retry loop: on answer, stop showing 'Reconnecting…';
        on end, re-ring again unless the leg was hung up on purpose, no other
        party remains, or we've hit the circuit breaker.
        """
        conference_name = request.args.get('conference')
        call_status = request.form.get('CallStatus', '')
        call_sid = request.form.get('CallSid', '')
        db = get_db()

        if not conference_name:
            return '', 200
        attempt = db.get_reconnect_attempt(conference_name)
        if not attempt:
            return '', 200  # already resolved/cleaned up

        # Only the CURRENT re-ring leg drives state. Twilio retries/duplicates
        # callbacks, and a superseded leg can deliver a late terminal status —
        # those must not start another dial or flip status.
        is_current = (call_sid == attempt.get('dropped_call_sid'))

        if call_status in ('answered', 'in-progress'):
            if not is_current:
                return '', 200
            # Leg rejoined — conference/join re-adds it to participants. Stop the
            # "Reconnecting…" card and point the transfer's consult SID at the new
            # leg so Hand off / Go back target the live leg, not the dead one.
            db.set_reconnect_status(conference_name, 'reconnected')
            try:
                ctx = json.loads(attempt['context']) if attempt.get('context') else {}
            except Exception:
                ctx = {}
            if ctx.get('kind') == 'transfer' and attempt.get('original_call_sid'):
                try:
                    db.update_transfer_consultation(
                        attempt['original_call_sid'], call_sid, conference_name,
                        source=ctx.get('source', 'queued_calls'))
                except Exception as e:
                    logger.warning(f"Could not repoint consult SID after reconnect "
                                   f"for {conference_name}: {e}")
            logger.info(f"Reconnect succeeded for conference {conference_name} (leg {call_sid})")
            return '', 200

        if call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled'):
            # Mark this leg as left regardless of whether it's the current one,
            # so an answered-then-dropped reconnect leg doesn't linger as an
            # active participant and keep _others_present() falsely True.
            try:
                db.remove_participant(call_sid)
            except Exception:
                pass
            if not is_current:
                return '', 200  # stale/duplicate callback — don't drive the retry
            # This re-ring leg ended. Deliberate hangup of the reconnected leg?
            if db.get_leg_intent(call_sid):
                logger.info(f"Reconnect leg {call_sid} ended deliberately — "
                            f"stopping reconnect for {conference_name}")
                _finish_reconnect(attempt, db, gave_up=False)
                return '', 200
            # For a transfer reconnect, stop if the transfer is no longer
            # consulting — the agent cancelled/completed it (or it failed
            # elsewhere), so we must not keep re-ringing the old target.
            try:
                ctx = json.loads(attempt['context']) if attempt.get('context') else {}
            except Exception:
                ctx = {}
            if ctx.get('kind') == 'transfer':
                orig = attempt.get('original_call_sid')
                ts = (db.get_transfer_state_log(orig)
                      if ctx.get('source') == 'call_log'
                      else db.get_transfer_state(orig)) if orig else None
                if not ts or ts.get('transfer_status') != 'consulting':
                    logger.info(f"Reconnect for {conference_name}: transfer no longer "
                                f"consulting — stopping")
                    _finish_reconnect(attempt, db, gave_up=False)
                    return '', 200
            # Still a drop. Stop if nobody else remains or we hit the breaker.
            if not _others_present(conference_name, db, exclude_sid=call_sid):
                logger.info(f"Reconnect for {conference_name}: no other parties left — stopping")
                _finish_reconnect(attempt, db, gave_up=True)
                return '', 200
            if attempt['attempt_count'] >= RECONNECT_ATTEMPT_CAP:
                logger.error(f"Reconnect circuit-breaker hit for {conference_name} after "
                             f"{attempt['attempt_count']} attempts — giving up")
                _finish_reconnect(attempt, db, gave_up=True)
                return '', 200
            # Re-ring.
            new_sid = _place_reconnect_call(attempt, db)
            if new_sid:
                count = db.bump_reconnect_attempt(conference_name, new_sid)
                logger.info(f"Reconnect re-ring #{count} for conference {conference_name} (leg {new_sid})")
            else:
                logger.warning(f"Reconnect re-ring failed to initiate for {conference_name} — giving up")
                _finish_reconnect(attempt, db, gave_up=True)
            return '', 200

        return '', 200


def _handle_failed_blind_transfer(original_call, transfer_state, db):
    """Handle a failed blind transfer — call agent back or redirect to voicemail."""
    xfer_conf = f"call_{original_call}_xfer"
    transferred_by = transfer_state.get('transferred_by', '')

    try:
        twilio_service = get_twilio_service()

        if transferred_by:
            agent_email = transferred_by.replace('session:', '').replace('api:', '')  # legacy cleanup
            agent_identity = f"client:{_email_to_browser_identity(agent_email)}"
            rejoin_url = f"{config.webhook_base_url}/api/voice/conference/join?room={xfer_conf}&role=agent"

            direction = db.get_call_log_field(original_call, 'direction')
            if direction == 'outbound':
                customer_number = db.get_call_log_field(original_call, 'to_number')
            else:
                customer_number = db.get_call_log_field(original_call, 'from_number')
            caller_id = customer_number or get_twilio_config('twilio_default_caller_id')

            callback_status_url = (
                f"{config.webhook_base_url}/api/voice/transfer/callback-status"
                f"?conference={xfer_conf}&customer_call={original_call}"
            )
            try:
                callback_call = twilio_service.client.calls.create(
                    to=agent_identity,
                    from_=caller_id,
                    url=rejoin_url,
                    timeout=15,
                    status_callback=callback_status_url,
                    status_callback_event=['completed', 'busy', 'no-answer', 'failed', 'canceled'],
                )
                logger.info(f"Calling agent {transferred_by} back after failed blind transfer: {callback_call.sid}")
                db.update_queued_call_transfer_status(original_call, 'callback')
                db.update_call_log_transfer_status(original_call, 'callback')
                try:
                    db.update_transfer_consultation(original_call, callback_call.sid, xfer_conf)
                except Exception as e:
                    logger.warning(f"Failed to update transfer consultation for {original_call}: {e}")
            except Exception as e:
                logger.warning(f"Could not call agent back: {e}")
                _redirect_conference_to_voicemail(twilio_service, xfer_conf)
        else:
            _redirect_conference_to_voicemail(twilio_service, xfer_conf)
    except Exception as e:
        logger.warning(f"Could not handle failed blind transfer: {e}")


def _restore_end_conference_on_exit(conference_name):
    """Set endConferenceOnExit=True on every participant currently in the
    conference. Called after a failed warm-transfer consult so the agent
    hanging up actually ends the conference for the customer."""
    try:
        twilio_service = get_twilio_service()
        conferences = twilio_list(twilio_service.client.conferences,
            friendly_name=conference_name, status='in-progress', limit=1
        )
        if not conferences:
            return
        for p in twilio_list(twilio_service.client.conferences(conferences[0].sid).participants):
            try:
                twilio_service.client.conferences(conferences[0].sid).participants(p.call_sid).update(
                    end_conference_on_exit=True
                )
            except Exception as e:
                logger.warning(f"Could not restore endConferenceOnExit for {p.call_sid}: {e}")
    except Exception as e:
        logger.warning(f"Could not restore endConferenceOnExit on conference {conference_name}: {e}")


def _unhold_and_rejoin_agent(conference_name, consult_conference):
    """Take caller off hold and (if separate) redirect agent back to original conference."""
    try:
        twilio_service = get_twilio_service()
        conferences = twilio_list(twilio_service.client.conferences,
            friendly_name=conference_name, status='in-progress', limit=1
        )
        if conferences:
            participants = twilio_list(twilio_service.client.conferences(conferences[0].sid).participants)
            for p in participants:
                if p.hold:
                    twilio_service.client.conferences(conferences[0].sid).participants(p.call_sid).update(hold=False)
    except Exception as e:
        logger.warning(f"Could not take caller off hold: {e}")

    # Only redirect agents back if there is a separate consult conference.
    # In the single-conference warm transfer model consult_conference == conference_name,
    # so skip the redirect (agents are already in the right place).
    if consult_conference and consult_conference != conference_name:
        try:
            twilio_service = get_twilio_service()
            consult_confs = twilio_list(twilio_service.client.conferences,
                friendly_name=consult_conference, status='in-progress', limit=1
            )
            if consult_confs:
                for p in twilio_list(twilio_service.client.conferences(consult_confs[0].sid).participants):
                    rejoin_url = f"{config.webhook_base_url}/api/voice/conference/join?room={conference_name}&role=agent"
                    twilio_service.client.calls(p.call_sid).update(url=rejoin_url, method='POST')
                    logger.info(f"Redirected agent {p.call_sid} back to conference {conference_name}")
        except Exception as e:
            logger.warning(f"Could not redirect agent back to conference: {e}")


def _redirect_conference_to_voicemail(twilio_service, conference_name):
    """Redirect all conference participants to the voicemail message endpoint."""
    confs = twilio_list(twilio_service.client.conferences,
        friendly_name=conference_name, status='in-progress', limit=1
    )
    if confs:
        for p in twilio_list(twilio_service.client.conferences(confs[0].sid).participants):
            fail_url = f"{config.webhook_base_url}/api/voice/transfer/failed-message"
            twilio_service.client.calls(p.call_sid).update(url=fail_url, method='POST')


# =============================================================================
# Leg-drop auto-reconnect (per-tenant flag 'auto_reconnect_enabled', default off)
#
# When one of our own softphone legs (client:/sip:) drops mid-call while other
# parties are still connected, re-ring it back into the SAME conference instead
# of failing the call. Event-driven: each re-ring is a Twilio call whose status
# callback (/voice/reconnect-status) decides whether to re-ring again or stop.
# No background threads — every step runs inside a Twilio webhook request.
#
# A leg that ended on PURPOSE records a leg_intent (the client beacons it before
# disconnecting); a network-dropped leg cannot, so absence of an intent == drop.
# =============================================================================

# Circuit breaker: a hard ceiling on re-rings so a logic bug can't auto-dial
# forever. Far above any real reconnect — should never trip in normal use.
RECONNECT_ATTEMPT_CAP = 30


def _auto_reconnect_enabled(db) -> bool:
    """Per-tenant feature flag (bot_settings 'auto_reconnect_enabled'), default off."""
    try:
        return db.get_bot_setting('auto_reconnect_enabled', '0') == '1'
    except Exception:
        return False


def _others_present(conference_name, db, exclude_sid=None) -> bool:
    """True if at least one OTHER live participant remains in the conference."""
    try:
        for p in (db.get_participants(conference_name) or []):
            if exclude_sid and p['call_sid'] == exclude_sid:
                continue
            return True
    except Exception as e:
        logger.warning(f"Could not check participants for {conference_name}: {e}")
    return False


def _place_reconnect_call(attempt, db) -> str | None:
    """Re-dial the dropped client:/SIP leg back into its conference.

    Returns the new call SID, or None if the dial couldn't even be initiated.
    """
    from urllib.parse import quote
    conference_name = attempt['conference_name']
    role = attempt.get('role') or 'agent'
    base_url = config.webhook_base_url
    join_url = (f"{base_url}/api/voice/conference/join"
                f"?room={quote(conference_name)}&role={quote(role)}")
    status_url = (f"{base_url}/api/voice/reconnect-status"
                  f"?conference={quote(conference_name)}")
    try:
        twilio_service = get_twilio_service()
        from_number = attempt.get('from_number') or get_twilio_config('twilio_default_caller_id')
        call = twilio_service.client.calls.create(
            to=attempt['target_to'],
            from_=from_number,
            url=join_url,
            timeout=25,
            status_callback=status_url,
            status_callback_event=['answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled'],
        )
        return call.sid
    except Exception as e:
        logger.warning(f"Reconnect dial failed for conference {conference_name}: {e}")
        return None


def start_leg_reconnect(conference_name, target_to, *, from_number=None,
                        role='agent', name=None, original_call_sid=None,
                        context=None, db=None) -> bool:
    """Begin reconnecting a dropped client:/SIP leg back into its conference.

    Caller is responsible for having already checked: the flag is on, the leg
    has no deliberate-hangup intent, the target is a client:/sip: leg, and other
    parties remain. Returns True if a re-ring was placed.
    """
    db = db or get_db()
    pseudo = {'conference_name': conference_name, 'target_to': target_to,
              'role': role, 'from_number': from_number}
    new_sid = _place_reconnect_call(pseudo, db)
    if not new_sid:
        return False
    db.upsert_reconnect_attempt(
        conference_name, new_sid, target_to,
        from_number=from_number, role=role, name=name,
        original_call_sid=original_call_sid, context=context,
    )
    logger.info(f"Reconnect started for conference {conference_name}: "
                f"re-ringing {target_to} (leg {new_sid}), reason=drop")
    return True


def _resolve_consult_target_to(transfer_state, db) -> str | None:
    """Resolve a warm-transfer target (extension or number) to the dial string
    we'd re-ring — client:identity for an extension, +E.164 for a number."""
    from rinq.services.transfer_service import _is_extension
    target = (transfer_state.get('transfer_target') or '').strip()
    if not target:
        return None
    if _is_extension(target):
        rec = db.get_staff_extension_by_ext(target)
        if rec and rec.get('email'):
            return f"client:{_email_to_browser_identity(rec['email'])}"
        return None
    try:
        return get_twilio_service()._format_phone_number(target)
    except Exception:
        return target


def _consult_caller_id(original_call, db) -> str:
    """Caller ID to show on the reconnect leg — the customer's number when we
    have it (so the target sees who's calling), else the tenant default."""
    try:
        direction = db.get_call_log_field(original_call, 'direction')
        field = 'to_number' if direction == 'outbound' else 'from_number'
        customer_number = db.get_call_log_field(original_call, field)
        if customer_number and customer_number.startswith('+'):
            return customer_number
    except Exception:
        pass
    return get_twilio_config('twilio_default_caller_id')


def _finish_reconnect(attempt, db, gave_up: bool):
    """Tear down a reconnect attempt. On give-up for a transfer, fall back to
    today's salvage (unhold customer, rejoin originating agent, fail transfer)
    so the worst case is exactly the pre-reconnect behaviour."""
    import json as _json
    conference_name = attempt['conference_name']
    context = {}
    try:
        if attempt.get('context'):
            context = _json.loads(attempt['context'])
    except Exception:
        context = {}

    if gave_up and context.get('kind') == 'transfer':
        original_call = attempt.get('original_call_sid')
        source = context.get('source', 'queued_calls')
        try:
            _unhold_and_rejoin_agent(conference_name, conference_name)
            _restore_end_conference_on_exit(conference_name)
            if original_call:
                if source == 'call_log':
                    db.fail_transfer_log(original_call, 'reconnect_exhausted')
                else:
                    db.fail_transfer(original_call, 'reconnect_exhausted')
                db.log_activity(
                    action="call_transfer_failed",
                    target=original_call,
                    details="Transfer target dropped and could not be reconnected",
                    performed_by="twilio",
                )
        except Exception as e:
            logger.warning(f"Reconnect give-up salvage failed for {conference_name}: {e}")

    db.delete_reconnect_attempt(conference_name)
