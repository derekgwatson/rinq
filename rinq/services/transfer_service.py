"""
Transfer service for Tina.

Handles call transfer operations:
- Blind transfer: Immediately transfer caller to target, agent drops
- Warm transfer: Agent consults with target first, then bridges caller
- 3-way call: All parties talk together, agent drops when ready

Architecture:
- Queue calls: already in a conference, warm transfer uses conference APIs
- Non-queue calls (outbound/direct inbound): escalated to a conference on-the-fly
- Transfer state tracked in queued_calls (queue calls) or call_log (non-queue)
"""

import logging
import secrets
from datetime import datetime

from twilio.base.exceptions import TwilioRestException

from rinq.config import config
from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service, twilio_list
from rinq.tenant.context import get_twilio_config

logger = logging.getLogger(__name__)


def _is_extension(target: str) -> bool:
    """Check if a target looks like an internal extension (4 digits)."""
    return bool(target and target.strip().isdigit() and len(target.strip()) == 4)


class TransferService:
    """Service for call transfer operations."""

    def __init__(self):
        self.twilio = get_twilio_service()
        self._base_url = None

    @property
    def db(self):
        return get_db()

    @property
    def base_url(self):
        """Get webhook base URL. Uses captured value if in a thread, otherwise config."""
        if self._base_url:
            return self._base_url
        return config.webhook_base_url

    def _capture_base_url(self):
        """Capture the base URL from request context for use in background threads."""
        self._base_url = config.webhook_base_url

    def _build_extension_dial_twiml(self, extension: str, caller_id: str,
                                     transferred_by: str = None) -> str | None:
        """Build TwiML to dial a staff member by extension.

        Returns TwiML string or None if extension not found.
        """
        ext_record = self.db.get_staff_extension_by_ext(extension)
        if not ext_record:
            return None

        email = ext_record['email']
        targets = []

        # Browser softphone — include callerName param so the receiving
        # agent sees who is transferring the call
        identity = email.replace('@', '_at_').replace('.', '_')
        if transferred_by:
            caller_name = transferred_by.split('@')[0].replace('.', ' ').replace('_', ' ').title()
            targets.append(
                f'<Client>'
                f'<Identity>{identity}</Identity>'
                f'<Parameter name="callerName" value="Transfer: {caller_name}" />'
                f'</Client>'
            )
        else:
            targets.append(f'<Client>{identity}</Client>')

        # SIP device (if they have one)
        from rinq.api.routes import _get_sip_domain, _get_sip_uri_for_user
        sip_domain = _get_sip_domain()
        if sip_domain:
            sip_uri = _get_sip_uri_for_user(email, sip_domain)
            if sip_uri:
                targets.append(f'<Sip>{sip_uri}</Sip>')

        # Mobile forwarding
        if ext_record.get('forward_to'):
            targets.append(f'<Number>{ext_record["forward_to"]}</Number>')

        if not targets:
            return None

        dial_targets = '\n        '.join(targets)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Please hold while we transfer your call.</Say>
    <Dial callerId="{caller_id}" timeout="30">
        {dial_targets}
    </Dial>
    <Hangup/>
</Response>'''

    def get_transfer_targets(self) -> list[dict]:
        """Get list of available transfer targets (all active staff).

        Pulls from Peter for the full staff list, falls back to queue members.
        """
        targets = []
        seen_emails = set()

        # Use local staff extensions as the primary source
        extensions = self.db.get_all_staff_extensions()
        for ext in extensions:
            if not ext.get('is_active'):
                continue
            email = ext.get('email', '').lower().strip()
            if email and email not in seen_emails:
                seen_emails.add(email)
                # Try to get a friendly name from the users table
                user = self.db.get_user_by_email(email)
                name = (user.get('friendly_name') if user else None) or email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                targets.append({
                    'email': email,
                    'name': name,
                    'has_sip': True,
                    'has_browser': True,
                    'extension': ext.get('extension'),
                })

        if targets:
            return sorted(targets, key=lambda x: x['name'].lower())

        # Fallback: queue members only
        members = self.db.get_all_queue_members()
        for member in members:
            email = member.get('user_email')
            if email and email not in seen_emails:
                seen_emails.add(email)
                email_name = email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                targets.append({
                    'email': email,
                    'name': email_name,
                    'has_sip': True,
                    'has_browser': True,
                })

        return sorted(targets, key=lambda x: x['name'].lower())

    def blind_transfer_direct(self, call_sid: str, target: str, target_name: str,
                              transferred_by: str, caller_id: str = None) -> dict:
        """Execute a blind transfer on a direct call (not in a conference).

        Used for outbound calls made via the softphone. The architecture is:
        - Agent call (call_sid): Browser → Twilio
        - Customer call (child): Twilio → Customer (created by <Dial> in outbound TwiML)

        To transfer the customer, we find and redirect the child call (customer leg)
        to dial the transfer target. The agent's call ends.

        Args:
            call_sid: The agent's call SID (browser → Twilio)
            target: Phone number to transfer to (E.164 format)
            target_name: Display name for logging
            transferred_by: Agent email initiating the transfer
            caller_id: Caller ID to use for the transfer (optional)

        Returns:
            Dict with success status or error
        """
        try:
            # Use provided caller ID or default
            from_number = caller_id or get_twilio_config('twilio_default_caller_id')

            # Handle extension vs phone number
            is_ext = _is_extension(target)
            if is_ext:
                target_e164 = target  # Keep as extension for logging
            else:
                target_e164 = self.twilio._format_phone_number(target)

            # Determine call architecture:
            # - Outbound: agent (parent) -> customer (child) — redirect child
            # - Inbound: customer (parent) -> agent (child via <Dial>) — redirect parent

            # First try: find child calls (outbound scenario)
            child_calls = twilio_list(self.twilio.client.calls, 
                parent_call_sid=call_sid,
                status='in-progress',
                limit=1
            )

            if child_calls:
                # Outbound: redirect the child (customer), end the agent
                customer_call_sid = child_calls[0].sid
                agent_call_sid = call_sid
                logger.info(f"Outbound transfer: agent={call_sid}, customer={customer_call_sid}")
            else:
                # Inbound: call_sid is the agent (child). Find the parent (customer).
                call_info = self.twilio.client.calls(call_sid).fetch()
                parent_sid = call_info.parent_call_sid

                if parent_sid:
                    customer_call_sid = parent_sid
                    agent_call_sid = call_sid
                    logger.info(f"Inbound transfer: agent={call_sid}, customer={parent_sid}")
                else:
                    # No parent, no children — redirect the call itself
                    logger.warning(f"No parent or child found for {call_sid}, redirecting call directly")
                    customer_call_sid = call_sid
                    agent_call_sid = None

            # Build TwiML to dial the transfer target
            if is_ext:
                twiml = self._build_extension_dial_twiml(target, from_number, transferred_by)
                if not twiml:
                    return {'success': False, 'error': f'Extension {target} not found'}
            else:
                twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Please hold while we transfer your call.</Say>
    <Dial callerId="{from_number}" timeout="30">
        <Number>{target_e164}</Number>
    </Dial>
    <Hangup/>
</Response>'''

            # Redirect the customer's call to the transfer TwiML
            self.twilio.client.calls(customer_call_sid).update(twiml=twiml)

            # End the agent's call (they're done)
            if agent_call_sid and agent_call_sid != customer_call_sid:
                try:
                    self.twilio.client.calls(agent_call_sid).update(status='completed')
                except Exception as e:
                    logger.warning(f"Could not end agent call {agent_call_sid}: {e}")

            self.db.log_activity(
                action="call_transfer_blind_direct",
                target=target_e164,
                details=f"Call transferred to {target_name} ({target_e164}). "
                        f"Agent: {agent_call_sid}, Customer: {customer_call_sid}",
                performed_by=transferred_by
            )

            logger.info(f"Direct blind transfer completed: {customer_call_sid} -> {target_e164}")
            return {'success': True, 'transfer_type': 'blind'}

        except TwilioRestException as e:
            logger.error(f"Twilio error during direct blind transfer: {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error during direct blind transfer: {e}")
            return {'success': False, 'error': str(e)}

    def blind_transfer(self, call_sid: str, target: str, target_name: str,
                       transferred_by: str, conference_name_override: str = None) -> dict:
        """Execute a blind (cold) transfer.

        The caller is immediately transferred to the target. The agent
        is disconnected from the call.

        Args:
            call_sid: The original caller's call SID
            target: Phone number to transfer to (E.164 format)
            target_name: Display name for logging
            transferred_by: Agent email initiating the transfer

        Returns:
            Dict with success status or error
        """
        try:
            # Get the call info — use override, queued_calls, or call_log
            queued_call = self.db.get_queued_call_by_sid(call_sid)
            conference_name = conference_name_override
            if not conference_name:
                conference_name = queued_call.get('conference_name') if queued_call else None
            if not conference_name:
                conference_name = self.db.get_call_conference(call_sid)
            if not conference_name:
                return {'success': False, 'error': 'Call not in a conference'}

            # Record the transfer start
            self.db.start_transfer(call_sid, 'blind', target, target_name, transferred_by)

            # Handle extension vs phone number
            is_ext = _is_extension(target)
            if is_ext:
                target_e164 = target
            else:
                target_e164 = self.twilio._format_phone_number(target)

            # Find the conference
            conferences = twilio_list(self.twilio.client.conferences, 
                friendly_name=conference_name,
                status='in-progress',
                limit=1
            )

            if not conferences:
                self.db.fail_transfer(call_sid, 'Conference not found')
                return {'success': False, 'error': 'Conference not found or ended'}

            conference = conferences[0]

            # Get the caller's participant in the conference
            participants = twilio_list(self.twilio.client.conferences(conference.sid).participants)

            caller_participant = None
            agent_participant = None
            for p in participants:
                if p.call_sid == call_sid:
                    caller_participant = p
                else:
                    agent_participant = p

            if not caller_participant:
                self.db.fail_transfer(call_sid, 'Caller not in conference')
                return {'success': False, 'error': 'Caller not found in conference'}

            # Conference-first blind transfer: put customer in a new conference,
            # call the target into it via REST API
            caller_id = (queued_call.get('called_number') if queued_call else None) or get_twilio_config('twilio_default_caller_id')
            new_conference = f"call_{call_sid}_xfer"
            conference_join_url = f"{self.base_url}/api/voice/conference/join?room={new_conference}&role=agent"

            # Resolve target address for REST API call
            if is_ext:
                ext_record = self.db.get_staff_extension_by_ext(target.strip())
                if not ext_record:
                    self.db.fail_transfer(call_sid, f'Extension {target} not found')
                    return {'success': False, 'error': f'Extension {target} not found'}
                from rinq.api.routes import _email_to_browser_identity
                target_to = f"client:{_email_to_browser_identity(ext_record['email'])}"
            else:
                target_to = target_e164

            # Use the customer's number as caller ID so the receiving agent
            # sees who the customer is
            customer_number = None
            call_log = self.db.get_call_log_field(call_sid, 'from_number')
            if call_log and call_log.startswith('+'):
                customer_number = call_log
            transfer_from = customer_number or caller_id or get_twilio_config('twilio_default_caller_id')

            # Call the target into the new conference
            try:
                target_call = self.twilio.client.calls.create(
                    to=target_to,
                    from_=transfer_from,
                    url=conference_join_url,
                    timeout=30,
                )
                logger.info(f"Blind transfer: called target {target_to} as {target_call.sid}")
                # Create proper call_log record for the target call
                self.db.log_call({
                    'call_sid': target_call.sid,
                    'direction': 'inbound',
                    'from_number': transfer_from,
                    'to_number': target_to,
                    'status': 'ringing',
                    'conference_name': new_conference,
                })
            except Exception as e:
                self.db.fail_transfer(call_sid, f'Could not reach {target_name}')
                return {'success': False, 'error': f'Could not reach {target_name}: {e}'}

            # Store conference info BEFORE any more Twilio calls — ensures
            # hold/hangup can find the conference even if later steps fail
            self.db.set_call_conference(call_sid, new_conference)
            self.db.set_call_conference(target_call.sid, new_conference)
            self.db.set_call_child_sid(target_call.sid, call_sid)
            logger.info(f"Stored conference {new_conference} for customer={call_sid}, target={target_call.sid}")

            # Redirect the customer into the new conference
            customer_conf_url = f"{self.base_url}/api/voice/conference/join?room={new_conference}&role=caller"
            self.twilio.client.calls(call_sid).update(url=customer_conf_url, method='POST')

            # End the original agent's call
            if agent_participant:
                try:
                    self.twilio.client.calls(agent_participant.call_sid).update(status='completed')
                except Exception as e:
                    logger.warning(f"Could not end agent call: {e}")

            self.db.complete_transfer(call_sid)

            self.db.log_activity(
                action="call_transfer_blind",
                target=target_e164,
                details=f"Caller {call_sid} blind transferred to {target_name} ({target_e164})",
                performed_by=transferred_by
            )

            logger.info(f"Blind transfer completed: {call_sid} -> {target_e164}")
            return {'success': True, 'transfer_type': 'blind'}

        except TwilioRestException as e:
            logger.error(f"Twilio error during blind transfer: {e}")
            self.db.fail_transfer(call_sid, str(e))
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error during blind transfer: {e}")
            self.db.fail_transfer(call_sid, str(e))
            return {'success': False, 'error': str(e)}

    def warm_transfer_start(self, call_sid: str, target: str, target_name: str,
                            transferred_by: str, agent_call_sid: str,
                            conference_name_override: str = None) -> dict:
        """Start a warm (attended) transfer.

        Puts the caller on hold and initiates a consultation call to the target.
        The agent can then:
        - Complete the transfer (bridge caller to target)
        - Cancel the transfer (hang up on target, resume with caller)

        Args:
            call_sid: The original caller's call SID
            target: Phone number to transfer to (E.164 format)
            target_name: Display name for logging
            transferred_by: Agent email initiating the transfer
            agent_call_sid: The agent's current call SID (for consultation)

        Returns:
            Dict with success status, consult_call_sid, or error
        """
        try:
            # Get the call info — check queued_calls first, then call_log
            queued_call = self.db.get_queued_call_by_sid(call_sid)
            conference_name = conference_name_override
            if not conference_name:
                conference_name = queued_call.get('conference_name') if queued_call else None
            if not conference_name:
                conference_name = self.db.get_call_conference(call_sid)
            if not conference_name:
                return {'success': False, 'error': 'Call not in a conference'}

            # Record the transfer start
            self.db.start_transfer(call_sid, 'warm', target, target_name, transferred_by)

            # Resolve target: extension -> browser client identity, otherwise E.164
            is_ext = _is_extension(target)
            if is_ext:
                ext_record = self.db.get_staff_extension_by_ext(target.strip())
                if not ext_record:
                    self.db.fail_transfer(call_sid, f'Extension {target} not found')
                    return {'success': False, 'error': f'Extension {target} not found'}
                # Use browser client identity — this rings the softphone
                from rinq.api.routes import _email_to_browser_identity
                browser_identity = _email_to_browser_identity(ext_record['email'])
                target_to = f"client:{browser_identity}"
                target_display = target
            else:
                target_to = self.twilio._format_phone_number(target)
                target_display = target_to

            # Create a unique conference room for the consultation
            consult_conference = f"consult_{secrets.token_hex(8)}"

            # Find the original conference and put caller on hold
            conferences = twilio_list(self.twilio.client.conferences, 
                friendly_name=conference_name,
                status='in-progress',
                limit=1
            )

            if not conferences:
                self.db.fail_transfer(call_sid, 'Conference not found')
                return {'success': False, 'error': 'Conference not found'}

            conference = conferences[0]
            hold_url = f"{self.base_url}/api/voice/hold-music"

            # Put the caller on hold
            self.twilio.client.conferences(conference.sid).participants(call_sid).update(
                hold=True,
                hold_url=hold_url
            )

            # IMPORTANT: Set agent's endConferenceOnExit to false BEFORE
            # redirecting them out, otherwise the conference (and caller) dies
            # when the agent leaves to join the consultation conference.
            try:
                self.twilio.client.conferences(conference.sid).participants(agent_call_sid).update(
                    end_conference_on_exit=False
                )
            except Exception as e:
                logger.warning(f"Could not update agent endConferenceOnExit: {e}")

            # Initiate outbound call to the transfer target
            # When they answer, they join a consultation conference with the agent
            consult_twiml_url = (
                f"{self.base_url}/api/voice/transfer/consult-join"
                f"?conference={consult_conference}&original_call={call_sid}"
            )

            caller_id = (queued_call.get('called_number') if queued_call else None) or get_twilio_config('twilio_default_caller_id')

            consult_call = self.twilio.client.calls.create(
                to=target_to,
                from_=caller_id,
                url=consult_twiml_url,
                status_callback=f"{self.base_url}/api/voice/transfer/consult-status?original_call={call_sid}",
                status_callback_event=['initiated', 'ringing', 'answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled']
            )

            # Move the agent to the consultation conference
            agent_consult_url = (
                f"{self.base_url}/api/voice/transfer/agent-consult"
                f"?conference={consult_conference}&original_call={call_sid}"
            )

            self.twilio.client.calls(agent_call_sid).update(url=agent_consult_url)

            # Update the transfer state with consultation details
            self.db.update_transfer_consultation(call_sid, consult_call.sid, consult_conference)

            self.db.log_activity(
                action="call_transfer_warm_start",
                target=target_display,
                details=f"Started warm transfer for {call_sid} to {target_name}. Consult call: {consult_call.sid}",
                performed_by=transferred_by
            )

            logger.info(f"Warm transfer started: {call_sid} -> {target_display}, consult: {consult_call.sid}")
            return {
                'success': True,
                'transfer_type': 'warm',
                'consult_call_sid': consult_call.sid,
                'consult_conference': consult_conference
            }

        except TwilioRestException as e:
            logger.error(f"Twilio error during warm transfer start: {e}")
            self.db.fail_transfer(call_sid, str(e))
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error during warm transfer start: {e}")
            self.db.fail_transfer(call_sid, str(e))
            return {'success': False, 'error': str(e)}

    def warm_transfer_complete(self, call_sid: str, transferred_by: str) -> dict:
        """Complete a warm transfer by bridging caller to transfer target.

        The agent drops off, and the caller is connected to the target.

        Args:
            call_sid: The original caller's call SID
            transferred_by: Agent email completing the transfer

        Returns:
            Dict with success status or error
        """
        try:
            transfer_state = self.db.get_transfer_state(call_sid)
            if not transfer_state:
                return {'success': False, 'error': 'No transfer in progress'}

            if transfer_state['transfer_status'] != 'consulting':
                # Provide a clearer error for failed state
                if transfer_state['transfer_status'] == 'failed':
                    return {
                        'success': False,
                        'error': 'Transfer failed - the original caller may have hung up. '
                                 'You can hang up or continue talking with the transfer target.',
                        'caller_disconnected': True
                    }
                return {'success': False, 'error': f"Cannot complete transfer in state: {transfer_state['transfer_status']}"}

            original_conference = transfer_state['conference_name']
            consult_conference = transfer_state['transfer_consult_conference']
            consult_call_sid = transfer_state['transfer_consult_call_sid']

            # Find the original conference
            conferences = twilio_list(self.twilio.client.conferences, 
                friendly_name=original_conference,
                status='in-progress',
                limit=1
            )

            if not conferences:
                self.db.fail_transfer(call_sid, 'Original caller disconnected')
                return {
                    'success': False,
                    'error': 'The original caller has disconnected. '
                             'You can hang up or continue talking with the transfer target.',
                    'caller_disconnected': True
                }

            conference = conferences[0]

            # Check if the caller is still in the conference
            try:
                participants = twilio_list(self.twilio.client.conferences(conference.sid).participants)
                caller_in_conference = any(p.call_sid == call_sid for p in participants)
                if not caller_in_conference:
                    self.db.fail_transfer(call_sid, 'Original caller disconnected')
                    return {
                        'success': False,
                        'error': 'The original caller has disconnected. '
                                 'You can hang up or continue talking with the transfer target.',
                        'caller_disconnected': True
                    }
            except Exception as e:
                logger.warning(f"Could not check conference participants: {e}")

            # Take the caller off hold
            self.twilio.client.conferences(conference.sid).participants(call_sid).update(
                hold=False
            )

            # Move the transfer target from consultation conference to the original conference
            # We do this by redirecting their call
            target_join_url = (
                f"{self.base_url}/api/voice/transfer/target-join"
                f"?conference={original_conference}"
            )

            self.twilio.client.calls(consult_call_sid).update(url=target_join_url)

            # End the consultation conference (agent will drop when it ends)
            consult_conferences = twilio_list(self.twilio.client.conferences, 
                friendly_name=consult_conference,
                status='in-progress',
                limit=1
            )

            if consult_conferences:
                # End the consultation conference
                self.twilio.client.conferences(consult_conferences[0].sid).update(status='completed')

            self.db.complete_transfer(call_sid)

            self.db.log_activity(
                action="call_transfer_warm_complete",
                target=transfer_state['transfer_target'],
                details=f"Warm transfer completed for {call_sid} to {transfer_state['transfer_target_name']}",
                performed_by=transferred_by
            )

            logger.info(f"Warm transfer completed: {call_sid} -> {transfer_state['transfer_target']}")
            return {'success': True}

        except TwilioRestException as e:
            logger.error(f"Twilio error completing warm transfer: {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error completing warm transfer: {e}")
            return {'success': False, 'error': str(e)}

    def warm_transfer_cancel(self, call_sid: str, cancelled_by: str) -> dict:
        """Cancel a warm transfer and return to the caller.

        Hangs up on the transfer target and takes the caller off hold.

        Args:
            call_sid: The original caller's call SID
            cancelled_by: Agent email cancelling the transfer

        Returns:
            Dict with success status or error
        """
        try:
            transfer_state = self.db.get_transfer_state(call_sid)
            if not transfer_state:
                return {'success': False, 'error': 'No transfer in progress'}

            # If transfer already failed (caller disconnected), just clean up
            if transfer_state['transfer_status'] == 'failed':
                # End any remaining consultation call/conference
                consult_call_sid = transfer_state.get('transfer_consult_call_sid')
                consult_conference = transfer_state.get('transfer_consult_conference')

                if consult_call_sid:
                    try:
                        self.twilio.client.calls(consult_call_sid).update(status='completed')
                    except Exception:
                        pass

                if consult_conference:
                    try:
                        consult_conferences = twilio_list(self.twilio.client.conferences, 
                            friendly_name=consult_conference,
                            status='in-progress',
                            limit=1
                        )
                        if consult_conferences:
                            self.twilio.client.conferences(consult_conferences[0].sid).update(status='completed')
                    except Exception:
                        pass

                self.db.cancel_transfer(call_sid)
                return {
                    'success': True,
                    'caller_disconnected': True,
                    'message': 'Transfer cancelled. The original caller had already disconnected.'
                }

            if transfer_state['transfer_status'] not in ('pending', 'consulting'):
                return {'success': False, 'error': f"Cannot cancel transfer in state: {transfer_state['transfer_status']}"}

            original_conference = transfer_state['conference_name']
            consult_call_sid = transfer_state.get('transfer_consult_call_sid')
            consult_conference = transfer_state.get('transfer_consult_conference')

            # Hang up the consultation call to agent 2 if it exists
            if consult_call_sid:
                try:
                    self.twilio.client.calls(consult_call_sid).update(status='completed')
                except Exception as e:
                    logger.warning(f"Could not end consult call: {e}")

            # Find agent in the consult conference and redirect back BEFORE
            # ending the conference, otherwise the agent's call drops
            if consult_conference and original_conference:
                try:
                    consult_conferences = twilio_list(self.twilio.client.conferences, 
                        friendly_name=consult_conference,
                        status='in-progress',
                        limit=1
                    )
                    if consult_conferences:
                        consult_participants = twilio_list(self.twilio.client.conferences(
                            consult_conferences[0].sid
                        ).participants)
                        for p in consult_participants:
                            # Skip the consult call (agent 2) — only redirect agent 1
                            if consult_call_sid and p.call_sid == consult_call_sid:
                                continue
                            rejoin_url = (
                                f"{self.base_url}/api/voice/conference/join"
                                f"?room={original_conference}&role=agent"
                            )
                            self.twilio.client.calls(p.call_sid).update(
                                url=rejoin_url, method='POST'
                            )
                            logger.info(f"Redirected agent {p.call_sid} back to {original_conference}")
                except Exception as e:
                    logger.warning(f"Could not redirect agent back: {e}")

            # Find the original conference and take caller off hold
            conferences = twilio_list(self.twilio.client.conferences, 
                friendly_name=original_conference,
                status='in-progress',
                limit=1
            )

            if conferences:
                self.twilio.client.conferences(conferences[0].sid).participants(call_sid).update(
                    hold=False
                )

            self.db.cancel_transfer(call_sid)

            self.db.log_activity(
                action="call_transfer_cancelled",
                target=transfer_state.get('transfer_target', 'unknown'),
                details=f"Transfer cancelled for {call_sid}",
                performed_by=cancelled_by
            )

            logger.info(f"Warm transfer cancelled: {call_sid}")
            return {'success': True}

        except TwilioRestException as e:
            logger.error(f"Twilio error cancelling transfer: {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error cancelling transfer: {e}")
            return {'success': False, 'error': str(e)}


    # =========================================================================
    # Universal transfer methods (work for all call types)
    # =========================================================================

    def _identify_call_parties(self, agent_call_sid: str, call_type: str) -> dict:
        """Identify customer and agent call SIDs based on call type.

        Returns:
            Dict with 'customer_call_sid', 'agent_call_sid', or 'error'
        """
        if call_type == 'outbound':
            # Agent is parent, customer is child
            child_calls = twilio_list(self.twilio.client.calls, 
                parent_call_sid=agent_call_sid,
                status='in-progress',
                limit=1
            )
            if child_calls:
                return {
                    'customer_call_sid': child_calls[0].sid,
                    'agent_call_sid': agent_call_sid,
                }

        elif call_type == 'direct_inbound':
            # Agent is child, customer is parent
            call_info = self.twilio.client.calls(agent_call_sid).fetch()
            parent_sid = call_info.parent_call_sid
            if parent_sid:
                return {
                    'customer_call_sid': parent_sid,
                    'agent_call_sid': agent_call_sid,
                }

        # Fallback: agent might be in a conference (e.g. after a prior transfer).
        # Look up via queued_calls to find the customer SID.
        answered = self.db.get_recent_answered_queued_calls(limit=10)
        for qc in answered:
            conf_name = qc.get('conference_name')
            if not conf_name:
                continue
            try:
                confs = twilio_list(self.twilio.client.conferences, 
                    friendly_name=conf_name, status='in-progress', limit=1
                )
                if not confs:
                    continue
                participants = twilio_list(self.twilio.client.conferences(
                    confs[0].sid
                ).participants)
                if any(p.call_sid == agent_call_sid for p in participants):
                    return {
                        'customer_call_sid': qc['call_sid'],
                        'agent_call_sid': agent_call_sid,
                        'conference_name': conf_name,
                    }
            except Exception:
                continue

        if call_type == 'outbound':
            return {'error': 'No active customer call found'}
        elif call_type == 'direct_inbound':
            return {'error': 'No parent call found for this inbound call'}
        return {'error': f'Unknown call type: {call_type}'}

    def warm_transfer_start_universal(self, agent_call_sid: str, target: str,
                                       target_name: str, transferred_by: str,
                                       call_type: str, three_way: bool = False,
                                       customer_call_sid_override: str = None,
                                       conference_name_override: str = None) -> dict:
        """Start a warm transfer or 3-way call for any call type.

        For non-queue calls, escalates to a conference on-the-fly first.

        Args:
            agent_call_sid: The agent's current call SID
            target: Phone number to transfer to
            target_name: Display name for the target
            transferred_by: Agent email
            call_type: 'outbound' or 'direct_inbound'
            three_way: If True, keep customer on the line (3-way call)

        Returns:
            Dict with success, transfer_key, or error
        """
        transfer_type = 'three_way' if three_way else 'warm'

        try:
            # Identify customer and agent call SIDs
            if customer_call_sid_override:
                parties = {
                    'customer_call_sid': customer_call_sid_override,
                    'agent_call_sid': agent_call_sid,
                    'conference_name': conference_name_override,
                }
            else:
                parties = self._identify_call_parties(agent_call_sid, call_type)
            if 'error' in parties:
                return {'success': False, 'error': parties['error']}

            customer_call_sid = parties['customer_call_sid']
            agent_sid = parties['agent_call_sid']
            is_ext = _is_extension(target)

            # Get a valid caller ID: use the Twilio number the customer called
            caller_id = None
            try:
                customer_call = self.twilio.client.calls(customer_call_sid).fetch()
                caller_id = customer_call.to  # The Twilio number that was called
            except Exception:
                pass
            if not caller_id:
                caller_id = get_twilio_config('twilio_default_caller_id')
            if not caller_id:
                # Last resort: grab any Twilio number we own
                numbers = self.db.get_phone_numbers()
                if numbers:
                    caller_id = numbers[0].get('phone_number')
            if not caller_id:
                return {'success': False, 'error': 'No caller ID available for transfer'}

            # Resolve target: extension -> client identity, phone -> E.164
            if is_ext:
                ext_record = self.db.get_staff_extension_by_ext(target)
                if not ext_record:
                    return {'success': False, 'error': f'Extension {target} not found'}
                target_email = ext_record['email']
                target_identity = f"client:{target_email.replace('@', '_at_').replace('.', '_')}"
                target_e164 = target  # Keep for logging
            else:
                target_e164 = self.twilio._format_phone_number(target)
                target_identity = target_e164

            # Create conference for the call
            # Use customer_call_sid as the transfer key — that's the SID in call_log
            transfer_key = customer_call_sid
            conference_name = f"transfer_{agent_sid}"

            # Store conference name and transfer state in call_log
            self.db.update_call_log(transfer_key, {'conference_name': conference_name})
            self.db.start_transfer_log(
                transfer_key, transfer_type, target_e164, target_name, transferred_by
            )

            if three_way:
                # 3-Way: add the target to the EXISTING conference.
                # Agent and customer stay where they are — no redirects needed.
                existing_conf = parties.get('conference_name') or conference_name

                # Set ALL participants to endConferenceOnExit=false so anyone
                # can leave without killing the conference for the others
                try:
                    confs = twilio_list(self.twilio.client.conferences, 
                        friendly_name=existing_conf, status='in-progress', limit=1
                    )
                    if confs:
                        for p in twilio_list(self.twilio.client.conferences(confs[0].sid).participants):
                            self.twilio.client.conferences(confs[0].sid).participants(p.call_sid).update(
                                end_conference_on_exit=False
                            )
                except Exception as e:
                    logger.warning(f"Could not update endConferenceOnExit: {e}")

                # Call the target into the existing conference
                target_join_url = (
                    f"{self.base_url}/api/voice/conference/join"
                    f"?room={existing_conf}&role=agent"
                )
                consult_call = self.twilio.client.calls.create(
                    to=target_identity,
                    from_=caller_id,
                    url=target_join_url,
                    status_callback=(
                        f"{self.base_url}/api/voice/transfer/consult-status"
                        f"?original_call={transfer_key}&source=call_log"
                    ),
                    status_callback_event=['answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled']
                )

                self.db.update_transfer_consultation_log(
                    transfer_key, consult_call.sid, existing_conf
                )

            else:
                # Warm: customer on hold, agent in consult conference with target
                # IMPORTANT: redirect agent first — redirecting the parent (customer)
                # on a direct inbound call kills the child (agent) call

                # Create consultation conference
                consult_conference = f"consult_{secrets.token_hex(8)}"

                # Call the target into the consultation conference
                consult_twiml_url = (
                    f"{self.base_url}/api/voice/transfer/consult-join"
                    f"?conference={consult_conference}&original_call={transfer_key}"
                )
                consult_call = self.twilio.client.calls.create(
                    to=target_identity,
                    from_=caller_id,
                    url=consult_twiml_url,
                    status_callback=(
                        f"{self.base_url}/api/voice/transfer/consult-status"
                        f"?original_call={transfer_key}&source=call_log"
                    ),
                    status_callback_event=['answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled']
                )

                # Redirect agent to consultation conference
                agent_consult_url = (
                    f"{self.base_url}/api/voice/transfer/agent-consult"
                    f"?conference={consult_conference}&original_call={transfer_key}"
                )
                self.twilio.client.calls(agent_sid).update(url=agent_consult_url)

                # Now redirect customer to conference (on hold)
                customer_conf_url = (
                    f"{self.base_url}/api/voice/conference/join"
                    f"?room={conference_name}&role=caller"
                )
                self.twilio.client.calls(customer_call_sid).update(url=customer_conf_url)

                self.db.update_transfer_consultation_log(
                    transfer_key, consult_call.sid, consult_conference
                )

            self.db.log_activity(
                action=f"call_transfer_{transfer_type}_start",
                target=target_e164,
                details=f"Started {transfer_type} transfer for {agent_sid} to {target_name}",
                performed_by=transferred_by
            )

            logger.info(f"Universal {transfer_type} transfer started: {transfer_key} -> {target_e164}")
            return {
                'success': True,
                'transfer_type': transfer_type,
                'transfer_key': transfer_key,
                'agent_call_sid': agent_sid,
            }

        except TwilioRestException as e:
            logger.error(f"Twilio error during universal transfer start: {e}")
            try:
                self.db.fail_transfer_log(transfer_key, str(e))
            except Exception:
                pass
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error during universal transfer start: {e}")
            try:
                self.db.fail_transfer_log(transfer_key, str(e))
            except Exception:
                pass
            return {'success': False, 'error': str(e)}

    def warm_transfer_complete_universal(self, transfer_key: str,
                                          transferred_by: str) -> dict:
        """Complete a warm transfer or 3-way call (non-queue calls).

        For warm: move target from consult conference to main conference with customer.
        For 3-way: agent just leaves (all parties already in one conference).
        """
        try:
            state = self.db.get_transfer_state_log(transfer_key)
            if not state:
                return {'success': False, 'error': 'No transfer in progress'}

            if state['transfer_status'] == 'failed':
                return {
                    'success': False,
                    'error': 'Transfer failed - the customer may have hung up.',
                    'caller_disconnected': True
                }

            if state['transfer_status'] != 'consulting':
                return {'success': False, 'error': f"Cannot complete transfer in state: {state['transfer_status']}"}

            conference_name = state['conference_name']
            consult_call_sid = state['transfer_consult_call_sid']
            consult_conference = state.get('transfer_consult_conference')

            if state['transfer_type'] == 'three_way':
                # 3-way: all parties already in one conference
                # Agent just needs to leave — their call ends
                # The conference continues with customer and target
                #
                # Clean up Tina's involvement: mark call as done and clear
                # conference name so any subsequent Twilio callbacks don't
                # trigger queue alerts or other side effects
                self.db.complete_transfer_log(transfer_key)
                self.db.update_call_log(transfer_key, {'conference_name': None})

                self.db.log_activity(
                    action="call_transfer_three_way_complete",
                    target=state['transfer_target'],
                    details=f"Agent left 3-way call, customer connected to {state['transfer_target_name']}",
                    performed_by=transferred_by
                )
                return {'success': True, 'agent_should_hangup': True}

            else:
                # Warm: move target from consult conference to main conference
                # Take customer off hold first
                conferences = twilio_list(self.twilio.client.conferences, 
                    friendly_name=conference_name,
                    status='in-progress',
                    limit=1
                )

                if not conferences:
                    self.db.fail_transfer_log(transfer_key, 'Customer disconnected')
                    return {
                        'success': False,
                        'error': 'The customer has disconnected.',
                        'caller_disconnected': True
                    }

                # Take customer off hold
                conference = conferences[0]
                participants = twilio_list(self.twilio.client.conferences(conference.sid).participants)
                for p in participants:
                    if p.hold:
                        self.twilio.client.conferences(conference.sid).participants(p.call_sid).update(
                            hold=False
                        )

                # Move target to main conference
                target_join_url = (
                    f"{self.base_url}/api/voice/transfer/target-join"
                    f"?conference={conference_name}"
                )
                self.twilio.client.calls(consult_call_sid).update(url=target_join_url)

                # End consultation conference
                if consult_conference:
                    consult_confs = twilio_list(self.twilio.client.conferences, 
                        friendly_name=consult_conference,
                        status='in-progress',
                        limit=1
                    )
                    if consult_confs:
                        self.twilio.client.conferences(consult_confs[0].sid).update(status='completed')

                self.db.complete_transfer_log(transfer_key)

                self.db.log_activity(
                    action="call_transfer_warm_complete",
                    target=state['transfer_target'],
                    details=f"Warm transfer completed: customer connected to {state['transfer_target_name']}",
                    performed_by=transferred_by
                )
                return {'success': True}

        except TwilioRestException as e:
            logger.error(f"Twilio error completing universal transfer: {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error completing universal transfer: {e}")
            return {'success': False, 'error': str(e)}

    def warm_transfer_cancel_universal(self, transfer_key: str,
                                        cancelled_by: str,
                                        agent_call_sid: str = None) -> dict:
        """Cancel a warm transfer or 3-way call (non-queue calls).

        Hangs up on the target, moves agent back to customer.
        """
        try:
            state = self.db.get_transfer_state_log(transfer_key)
            if not state:
                return {'success': False, 'error': 'No transfer in progress'}

            consult_call_sid = state.get('transfer_consult_call_sid')
            consult_conference = state.get('transfer_consult_conference')
            conference_name = state['conference_name']

            # If already failed, just clean up
            if state['transfer_status'] == 'failed':
                if consult_call_sid:
                    try:
                        self.twilio.client.calls(consult_call_sid).update(status='completed')
                    except Exception:
                        pass
                self.db.cancel_transfer_log(transfer_key)
                return {'success': True, 'caller_disconnected': True}

            if state['transfer_status'] not in ('pending', 'consulting'):
                return {'success': False, 'error': f"Cannot cancel in state: {state['transfer_status']}"}

            # For warm transfer: redirect agent back to main conference FIRST
            # (before ending the consult conference which would kill their call)
            if state['transfer_type'] == 'warm' and agent_call_sid:
                agent_conf_url = (
                    f"{self.base_url}/api/voice/conference/join"
                    f"?room={conference_name}&role=agent"
                )
                self.twilio.client.calls(agent_call_sid).update(url=agent_conf_url)

            # Hang up the target/consult call
            if consult_call_sid:
                try:
                    self.twilio.client.calls(consult_call_sid).update(status='completed')
                except Exception as e:
                    logger.warning(f"Could not end consult call: {e}")

            # End consultation conference if it exists (warm transfer)
            if consult_conference and consult_conference != conference_name:
                try:
                    consult_confs = twilio_list(self.twilio.client.conferences, 
                        friendly_name=consult_conference,
                        status='in-progress',
                        limit=1
                    )
                    if consult_confs:
                        self.twilio.client.conferences(consult_confs[0].sid).update(status='completed')
                except Exception as e:
                    logger.warning(f"Could not end consult conference: {e}")

            # Take customer off hold in main conference
            if state['transfer_type'] == 'warm':
                conferences = twilio_list(self.twilio.client.conferences, 
                    friendly_name=conference_name,
                    status='in-progress',
                    limit=1
                )
                if conferences:
                    participants = twilio_list(self.twilio.client.conferences(conferences[0].sid).participants)
                    for p in participants:
                        if p.hold:
                            self.twilio.client.conferences(conferences[0].sid).participants(p.call_sid).update(
                                hold=False
                            )

            # For 3-way: agent is already in the main conference, just removed the target
            # Nothing else needed — agent and customer continue talking

            self.db.cancel_transfer_log(transfer_key)

            self.db.log_activity(
                action="call_transfer_cancelled",
                target=state.get('transfer_target', 'unknown'),
                details=f"Transfer cancelled for {transfer_key}",
                performed_by=cancelled_by
            )

            logger.info(f"Universal transfer cancelled: {transfer_key}")
            return {'success': True}

        except TwilioRestException as e:
            logger.error(f"Twilio error cancelling universal transfer: {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Error cancelling universal transfer: {e}")
            return {'success': False, 'error': str(e)}


# Singleton instance
_service = None


def get_transfer_service() -> TransferService:
    """Get transfer service singleton."""
    global _service
    if _service is None:
        _service = TransferService()
    return _service
