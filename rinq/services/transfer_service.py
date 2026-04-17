"""
Transfer service for Rinq.

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
from datetime import datetime

from twilio.base.exceptions import TwilioRestException

from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

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
                                     transferred_by: str = None,
                                     customer_number: str = None) -> str | None:
        """Build TwiML to dial a staff member by extension.

        Returns TwiML string or None if extension not found.

        Args:
            extension: The extension to dial.
            caller_id: Caller ID for the <Dial>. Must be a verified/owned
                number since the ring group may include PSTN mobile forward
                destinations.
            transferred_by: Optional email of the agent initiating the transfer.
            customer_number: Optional customer phone number. When set, it is
                surfaced on agent 2's browser display via the callerName
                custom parameter so they see who the customer is.
        """
        ext_record = self.db.get_staff_extension_by_ext(extension)
        if not ext_record:
            return None

        email = ext_record['email']
        targets = []

        # Browser softphone — include callerName param so the receiving
        # agent sees the customer they're about to pick up (preferred),
        # otherwise at least who is transferring the call.
        identity = email.replace('@', '_at_').replace('.', '_')
        if customer_number:
            caller_label = f'Transfer: {customer_number}'
        elif transferred_by:
            caller_name = transferred_by.split('@')[0].replace('.', ' ').replace('_', ' ').title()
            caller_label = f'Transfer: {caller_name}'
        else:
            caller_label = None
        if caller_label:
            targets.append(
                f'<Client>'
                f'<Identity>{identity}</Identity>'
                f'<Parameter name="callerName" value="{xml_escape(caller_label)}" />'
                f'</Client>'
            )
        else:
            targets.append(f'<Client>{identity}</Client>')

        # SIP device (if they have one)
        from rinq.services.sip import get_sip_domain, get_sip_uri_for_user as _get_sip_uri_for_user
        sip_domain = get_sip_domain()
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
        """Get list of available transfer targets (all staff with extensions).

        Uses local staff extensions as the primary source, supplemented by
        queue members who may not have extensions yet.
        """
        targets = []
        seen_emails = set()

        # All staff with extensions are valid transfer targets
        extensions = self.db.get_all_staff_extensions()
        for ext in extensions:
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

        # Also include queue members who don't have extensions yet
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
                ext_record = self.db.get_staff_extension_by_ext(target.strip())
                if ext_record and ext_record.get('dnd_enabled'):
                    return {'success': False, 'error': f'{target_name} is on Do Not Disturb', 'on_dnd': True}
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

            customer_display_number = None
            if child_calls:
                # Outbound: redirect the child (customer), end the agent
                customer_call_sid = child_calls[0].sid
                agent_call_sid = call_sid
                # The child's "to" is the number we dialled — i.e. the customer
                customer_display_number = getattr(child_calls[0], 'to', None)
                logger.info(f"Outbound transfer: agent={call_sid}, customer={customer_call_sid}")
            else:
                # Inbound: call_sid is the agent (child). Find the parent (customer).
                call_info = self.twilio.client.calls(call_sid).fetch()
                parent_sid = call_info.parent_call_sid

                if parent_sid:
                    customer_call_sid = parent_sid
                    agent_call_sid = call_sid
                    # The parent's "from" is the customer who called in
                    try:
                        parent_call = self.twilio.client.calls(parent_sid).fetch()
                        customer_display_number = getattr(parent_call, '_from', None)
                    except Exception as e:
                        logger.warning(f"Could not fetch parent call for customer ID: {e}")
                    logger.info(f"Inbound transfer: agent={call_sid}, customer={parent_sid}")
                else:
                    # No parent, no children — redirect the call itself
                    logger.warning(f"No parent or child found for {call_sid}, redirecting call directly")
                    customer_call_sid = call_sid
                    agent_call_sid = None

            # Build TwiML to dial the transfer target
            # Use dial action to handle rejection — calls back agent 1 or routes to voicemail
            dial_action = (
                f"{self.base_url}/api/voice/transfer/direct-dial-status"
                f"?transferred_by={quote(transferred_by or '', safe='')}"
                f"&customer_call_sid={quote(customer_call_sid, safe='')}"
            )
            if is_ext:
                twiml = self._build_extension_dial_twiml(target, from_number, transferred_by, customer_number=customer_display_number)
                if not twiml:
                    return {'success': False, 'error': f'Extension {target} not found'}
                # Inject dial action into the TwiML
                twiml = twiml.replace('<Dial ', f'<Dial action="{xml_escape(dial_action)}" ')
            else:
                twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Please hold while we transfer your call.</Say>
    <Dial callerId="{from_number}" timeout="30" action="{xml_escape(dial_action)}">
        <Number>{target_e164}</Number>
    </Dial>
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
            fallback_caller_id = (queued_call.get('called_number') if queued_call else None) or get_twilio_config('twilio_default_caller_id')
            new_conference = f"call_{call_sid}_xfer"
            conference_join_url = f"{self.base_url}/api/voice/conference/join?room={new_conference}&role=agent"

            # Resolve target address for REST API call
            if is_ext:
                ext_record = self.db.get_staff_extension_by_ext(target.strip())
                if not ext_record:
                    self.db.fail_transfer(call_sid, f'Extension {target} not found')
                    return {'success': False, 'error': f'Extension {target} not found'}
                if ext_record.get('dnd_enabled'):
                    self.db.fail_transfer(call_sid, 'dnd')
                    return {'success': False, 'error': f'{target_name} is on Do Not Disturb', 'on_dnd': True}
                from rinq.api.identity import email_to_browser_identity as _email_to_browser_identity
                target_to = f"client:{_email_to_browser_identity(ext_record['email'])}"
            else:
                target_to = target_e164

            # Agent 2 should see the customer's number, so they know who they're
            # about to pick up. Customer's number lives in from_number for
            # inbound calls, to_number for outbound. Only use it when the
            # target is a browser/SIP device — for PSTN destinations Twilio
            # requires a verified/owned number, so fall back in that case.
            customer_field = 'to_number' if self.db.get_call_log_field(call_sid, 'direction') == 'outbound' else 'from_number'
            customer_number = self.db.get_call_log_field(call_sid, customer_field)
            if not (customer_number and customer_number.startswith('+')):
                customer_number = None
            target_is_client_or_sip = target_to.startswith('client:') or target_to.startswith('sip:')
            transfer_from = customer_number if (customer_number and target_is_client_or_sip) else fallback_caller_id

            # Call the target into the new conference
            status_callback_url = (
                f"{self.base_url}/api/voice/transfer/consult-status"
                f"?original_call={call_sid}&source=queued_calls"
            )
            try:
                target_call = self.twilio.client.calls.create(
                    to=target_to,
                    from_=transfer_from,
                    url=conference_join_url,
                    timeout=30,
                    status_callback=status_callback_url,
                    status_callback_event=['answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled'],
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
            # Store consult call SID so transfer context lookup works
            self.db.update_transfer_consultation(call_sid, target_call.sid, new_conference)
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

            # Don't mark transfer as completed yet — wait for the target to
            # actually answer (handled by consult-status callback). If they
            # reject, the callback will call agent 1 back.

            # Update agent_email so presence shows the new agent, not the old one
            new_agent_email = ext_record.get('email') if is_ext else None
            if new_agent_email:
                self.db.update_call_log(call_sid, {'agent_email': new_agent_email})

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
                            conference_name_override: str = None,
                            three_way: bool = False) -> dict:
        """Start a warm transfer or 3-way call.

        Warm transfer: puts the caller on hold, initiates a consultation call
        to the target, and moves the agent to the consultation conference.
        The agent can then complete (bridge caller to target) or cancel.

        3-way: calls the target into the EXISTING conference so all three
        parties can talk. Customer stays connected; agent hangs up to hand off
        or stays for a true 3-way.

        Args:
            call_sid: The original caller's call SID
            target: Extension (4 digits), browser identity, or E.164 number
            target_name: Display name for logging
            transferred_by: Agent email initiating the transfer
            agent_call_sid: The agent's current call SID
            conference_name_override: Override if conference name is known
            three_way: If True, keep customer connected (3-way call)

        Returns:
            Dict with success status, consult_call_sid, or error
        """
        transfer_type = 'three_way' if three_way else 'warm'
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
            self.db.start_transfer(call_sid, transfer_type, target, target_name, transferred_by)

            # Resolve target: extension -> browser client identity, otherwise E.164
            is_ext = _is_extension(target)
            if is_ext:
                ext_record = self.db.get_staff_extension_by_ext(target.strip())
                if not ext_record:
                    self.db.fail_transfer(call_sid, f'Extension {target} not found')
                    return {'success': False, 'error': f'Extension {target} not found'}
                # Refuse up-front if the target has DND on, rather than letting
                # the consult ring and come back "busy" — clearer for the agent
                # and doesn't interrupt the target.
                if ext_record.get('dnd_enabled'):
                    self.db.fail_transfer(call_sid, 'dnd')
                    return {'success': False, 'error': f'{target_name} is on Do Not Disturb', 'on_dnd': True}
                from rinq.api.identity import email_to_browser_identity as _email_to_browser_identity
                browser_identity = _email_to_browser_identity(ext_record['email'])
                target_to = f"client:{browser_identity}"
                target_display = target
            else:
                target_to = self.twilio._format_phone_number(target)
                target_display = target_to

            # Find the existing conference
            conferences = twilio_list(self.twilio.client.conferences,
                friendly_name=conference_name,
                status='in-progress',
                limit=1
            )
            if not conferences:
                self.db.fail_transfer(call_sid, 'Conference not found')
                return {'success': False, 'error': 'Conference not found'}

            conference = conferences[0]

            # Caller ID: use customer's number for client/SIP targets; fall back
            # to the tenant's main number for PSTN targets.
            fallback_caller_id = (queued_call.get('called_number') if queued_call else None) or get_twilio_config('twilio_default_caller_id')
            customer_field = 'to_number' if self.db.get_call_log_field(call_sid, 'direction') == 'outbound' else 'from_number'
            customer_number = self.db.get_call_log_field(call_sid, customer_field)
            if not (customer_number and customer_number.startswith('+')):
                customer_number = None
            target_is_client_or_sip = target_to.startswith('client:') or target_to.startswith('sip:')
            consult_from = customer_number if (customer_number and target_is_client_or_sip) else fallback_caller_id

            if three_way:
                # 3-Way: call the target directly into the existing conference.
                # No hold, no consult conference — all three parties connect.
                # Set all existing participants to endConferenceOnExit=false so
                # anyone can leave without killing the conference for others.
                try:
                    for p in twilio_list(self.twilio.client.conferences(conference.sid).participants):
                        self.twilio.client.conferences(conference.sid).participants(p.call_sid).update(
                            end_conference_on_exit=False
                        )
                except Exception as e:
                    logger.warning(f"Could not update endConferenceOnExit for 3-way: {e}")

                target_join_url = (
                    f"{self.base_url}/api/voice/conference/join"
                    f"?room={conference_name}&role=agent_no_exit"
                )
                consult_call = self.twilio.client.calls.create(
                    to=target_to,
                    from_=consult_from,
                    url=target_join_url,
                    status_callback=f"{self.base_url}/api/voice/transfer/consult-status?original_call={call_sid}",
                    status_callback_event=['answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled']
                )

                # For 3-way the "consult conference" is the main conference itself
                self.db.update_transfer_consultation(call_sid, consult_call.sid, conference_name)

                self.db.log_activity(
                    action="call_transfer_three_way_start",
                    target=target_display,
                    details=f"Started 3-way call for {call_sid} to {target_name}. Call: {consult_call.sid}",
                    performed_by=transferred_by
                )
                logger.info(f"3-way call started: {call_sid} -> {target_display}, call: {consult_call.sid}")
                return {
                    'success': True,
                    'transfer_type': 'three_way',
                    'consult_call_sid': consult_call.sid,
                    'consult_conference': conference_name,
                }

            # Warm transfer (single-conference model): put caller on hold, then
            # call Agent 2 into the SAME main conference. Agent 1 stays put —
            # no redirect to a separate consult conference.
            hold_url = f"{self.base_url}/api/voice/hold-music"
            self.twilio.client.conferences(conference.sid).participants(call_sid).update(
                hold=True,
                hold_url=hold_url
            )

            # Set endConferenceOnExit=false for all existing participants so
            # anyone can leave without killing the conference for others.
            try:
                for p in twilio_list(self.twilio.client.conferences(conference.sid).participants):
                    self.twilio.client.conferences(conference.sid).participants(p.call_sid).update(
                        end_conference_on_exit=False
                    )
            except Exception as e:
                logger.warning(f"Could not update endConferenceOnExit for warm transfer: {e}")

            # Call Agent 2 directly into the existing conference (same as 3-way)
            target_join_url = (
                f"{self.base_url}/api/voice/conference/join"
                f"?room={conference_name}&role=agent_no_exit"
            )
            consult_call = self.twilio.client.calls.create(
                to=target_to,
                from_=consult_from,
                url=target_join_url,
                status_callback=f"{self.base_url}/api/voice/transfer/consult-status?original_call={call_sid}",
                status_callback_event=['answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled']
            )

            # consult_conference IS the main conference in the single-conf model
            self.db.update_transfer_consultation(call_sid, consult_call.sid, conference_name)

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
                'consult_conference': conference_name,
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
        """Complete a warm transfer or 3-way call.

        Warm: bridges caller to target (agent drops off).
        3-way: agent signals they're done; all parties are already connected.

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
                if transfer_state['transfer_status'] == 'failed':
                    return {
                        'success': False,
                        'error': 'Transfer failed - the original caller may have hung up. '
                                 'You can hang up or continue talking with the transfer target.',
                        'caller_disconnected': True
                    }
                return {'success': False, 'error': f"Cannot complete transfer in state: {transfer_state['transfer_status']}"}

            original_conference = transfer_state['conference_name']
            consult_call_sid = transfer_state['transfer_consult_call_sid']

            # 3-way: all parties are already in one conference — just mark done.
            if transfer_state.get('transfer_type') == 'three_way':
                self.db.complete_transfer(call_sid)
                self.db.log_activity(
                    action="call_transfer_three_way_complete",
                    target=transfer_state['transfer_target'],
                    details=f"Agent left 3-way call, customer connected to {transfer_state['transfer_target_name']}",
                    performed_by=transferred_by
                )
                logger.info(f"3-way call completed: {call_sid}")
                return {'success': True, 'agent_should_hangup': True}

            # Single-conference model: Agent 2 is already in the main conference.
            # Just unhold the customer and tell Agent 1 to hang up.
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

            self.db.complete_transfer(call_sid)

            self.db.log_activity(
                action="call_transfer_warm_complete",
                target=transfer_state['transfer_target'],
                details=f"Warm transfer completed for {call_sid} to {transfer_state['transfer_target_name']}",
                performed_by=transferred_by
            )
            logger.info(f"Warm transfer completed: {call_sid} -> {transfer_state['transfer_target']}")
            return {'success': True, 'agent_should_hangup': True}

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

                if consult_call_sid and consult_conference:
                    try:
                        consult_confs = twilio_list(self.twilio.client.conferences,
                            friendly_name=consult_conference, status='in-progress', limit=1)
                        if consult_confs:
                            self.twilio.client.conferences(consult_confs[0].sid).participants(
                                consult_call_sid
                            ).delete()
                        self.db.remove_participant(consult_call_sid)
                    except Exception:
                        pass

                self.db.cancel_transfer(call_sid)
                return {
                    'success': True,
                    'caller_disconnected': True,
                    'message': 'Transfer cancelled. The original caller had already disconnected.'
                }

            if transfer_state['transfer_status'] not in ('pending', 'consulting', 'completed'):
                return {'success': False, 'error': f"Cannot cancel transfer in state: {transfer_state['transfer_status']}"}

            original_conference = transfer_state['conference_name']
            consult_call_sid = transfer_state.get('transfer_consult_call_sid')
            consult_conference = transfer_state.get('transfer_consult_conference')
            is_three_way = transfer_state.get('transfer_type') == 'three_way'

            # Remove Agent 2: try conference participant.delete() first (if they've
            # already joined), fall back to cancelling the outbound call (if still ringing).
            if consult_call_sid:
                kicked = False
                if consult_conference:
                    try:
                        consult_confs = twilio_list(self.twilio.client.conferences,
                            friendly_name=consult_conference, status='in-progress', limit=1)
                        if consult_confs:
                            self.twilio.client.conferences(consult_confs[0].sid).participants(
                                consult_call_sid
                            ).delete()
                            kicked = True
                            self.db.remove_participant(consult_call_sid)
                    except TwilioRestException as e:
                        if e.status != 404:
                            logger.warning(f"Could not kick consult participant: {e}")
                    except Exception as e:
                        logger.warning(f"Could not kick consult participant: {e}")
                if not kicked:
                    # Not in conference yet (still ringing) — cancel outbound call
                    try:
                        self.twilio.client.calls(consult_call_sid).update(status='completed')
                    except Exception as e:
                        logger.warning(f"Could not cancel ringing consult call: {e}")

            if not is_three_way:
                # Single-conference warm: Agent 1 never left the main conference,
                # so no redirect needed. Just unhold the customer.
                conferences = twilio_list(self.twilio.client.conferences,
                    friendly_name=original_conference,
                    status='in-progress',
                    limit=1
                )
                if conferences:
                    self.twilio.client.conferences(conferences[0].sid).participants(call_sid).update(
                        hold=False
                    )
            # 3-way cancel: agent and customer are already talking — nothing to undo.

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


    def _identify_call_parties(self, agent_call_sid: str, call_type: str) -> dict:
        """Removed — every call is now conference-backed from the start.

        Kept as a tombstone so any lingering call sites get a clear error
        rather than an AttributeError.
        """
        raise NotImplementedError(
            "_identify_call_parties was removed; all transfers use warm_transfer_start directly."
        )




# Singleton instance
_service = None


def get_transfer_service() -> TransferService:
    """Get transfer service singleton."""
    global _service
    if _service is None:
        _service = TransferService()
    return _service
