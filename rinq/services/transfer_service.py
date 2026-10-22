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
import uuid
from datetime import datetime

from twilio.base.exceptions import TwilioException, TwilioRestException

from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from rinq.config import config
from rinq.database.db import get_db
from rinq.services.person_devices import person_ring_targets, mobile_screen_url
from rinq.services.twilio_service import get_twilio_service, twilio_list
from rinq.tenant.context import get_twilio_config

logger = logging.getLogger(__name__)

# Per-tenant bot_settings flag: transfers to a person ring every device they
# have (browser + desk phone + screened mobile), not just the browser.
# Default off. Toggled at /admin/settings.
TRANSFER_RING_ALL_KEY = 'transfer_ring_all_devices'

# transfer_failure_reason values after which the server has taken the customer
# OFF hold (transfer_routes: mid-consult disconnect, failed auto-reconnect).
# Every other failure (no-answer, busy, failed, dnd, canceled) leaves them held.
# phone.html mirrors this list in TRANSFER_RESUMED_REASONS.
RESUMED_FAILURE_REASONS = ('completed', 'reconnect_exhausted')


def _is_extension(target: str) -> bool:
    """Check if a target looks like an internal extension (4 digits)."""
    return bool(target and target.strip().isdigit() and len(target.strip()) == 4)


class TransferService:
    """Service for call transfer operations."""

    def __init__(self):
        self.twilio = get_twilio_service()

    @property
    def db(self):
        return get_db()

    @property
    def base_url(self):
        """Webhook base URL, resolved per request.

        Never cache this on the instance: TransferService is a process-wide
        singleton shared across tenants, so captured state leaks the first
        tenant's domain into every later transfer.
        """
        return config.webhook_base_url

    # =========================================================================
    # Ringing every device a transfer target has
    # =========================================================================
    # Gated by the per-tenant flag below. Off: a transfer to an extension
    # rings only the target's browser (the old behaviour). On: it rings the
    # same devices a queue or direct call would — browser, desk phone and a
    # screened mobile (services/person_devices.py) — and the first to answer
    # takes the call. Bookkeeping in migration 082.

    def ring_all_devices_enabled(self) -> bool:
        try:
            return self.db.get_bot_setting(TRANSFER_RING_ALL_KEY, '0') == '1'
        except Exception as e:
            logger.warning(f"Could not read {TRANSFER_RING_ALL_KEY} — defaulting to off: {e}")
            return False

    def _ring_person_for_transfer(self, *, original_call: str, email: str, kind: str,
                                  conference: str, role: str, client_from: str,
                                  pstn_from: str | None, label: str,
                                  source: str = 'queued_calls', timeout: int = 30) -> dict:
        """Ring every device `email` has into `conference`.

        kind: 'blind' | 'warm' | 'three_way' — decides the winner's bookkeeping.
        client_from: caller ID for browser/SIP legs (the customer's number).
        pstn_from: caller ID for the mobile leg — must be a number we own
            (gotcha 3); no mobile leg without one.

        Returns {'group_key', 'primary_sid', 'all_failed'}. primary_sid is the
        leg recorded as the consult call until one answers (the browser leg
        when there is one, so the target's softphone still shows "Transfer
        from …"). primary_sid None = nothing could be rung. all_failed = every
        leg died before placing finished; no status callback will report
        that, so the caller must handle it.
        """
        from rinq.services.sip import get_sip_domain

        targets = person_ring_targets(email, self.db, get_sip_domain())
        if not pstn_from:
            targets = [t for t in targets if t.kind != 'mobile']
        if not targets:
            return {'group_key': None, 'primary_sid': None, 'all_failed': False}

        group_key = f"xfer:{original_call}:{uuid.uuid4().hex[:8]}"
        # expected starts above any real leg count, so a leg that dies while
        # the others are still being placed can't read as "all failed".
        self.db.create_transfer_ring_group(group_key, original_call, 99, {
            'kind': kind,
            'conference': conference,
            'source': source,
            'email': email,
            'client_from': client_from,
            'pstn_from': pstn_from,
        })

        join_path = f"/api/voice/conference/join?room={quote(conference, safe='')}&role={role}"
        status_url = (
            f"{self.base_url}/api/voice/transfer/consult-status"
            f"?original_call={quote(original_call, safe='')}&source={source}"
            f"&ring_group={quote(group_key, safe='')}"
        )

        placed = []
        try:
            for target in targets:
                if (self.db.get_transfer_ring_group(group_key) or {}).get('winner_sid'):
                    break  # someone already answered on a phone we rang first
                self._place_transfer_leg(group_key, target, email, original_call, join_path,
                                         status_url, client_from, pstn_from, label, timeout,
                                         placed)
        except Exception:
            # Part-way through (e.g. the database was locked). Hang up what we
            # rang and let the caller fail the transfer — never leave phones
            # ringing for a group that can no longer fail.
            logger.exception(f"Transfer {original_call}: placing ring legs failed ({group_key})")
            self.db.abandon_transfer_ring_group(group_key)
            for sid, _kind in placed:
                try:
                    self.twilio.client.calls(sid).update(status='completed')
                except Exception as e:
                    logger.debug(f"Could not cancel transfer ring leg {sid}: {e}")
            raise

        if not placed:
            self.db.finalize_transfer_ring_group(group_key, 0)
            return {'group_key': group_key, 'primary_sid': None, 'all_failed': False}

        primary_sid = next((sid for sid, k in placed if k == 'browser'), placed[0][0])
        # Recorded as the consult call (so the target's softphone shows
        # "Transfer from ...") — unless a leg already answered, in which case
        # its claim has recorded itself and must not be overwritten.
        self.db.set_transfer_consult_unless_answered(original_call, primary_sid, conference,
                                                     group_key, source=source)
        all_failed = self.db.finalize_transfer_ring_group(group_key, len(placed))
        winner = (self.db.get_transfer_ring_group(group_key) or {}).get('winner_sid')
        if winner:
            # Answered while we were still dialling: anything placed after
            # the claim's own cancel sweep is still ringing.
            self.cancel_transfer_ring_legs(group_key, except_sid=winner)
        return {'group_key': group_key, 'primary_sid': primary_sid, 'all_failed': all_failed}

    def _place_transfer_leg(self, group_key, target, email, original_call, join_path,
                            status_url, client_from, pstn_from, label, timeout, placed):
        if target.kind == 'mobile':
            screened_path = (
                f"/api/voice/transfer/screened-answer"
                f"?ring_group={quote(group_key, safe='')}&next={quote(join_path, safe='')}"
            )
            url = mobile_screen_url(self.base_url, screened_path, label)
            leg_status_url = f"{status_url}&screened=1"
            from_ = pstn_from
        else:
            url = f"{self.base_url}{join_path}"
            leg_status_url = status_url
            from_ = client_from
        try:
            call = self.twilio.client.calls.create(
                to=target.to,
                from_=from_,
                url=url,
                timeout=timeout,
                status_callback=leg_status_url,
                status_callback_event=['answered', 'completed'],
            )
        except Exception as e:
            logger.warning(f"Transfer {original_call}: could not ring {target.kind} {target.to}: {e}")
            return
        placed.append((call.sid, target.kind))  # first, so a failure below still hangs it up
        self.db.add_transfer_ring_leg(group_key, call.sid, target.kind, target.to)
        if target.kind == 'sip':
            self.db.stamp_sip_activity(email)
            self.db.stamp_sip_ring_attempt(email)
        logger.info(f"Transfer {original_call}: ringing {target.kind} {target.to} as {call.sid} ({group_key})")

    def claim_transfer_leg(self, group_key: str, call_sid: str) -> bool:
        """First leg to answer wins the transfer: stop the others ringing and
        point the transfer's bookkeeping at the leg that answered."""
        if not self.db.claim_transfer_ring_group(group_key, call_sid):
            return False

        group = self.db.get_transfer_ring_group(group_key) or {}
        ctx = group.get('context') or {}
        original_call = group.get('original_call_sid')
        conference = ctx.get('conference')
        self.cancel_transfer_ring_legs(group_key, except_sid=call_sid)

        self.db.update_transfer_consultation(original_call, call_sid, conference,
                                             source=ctx.get('source', 'queued_calls'))
        leg = self.db.get_transfer_ring_leg(group_key, call_sid) or {}
        if leg.get('kind') == 'mobile' and ctx.get('email'):
            # conference/join can name a browser or desk phone from its address;
            # a mobile is just a number, so record who it is before it joins
            # (as _claim_inbound_answer does) — the card shows their name and
            # talk time is credited to them.
            user = self.db.get_user_by_email(ctx['email'])
            name = (user.get('friendly_name') if user else None) or ctx['email']
            self.db.add_participant(conference, call_sid, 'agent', name=name, email=ctx['email'])
        if ctx.get('kind') == 'blind':
            # What blind_transfer did for its single target leg up front.
            to_addr = leg.get('to_addr') or ''
            from_number = ctx.get('pstn_from') if leg.get('kind') == 'mobile' else ctx.get('client_from')
            self.db.log_call({
                'call_sid': call_sid,
                'direction': 'inbound',
                'from_number': from_number,
                'to_number': to_addr,
                'status': 'ringing',
                'conference_name': conference,
            })
            self.db.set_call_conference(call_sid, conference)
            self.db.set_call_child_sid(call_sid, original_call)
        logger.info(f"Transfer {original_call}: {call_sid} answered ({group_key})")
        return True

    def cancel_transfer_ring_legs(self, group_key: str, except_sid: str = None) -> None:
        """Hang up every leg of a transfer that is still ringing."""
        if not group_key:
            return
        for sid in self.db.get_ringing_transfer_legs(group_key):
            if sid == except_sid:
                continue
            try:
                self.twilio.client.calls(sid).update(status='completed')
            except Exception as e:
                logger.debug(f"Could not cancel transfer ring leg {sid}: {e}")

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

        # Who is calling — the browser shows it via the callerName param:
        # the customer they're about to pick up (preferred), otherwise at
        # least who is transferring the call.
        if customer_number:
            caller_label = f'Transfer: {customer_number}'
        elif transferred_by:
            caller_name = transferred_by.split('@')[0].replace('.', ' ').replace('_', ' ').title()
            caller_label = f'Transfer: {caller_name}'
        else:
            caller_label = None

        # Same device list as every other way of ringing a person
        # (services/person_devices). The mobile is left out: in a <Dial> the
        # first leg to pick up wins before any "press 1" screen could run, so
        # a mobile's voicemail would take the transfer. This path predates
        # conferences and is only reached for a call that isn't in one.
        from rinq.services.sip import get_sip_domain
        for t in person_ring_targets(email, self.db, get_sip_domain()):
            if t.kind == 'browser':
                identity = t.to[len('client:'):]
                if caller_label:
                    targets.append(
                        f'<Client>'
                        f'<Identity>{identity}</Identity>'
                        f'<Parameter name="callerName" value="{xml_escape(caller_label)}" />'
                        f'</Client>'
                    )
                else:
                    targets.append(f'<Client>{identity}</Client>')
            elif t.kind == 'sip':
                targets.append(f'<Sip>{t.to}</Sip>')

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
        address_book = self.db.get_address_book()
        book_by_email = {(e.get('email') or '').lower().strip(): e
                         for e in address_book if e.get('email')}

        # All staff with extensions are valid transfer targets
        extensions = self.db.get_all_staff_extensions()
        for ext in extensions:
            email = ext.get('email', '').lower().strip()
            if email and email not in seen_emails:
                seen_emails.add(email)
                # Try to get a friendly name from the users table
                user = self.db.get_user_by_email(email)
                name = (user.get('friendly_name') if user else None) or email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                target = {
                    'email': email,
                    'name': name,
                    'has_sip': True,
                    'has_browser': True,
                    'extension': ext.get('extension'),
                }
                # Their mobile too, so an agent can transfer to someone who
                # isn't at their desk in one click instead of looking the
                # number up in Contacts and typing it in. A transfer to an
                # extension only rings the browser, so for anyone not logged
                # in to Tina the mobile is the only way through. Same rule
                # as the Contacts list: hidden if they ticked "hide my mobile".
                book = book_by_email.get(email) or {}
                if not ext.get('hide_mobile'):
                    mobile = book.get('mobile_e164') or ext.get('forward_to')
                    if mobile:
                        target['mobile'] = mobile
                        target['display_mobile'] = (book.get('display_mobile')
                                                    if book.get('mobile_e164') else None) or mobile
                target['position'] = book.get('position') or ''
                target['section'] = book.get('section') or ''
                targets.append(target)

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

        targets.sort(key=lambda x: x['name'].lower())

        # Address book mobiles (e.g. fitters, synced from Peter) for people
        # who aren't on Tina — lets an agent warm-transfer to a fitter, ask
        # how far away they are, then "Go back" to the customer. Anyone who
        # has an extension is already listed above, with their mobile.
        mobiles = []
        for entry in address_book:
            phone = entry.get('mobile_e164')
            email = (entry.get('email') or '').lower().strip()
            if not phone or (email and email in seen_emails):
                continue
            mobiles.append({
                'name': entry.get('name') or entry.get('display_mobile') or phone,
                'phone': phone,
                'display_mobile': entry.get('display_mobile') or phone,
                'position': entry.get('position') or '',
                'section': entry.get('section') or '',
                'is_mobile': True,
            })
        mobiles.sort(key=lambda x: x['name'].lower())

        return targets + mobiles

    def get_showroom_targets(self) -> list[dict]:
        """Queues an agent can transfer a caller into, so the whole store rings.

        Every active queue except test ones. Returned separately from
        get_transfer_targets() so an old cached phone page can't mistake a
        showroom for a person.
        """
        return [
            {'queue_id': q['id'], 'name': q['name'],
             'member_count': q.get('member_count') or 0}
            for q in self.db.get_queues()
            if 'test' not in (q.get('name') or '').lower()
        ]

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

            # Find the conference. Strict list mode: empty → fail_transfer, so
            # a transient API error must raise rather than look like "ended".
            try:
                conferences = twilio_list(self.twilio.client.conferences,
                    friendly_name=conference_name,
                    status='in-progress',
                    limit=1,
                    raise_on_error=True
                )
            except (TwilioRestException, TwilioException) as e:
                logger.error(f"Could not look up conference for blind transfer of {call_sid}: {e}")
                return {
                    'success': False,
                    'error': 'Could not verify the call state (Twilio API error) — try again.',
                    'retryable': True
                }

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
            ring_group = None
            if is_ext and self.ring_all_devices_enabled():
                # Every device they have; the first to answer takes the call
                # (claim_transfer_leg does the bookkeeping below for the winner).
                rung = self._ring_person_for_transfer(
                    original_call=call_sid, email=ext_record['email'], kind='blind',
                    conference=new_conference, role='agent',
                    client_from=customer_number or fallback_caller_id,
                    pstn_from=fallback_caller_id,
                    label='Transferred call',
                )
                ring_group = rung['group_key']
                if not rung['primary_sid'] or rung['all_failed']:
                    # Nothing rang, or every device failed at once (e.g. only
                    # a browser, and it isn't open). The customer is still with
                    # the agent — say so rather than calling the agent back.
                    self.db.fail_transfer(call_sid, 'no-answer')
                    return {'success': False, 'error': f"Couldn't reach {target_name} on any of their phones"}
                target_sid = rung['primary_sid']
            else:
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
                    logger.warning(f"Blind transfer: could not call target {target_to} "
                                   f"for {call_sid}: {e}")
                    self.db.fail_transfer(call_sid, f'Could not reach {target_name}')
                    return {'success': False, 'error': f'Could not reach {target_name}: {e}'}
                target_sid = target_call.sid

            # Store conference info BEFORE any more Twilio calls — ensures
            # hold/hangup can find the conference even if later steps fail
            self.db.set_call_conference(call_sid, new_conference)
            if not ring_group:
                # A ring group's winner gets these when it answers — writing
                # them for every leg would leave a call_log row per phone.
                self.db.set_call_conference(target_sid, new_conference)
                self.db.set_call_child_sid(target_sid, call_sid)
            # Store consult call SID so transfer context lookup works (a ring
            # group's placer already did, unless a leg had answered)
            if not ring_group:
                self.db.update_transfer_consultation(call_sid, target_sid, new_conference)
            logger.info(f"Stored conference {new_conference} for customer={call_sid}, target={target_sid}")

            # Redirect the customer into the new conference
            customer_conf_url = f"{self.base_url}/api/voice/conference/join?room={new_conference}&role=caller"
            try:
                self.twilio.client.calls(call_sid).update(url=customer_conf_url, method='POST')
            except Exception as e:
                # Customer likely hung up mid-transfer. Cancel the still-ringing
                # target leg so their phone doesn't ring into an empty conference
                # (canceling a ringing leg never runs its TwiML, so gotcha 21's
                # after-dial concern doesn't apply here).
                logger.warning(f"Blind transfer: could not redirect customer {call_sid}: {e}")
                if ring_group:
                    self.cancel_transfer_ring_legs(ring_group)
                else:
                    try:
                        self.twilio.client.calls(target_sid).update(status='canceled')
                    except Exception as cancel_err:
                        logger.warning(f"Blind transfer: could not cancel target leg {target_sid}: {cancel_err}")
                self.db.fail_transfer(call_sid, 'Caller disconnected before transfer')
                return {'success': False, 'error': 'Caller disconnected before transfer completed'}

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

    def _place_consult_legs(self, *, call_sid, is_ext, ext_record, kind, conference_name,
                            target_to, target_join_url, consult_from, customer_number,
                            fallback_caller_id):
        """Ring the warm / 3-way target into the main conference and record the
        consult leg. Returns the consult call SID (with every-device ringing,
        the browser leg until one answers), or None if nothing could be rung —
        in which case the transfer is already marked failed and, for warm, the
        customer is still held as for any ring failure.
        """
        status_callback = f"{self.base_url}/api/voice/transfer/consult-status?original_call={call_sid}"
        if not (is_ext and self.ring_all_devices_enabled()):
            consult_call = self.twilio.client.calls.create(
                to=target_to,
                from_=consult_from,
                url=target_join_url,
                status_callback=status_callback,
                status_callback_event=['answered', 'completed', 'busy', 'no-answer', 'failed', 'canceled']
            )
            # consult_conference IS the main conference in the single-conf model
            self.db.update_transfer_consultation(call_sid, consult_call.sid, conference_name)
            return consult_call.sid

        rung = self._ring_person_for_transfer(
            original_call=call_sid, email=ext_record['email'], kind=kind,
            conference=conference_name, role='agent_no_exit',
            client_from=customer_number or fallback_caller_id,
            pstn_from=fallback_caller_id,
            label='Transferred call',
            timeout=60,  # Twilio's default, which the single-leg consult used
        )
        if rung['primary_sid'] and not rung['all_failed']:
            return rung['primary_sid']

        # Every device failed before we finished ringing them (e.g. only a
        # browser, and it isn't open), so no status callback will report it.
        # Do what consult-status does for a ring failure: the customer stays
        # held so the agent can try someone else (3-way: never held), and
        # endConferenceOnExit goes back on so the agent hanging up still ends
        # the call.
        from rinq.api.transfer_routes import _restore_end_conference_on_exit
        _restore_end_conference_on_exit(conference_name)
        self.db.fail_transfer(call_sid, 'no-answer')
        self.db.log_activity(
            action="call_transfer_failed",
            target=call_sid,
            details="Transfer target did not answer: no-answer (no device could be rung)",
            performed_by="twilio"
        )
        return None

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
                consult_sid = self._place_consult_legs(
                    call_sid=call_sid, is_ext=is_ext, ext_record=ext_record if is_ext else None,
                    kind='three_way', conference_name=conference_name,
                    target_to=target_to, target_join_url=target_join_url,
                    consult_from=consult_from, customer_number=customer_number,
                    fallback_caller_id=fallback_caller_id,
                )
                if not consult_sid:
                    return {'success': False, 'error': f"Couldn't reach {target_name} on any of their phones"}

                self.db.log_activity(
                    action="call_transfer_three_way_start",
                    target=target_display,
                    details=f"Started 3-way call for {call_sid} to {target_name}. Call: {consult_sid}",
                    performed_by=transferred_by
                )
                logger.info(f"3-way call started: {call_sid} -> {target_display}, call: {consult_sid}")
                return {
                    'success': True,
                    'transfer_type': 'three_way',
                    'consult_call_sid': consult_sid,
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
            consult_sid = self._place_consult_legs(
                call_sid=call_sid, is_ext=is_ext, ext_record=ext_record if is_ext else None,
                kind='warm', conference_name=conference_name,
                target_to=target_to, target_join_url=target_join_url,
                consult_from=consult_from, customer_number=customer_number,
                fallback_caller_id=fallback_caller_id,
            )
            if not consult_sid:
                return {'success': False, 'error': f"Couldn't reach {target_name} on any of their phones"}

            self.db.log_activity(
                action="call_transfer_warm_start",
                target=target_display,
                details=f"Started warm transfer for {call_sid} to {target_name}. Consult call: {consult_sid}",
                performed_by=transferred_by
            )
            logger.info(f"Warm transfer started: {call_sid} -> {target_display}, consult: {consult_sid}")
            return {
                'success': True,
                'transfer_type': 'warm',
                'consult_call_sid': consult_sid,
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

    def warm_transfer_complete(self, call_sid: str, transferred_by: str,
                                agent_call_sid: str = None) -> dict:
        """Complete a warm transfer or 3-way call.

        Warm: bridges caller to target (agent drops off).
        3-way: agent signals they're done; all parties are already connected.

        Args:
            call_sid: The original caller's call SID
            transferred_by: Agent email completing the transfer
            agent_call_sid: The initiating agent's browser-leg SID. If provided,
                we forcibly remove their participant from the conference to
                guarantee their leg ends — the client-side disconnect has
                proven flaky in the field.

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

            # Guard: only the initiating agent can complete the handoff. If the
            # caller (customer) or the consult target sends a complete request,
            # refuse — otherwise a misclick by the recipient would prematurely
            # take the caller off hold.
            if agent_call_sid and agent_call_sid in (call_sid, consult_call_sid):
                logger.warning(f"Rejecting handoff from wrong party: agent_call_sid={agent_call_sid}, call_sid={call_sid}, consult_call_sid={consult_call_sid}")
                return {'success': False, 'error': 'Only the transferring agent can complete the handoff'}

            # 3-way: all parties are already in one conference — just mark done.
            if transfer_state.get('transfer_type') == 'three_way':
                # Restore endConferenceOnExit=True on the caller and the new
                # agent so either hanging up ends the conference cleanly. Agent
                # 1 is removed below and shouldn't end the conference.
                conferences = twilio_list(self.twilio.client.conferences,
                    friendly_name=original_conference,
                    status='in-progress',
                    limit=1
                )
                if conferences:
                    for participant_sid in (call_sid, consult_call_sid):
                        try:
                            self.twilio.client.conferences(conferences[0].sid).participants(participant_sid).update(
                                end_conference_on_exit=True
                            )
                        except Exception as e:
                            logger.warning(f"Could not restore endConferenceOnExit for 3-way participant {participant_sid}: {e}")

                    # Forcibly remove Agent 1 — same reasoning as warm path.
                    if agent_call_sid and agent_call_sid not in (call_sid, consult_call_sid):
                        try:
                            self.twilio.client.conferences(conferences[0].sid).participants(agent_call_sid).delete()
                            logger.info(f"Removed initiating agent {agent_call_sid} from 3-way conference {original_conference}")
                        except Exception as e:
                            logger.warning(f"Could not remove initiating agent {agent_call_sid} from 3-way conference: {e}")

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
            # Strict list mode: an empty result here is interpreted as "the
            # caller hung up" and fails the transfer — a swallowed transient
            # API error must not masquerade as that.
            try:
                conferences = twilio_list(self.twilio.client.conferences,
                    friendly_name=original_conference,
                    status='in-progress',
                    limit=1,
                    raise_on_error=True
                )
            except (TwilioRestException, TwilioException) as e:
                logger.error(f"Could not verify conference state for hand-off of {call_sid}: {e}")
                return {
                    'success': False,
                    'error': 'Could not verify the call state (Twilio API error). '
                             'The customer may still be on hold — try Hand off again.',
                    'retryable': True
                }

            if not conferences:
                self.db.fail_transfer(call_sid, 'Original caller disconnected')
                return {
                    'success': False,
                    'error': 'The original caller has disconnected. '
                             'You can hang up or continue talking with the transfer target.',
                    'caller_disconnected': True
                }

            conference = conferences[0]

            # Check who is still in the conference before committing the hand-off.
            # If the consult target already left we must NOT remove the agent —
            # that would leave the customer alone. Abort and put them back together.
            try:
                participants = twilio_list(
                    self.twilio.client.conferences(conference.sid).participants,
                    raise_on_error=True
                )
                present_sids = {p.call_sid for p in participants}

                if call_sid not in present_sids:
                    self.db.fail_transfer(call_sid, 'Original caller disconnected')
                    return {
                        'success': False,
                        'error': 'The original caller has disconnected. '
                                 'You can hang up or continue talking with the transfer target.',
                        'caller_disconnected': True
                    }

                if consult_call_sid and consult_call_sid not in present_sids:
                    # Target hung up before agent pressed Hand Off — abort and rejoin
                    self.db.fail_transfer(call_sid, 'Transfer target disconnected before hand-off')
                    try:
                        self.twilio.client.conferences(conference.sid).participants(call_sid).update(hold=False)
                    except Exception as e:
                        logger.warning(f"Could not take caller {call_sid} off hold in conference "
                                       f"{conference.sid} after target disconnected: {e}")
                    target_name = transfer_state.get('transfer_target_name', 'The transfer target')
                    return {
                        'success': False,
                        'error': f'{target_name} has ended the call. You are back with the caller.',
                        'target_disconnected': True
                    }

            except Exception as e:
                logger.warning(f"Could not check conference participants: {e}")

            # Take the caller off hold
            self.twilio.client.conferences(conference.sid).participants(call_sid).update(
                hold=False
            )

            # Restore endConferenceOnExit=True on the caller and the new agent
            # so either hanging up ends the conference cleanly. (Set to False
            # at warm-start so the consult could happen without killing it.)
            # Agent 1 is left as-is — they're about to be removed immediately
            # below, and we don't want their removal to end the conference.
            for participant_sid in (call_sid, consult_call_sid):
                try:
                    self.twilio.client.conferences(conference.sid).participants(participant_sid).update(
                        end_conference_on_exit=True
                    )
                except Exception as e:
                    logger.warning(f"Could not restore endConferenceOnExit for {participant_sid}: {e}")

            # Forcibly remove Agent 1 from the conference so their leg ends
            # regardless of whether the browser-side disconnect fires. The
            # client-side `callRef.disconnect()` has proven flaky in the field
            # — agents reported staying on the line after pressing Hand off.
            if agent_call_sid and agent_call_sid not in (call_sid, consult_call_sid):
                try:
                    self.twilio.client.conferences(conference.sid).participants(agent_call_sid).delete()
                    logger.info(f"Removed initiating agent {agent_call_sid} from conference {original_conference}")
                except Exception as e:
                    logger.warning(f"Could not remove initiating agent {agent_call_sid} from conference: {e}")

            self.db.complete_transfer(call_sid)

            # Clear any in-flight reconnect for this conference (no-op unless the
            # feature is enabled) — the hand-off is done, don't keep re-ringing.
            try:
                self.db.delete_reconnect_attempt(original_conference)
            except Exception as e:
                logger.warning(f"Could not clear reconnect attempt for {original_conference} "
                               f"after warm transfer complete: {e}")

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

            # Every-device ringing: this transfer's ring group (found through
            # its consult leg — an earlier transfer of the same customer has
            # its own group, whose winner may be the agent pressing Go back).
            # Closed now, so no leg can answer from here on; a leg that won
            # between the read above and the close is hung up further down.
            ring_group = self.db.get_transfer_ring_group_for_leg(
                call_sid, transfer_state.get('transfer_consult_call_sid'))
            if ring_group:
                self.db.close_transfer_ring_group(ring_group['group_key'])

            # Transfer already failed — clean up any lingering consult leg then
            # check whether the customer's conference is still alive before
            # telling the client the caller disconnected.  The transfer can fail
            # because the target hung up (customer still waiting) OR because the
            # customer themselves disconnected — we must distinguish the two.
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
                    except Exception as e:
                        logger.warning(f"Could not clean up lingering consult leg {consult_call_sid} "
                                       f"in {consult_conference} for failed transfer of {call_sid}: {e}")

                self.db.cancel_transfer(call_sid)

                # Only tell the client the caller disconnected if the original
                # conference is truly gone.  If it still exists the customer is
                # still waiting (transfer failed because the target left, not the
                # customer) and endCall() must NOT be triggered on the client.
                original_conference = transfer_state.get('conference_name')
                caller_gone = True
                if original_conference:
                    try:
                        live_confs = twilio_list(self.twilio.client.conferences,
                            friendly_name=original_conference, status='in-progress', limit=1)
                        caller_gone = len(live_confs) == 0
                    except Exception as e:
                        logger.warning(f"Could not check if conference {original_conference} is still "
                                       f"live for cancelled transfer of {call_sid} — assuming caller gone: {e}")

                # This branch does NOT touch the customer's hold: the consult-status
                # callback already decided when the transfer failed. It unholds
                # only after a mid-consult disconnect ('completed') or a failed
                # auto-reconnect; a ring failure (no-answer/busy/...) keeps the
                # customer held. Tell the page which, so its Hold/Unhold button
                # matches what the customer is actually hearing.
                resumed = transfer_state.get('transfer_failure_reason') in RESUMED_FAILURE_REASONS
                return {
                    'success': True,
                    'caller_disconnected': caller_gone,
                    'resumed': resumed,
                    'message': (
                        'Transfer cancelled. The original caller had already disconnected.'
                        if caller_gone else
                        'Transfer cancelled.'
                    )
                }

            if transfer_state['transfer_status'] not in ('pending', 'consulting', 'completed'):
                return {'success': False, 'error': f"Cannot cancel transfer in state: {transfer_state['transfer_status']}"}

            original_conference = transfer_state['conference_name']
            consult_call_sid = transfer_state.get('transfer_consult_call_sid')
            consult_conference = transfer_state.get('transfer_consult_conference')
            is_three_way = transfer_state.get('transfer_type') == 'three_way'

            # Auto-reconnect guard (no-op unless the feature is enabled):
            # cancelling kills the consult leg below, which fires the consult-status
            # callback — without this it could be mistaken for a network drop and
            # re-ring the target the agent just cancelled. Mark the leg as a
            # deliberate end, and cancel/clear any reconnect already in flight.
            try:
                recon = self.db.get_reconnect_attempt(original_conference)
                if recon and recon.get('dropped_call_sid'):
                    try:
                        self.twilio.client.calls(recon['dropped_call_sid']).update(status='completed')
                    except Exception as e:
                        logger.warning(f"Could not end in-flight reconnect leg "
                                       f"{recon['dropped_call_sid']} on cancel: {e}")
                if consult_call_sid:
                    self.db.record_leg_intent(consult_call_sid, 'cancel')
                self.db.delete_reconnect_attempt(original_conference)
            except Exception as e:
                logger.warning(f"Could not clear reconnect state on cancel for {original_conference}: {e}")

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

            # With every-device ringing, the target's other phones may still
            # be ringing — the consult leg above is only one of them. A leg
            # that won between the state read and the close is hung up too.
            if ring_group:
                self.cancel_transfer_ring_legs(ring_group['group_key'], except_sid=consult_call_sid)
                winner = (self.db.get_transfer_ring_group(ring_group['group_key']) or {}).get('winner_sid')
                if winner and winner != consult_call_sid:
                    self.db.record_leg_intent(winner, 'cancel')
                    try:
                        self.twilio.client.calls(winner).update(status='completed')
                    except Exception as e:
                        logger.warning(f"Could not hang up transfer leg {winner} on cancel: {e}")
                    self.db.remove_participant(winner)

            # 3-way: the customer was never held. Warm: true once the unhold below succeeds.
            resumed = is_three_way
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
                    resumed = True
                    # Restore endConferenceOnExit=True so the call terminates
                    # cleanly when either party hangs up. warm_transfer_start set
                    # this to False for all participants; the consult-status
                    # callback does the same on failure — mirror it here on cancel.
                    try:
                        for p in twilio_list(self.twilio.client.conferences(conferences[0].sid).participants):
                            try:
                                self.twilio.client.conferences(conferences[0].sid).participants(p.call_sid).update(
                                    end_conference_on_exit=True
                                )
                            except Exception as e:
                                logger.warning(f"Could not restore endConferenceOnExit for participant "
                                               f"{p.call_sid} in {original_conference} after cancel: {e}")
                    except Exception as e:
                        logger.warning(f"Could not restore endConferenceOnExit after cancel: {e}")
            # 3-way cancel: agent and customer are already talking — nothing to undo.

            self.db.cancel_transfer(call_sid)

            self.db.log_activity(
                action="call_transfer_cancelled",
                target=transfer_state.get('transfer_target', 'unknown'),
                details=f"Transfer cancelled for {call_sid}",
                performed_by=cancelled_by
            )

            logger.info(f"Warm transfer cancelled: {call_sid}")
            return {'success': True, 'resumed': resumed}

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
