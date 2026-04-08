"""Transfer API routes — blind, warm, 3-way, and transfer webhooks.

Extracted from routes.py. Registered via register(api_bp) at import time.
"""

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
        call_type = data.get('call_type', 'queue')
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

        if three_way and conf_name and customer_sid:
            result = transfer_service.warm_transfer_start_universal(
                agent_call_sid, target, target_name, transferred_by,
                call_type, three_way=True,
                customer_call_sid_override=customer_sid,
                conference_name_override=conf_name
            )
        elif conf_name and customer_sid:
            if not customer_sid or not target or not agent_call_sid:
                return jsonify({"error": "call_sid, target, and agent_call_sid required"}), 400
            result = transfer_service.warm_transfer_start(
                customer_sid, target, target_name, transferred_by, agent_call_sid,
                conference_name_override=conf_name
            )
            if result.get('success'):
                result['transfer_key'] = customer_sid
        else:
            if not agent_call_sid or not target:
                return jsonify({"error": "agent_call_sid and target required"}), 400
            result = transfer_service.warm_transfer_start_universal(
                agent_call_sid, target, target_name, transferred_by,
                call_type, three_way=three_way
            )

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

        result = transfer_service.warm_transfer_complete(call_sid, transferred_by)
        if not result.get('success') and result.get('error') == 'No transfer in progress':
            result = transfer_service.warm_transfer_complete_universal(call_sid, transferred_by)

        # Mark original agent as left (they've handed off the call)
        if result.get('success'):
            db = get_db()
            agent_call_sid = result.get('agent_call_sid')
            if agent_call_sid:
                db.remove_participant(agent_call_sid)

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
        if not result.get('success') and 'not found' in (result.get('error', '').lower()):
            agent_call_sid = data.get('agent_call_sid')
            result = transfer_service.warm_transfer_cancel_universal(call_sid, cancelled_by, agent_call_sid=agent_call_sid)

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
        """TwiML to move agent to consultation conference."""
        conference = request.args.get('conference')
        if not conference:
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response><Say>Sorry, an error occurred.</Say><Hangup/></Response>', mimetype='application/xml')

        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference beep="false" startConferenceOnEnter="true" endConferenceOnExit="true">
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

        call_duration = int(request.form.get('CallDuration', '0') or '0')
        if call_status == 'completed' and call_duration > 0:
            logger.info(f"Transfer target call ended normally for {original_call} (duration={call_duration}s)")
            if source == 'call_log':
                db.complete_transfer_log(original_call)
            else:
                db.complete_transfer(original_call)
            return '', 200

        if call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled'):
            logger.info(f"Consultation call failed ({call_status}) for transfer {original_call} (source={source})")

            if source == 'call_log':
                transfer_state = db.get_transfer_state_log(original_call)
            else:
                transfer_state = db.get_transfer_state(original_call)

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
                    _unhold_and_rejoin_agent(conference_name, consult_conference)

            if source == 'call_log':
                db.fail_transfer_log(original_call, f"Consultation call: {call_status}")
            else:
                db.fail_transfer(original_call, f"Consultation call: {call_status}")

            db.log_activity(
                action="call_transfer_failed",
                target=original_call,
                details=f"Transfer target did not answer: {call_status}",
                performed_by="twilio"
            )

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


def _unhold_and_rejoin_agent(conference_name, consult_conference):
    """Take caller off hold and redirect agent back to original conference."""
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

    if consult_conference:
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
