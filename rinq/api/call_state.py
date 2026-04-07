"""Call state and participant resolution.

Extracted from routes.py. Handles:
- Building user maps for name resolution (browser identity, SIP)
- Resolving call SIDs to participant names and roles
- Fetching conference state with resolved participants
- The main call state polling logic (_get_call_state_inner)
"""

import logging

from rinq.api.identity import email_to_browser_identity, normalize_staff_identifier
from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service, twilio_list

logger = logging.getLogger(__name__)


def build_user_map(db=None) -> dict:
    """Build a map of Twilio identifiers → friendly names for name resolution.

    Maps both browser client identities (client:user_at_domain_com) and
    SIP usernames (sip:username) to {'name': ..., 'email': ...}.
    """
    if db is None:
        db = get_db()
    all_users = db.get_users()
    user_map = {}
    for u in all_users:
        email = u.get('staff_email', '')
        username = u.get('username', '')
        if email:
            identity = email_to_browser_identity(email)
            friendly = email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
            user_map[f"client:{identity}"] = {'name': friendly, 'email': email}
            if username:
                user_map[f"sip:{username}"] = {'name': friendly, 'email': email}
    return user_map


def resolve_participant(call_sid, *, agent_call_sid=None, caller_email=None,
                        user_map=None, transfer_names=None, db=None,
                        twilio_service=None) -> dict:
    """Resolve a call SID to a participant dict with name and role.

    Tries these strategies in order:
    1. Agent's own call (matches agent_call_sid)
    2. Known transfer consult call (from transfer_names)
    3. Queued call (customer from queue)
    4. Call log agent_email
    5. Call log from/to numbers (customer)
    6. Twilio call details (API fetch)

    Returns:
        {'call_sid': str, 'name': str, 'role': str}
    """
    if db is None:
        db = get_db()
    if twilio_service is None:
        twilio_service = get_twilio_service()
    if user_map is None:
        user_map = {}
    if transfer_names is None:
        transfer_names = {}

    # 1. Agent's own call
    if call_sid == agent_call_sid and caller_email:
        name = caller_email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
        return {'call_sid': call_sid, 'name': name, 'role': 'agent'}

    # 2. Known transfer consult call
    if call_sid in transfer_names:
        return {'call_sid': call_sid, 'name': transfer_names[call_sid], 'role': 'transfer_target'}

    # 3. Queued call (customer)
    queued = db.get_queued_call_by_sid(call_sid)
    if queued:
        return {
            'call_sid': call_sid,
            'name': queued.get('customer_name') or queued.get('caller_number', 'Customer'),
            'role': 'customer',
        }

    # 4. Call log — agent_email
    agent_email_field = db.get_call_log_field(call_sid, 'agent_email')
    if agent_email_field:
        email, friendly = normalize_staff_identifier(agent_email_field)
        if friendly:
            return {'call_sid': call_sid, 'name': friendly, 'role': 'agent'}

    # 5. Call log — from/to numbers (customer)
    try:
        from_num = db.get_call_log_field(call_sid, 'from_number')
        to_num = db.get_call_log_field(call_sid, 'to_number')
        direction = db.get_call_log_field(call_sid, 'direction')
        customer_num = from_num if direction == 'inbound' else to_num
        if customer_num and customer_num.startswith('+'):
            return {'call_sid': call_sid, 'name': customer_num, 'role': 'customer'}
    except Exception as e:
        logger.debug(f"Call log lookup failed for participant {call_sid}: {e}")

    # 6. Twilio call details (API fetch)
    try:
        call = twilio_service.client.calls(call_sid).fetch()
        for identifier in [call.to, call.from_]:
            if identifier and identifier in user_map:
                name_val = user_map[identifier]
                name = name_val['name'] if isinstance(name_val, dict) else name_val
                return {'call_sid': call_sid, 'name': name, 'role': 'agent'}
            if identifier:
                email, friendly = normalize_staff_identifier(identifier)
                if friendly:
                    return {'call_sid': call_sid, 'name': friendly, 'role': 'agent'}
        # Not a known staff member — show the phone number
        for num in [call.to, call.from_]:
            if num and num.startswith('+') and num not in user_map:
                return {'call_sid': call_sid, 'name': num, 'role': 'customer'}
    except Exception as e:
        logger.warning(f"resolve_participant: Twilio fetch failed for {call_sid}: {e}")

    logger.warning(f"resolve_participant: Could not resolve {call_sid}")
    return {'call_sid': call_sid, 'name': 'Unknown', 'role': 'unknown'}


def get_conference_participants(conference_name, *, user_map=None, transfer_names=None,
                                 agent_call_sid=None, caller_email=None,
                                 db=None, twilio_service=None) -> list[dict] | None:
    """Get resolved participants for a conference.

    Returns list of participant dicts with hold/muted state, or None if
    the conference doesn't exist or isn't in progress.
    """
    if db is None:
        db = get_db()
    if twilio_service is None:
        twilio_service = get_twilio_service()

    try:
        confs = twilio_list(twilio_service.client.conferences,
            friendly_name=conference_name, status='in-progress', limit=1
        )
        if not confs:
            return None

        participants = twilio_list(twilio_service.client.conferences(confs[0].sid).participants)
        result = []
        for p in participants:
            info = resolve_participant(
                p.call_sid,
                agent_call_sid=agent_call_sid,
                caller_email=caller_email,
                user_map=user_map,
                transfer_names=transfer_names,
                db=db,
                twilio_service=twilio_service,
            )
            info['hold'] = p.hold
            info['muted'] = p.muted
            result.append(info)
        return result
    except Exception as e:
        logger.debug(f"Conference {conference_name} lookup failed: {e}")
        return None


def get_call_state(agent_call_sid: str, caller_email: str = None) -> dict:
    """Get the current call state for an agent.

    This is the main polling function called by the phone UI. It finds
    which conference the agent is in and resolves all participants.

    Strategies (tried in order):
    1. Check answered queued calls for a conference containing this agent
    2. Check transfer consult conferences
    3. Check the agent's own call_log for a stored conference name

    Returns:
        Dict with in_call, conference, participants, transfer, customer_call_sid
    """
    db = get_db()
    twilio_service = get_twilio_service()
    user_map = build_user_map(db)
    transfer_names = {}  # consult_call_sid -> target_name

    result = {
        'in_call': True,
        'conference': None,
        'participants': [],
        'transfer': None,
        'customer_call_sid': None,
    }

    # Verify the agent's call is still active
    try:
        twilio_service.client.calls(agent_call_sid).fetch()
    except Exception:
        return {"in_call": False}

    resolve_kwargs = dict(
        agent_call_sid=agent_call_sid,
        caller_email=caller_email,
        user_map=user_map,
        transfer_names=transfer_names,
        db=db,
        twilio_service=twilio_service,
    )

    # Strategy 1: answered queued calls
    answered_calls = db.get_recent_answered_queued_calls(limit=10)
    for qc in answered_calls:
        conf_name = qc.get('conference_name')
        if not conf_name:
            continue

        try:
            confs = twilio_list(twilio_service.client.conferences,
                friendly_name=conf_name, status='in-progress', limit=1
            )
            if not confs:
                continue

            participants = twilio_list(twilio_service.client.conferences(confs[0].sid).participants)
            if not any(p.call_sid == agent_call_sid for p in participants):
                continue

            result['conference'] = conf_name
            result['customer_call_sid'] = qc.get('call_sid')

            # Populate transfer names before resolving participants
            transfer_state = db.get_transfer_state(qc['call_sid'])
            if transfer_state and transfer_state.get('transfer_status') in ('pending', 'consulting'):
                consult_sid = transfer_state.get('transfer_consult_call_sid')
                target_name = transfer_state.get('transfer_target_name')
                if consult_sid and target_name:
                    transfer_names[consult_sid] = target_name

                result['transfer'] = _build_transfer_info(
                    transfer_state, transfer_names, resolve_kwargs
                )

            result['participants'] = []
            for p in participants:
                info = resolve_participant(p.call_sid, **resolve_kwargs)
                info['hold'] = p.hold
                info['muted'] = p.muted
                result['participants'].append(info)
            break

        except Exception as e:
            logger.debug(f"Conference lookup for queued call failed: {e}")
            continue

    # Strategy 2: consult conferences (agent in transfer)
    if not result['conference']:
        for qc in answered_calls:
            transfer_state = db.get_transfer_state(qc.get('call_sid', ''))
            if not transfer_state:
                continue
            consult_conf = transfer_state.get('transfer_consult_conference')
            if not consult_conf:
                continue

            try:
                confs = twilio_list(twilio_service.client.conferences,
                    friendly_name=consult_conf, status='in-progress', limit=1
                )
                if not confs:
                    continue

                participants = twilio_list(twilio_service.client.conferences(confs[0].sid).participants)
                if not any(p.call_sid == agent_call_sid for p in participants):
                    continue

                consult_sid = transfer_state.get('transfer_consult_call_sid')
                target_name = transfer_state.get('transfer_target_name')
                if consult_sid and target_name:
                    transfer_names[consult_sid] = target_name

                result['conference'] = qc.get('conference_name')
                result['customer_call_sid'] = qc.get('call_sid')

                # Original conference participants
                orig_parts = get_conference_participants(
                    qc.get('conference_name'), **resolve_kwargs
                )
                result['participants'] = orig_parts or []

                # Consult conference participants
                transfer_info = {
                    'status': transfer_state['transfer_status'],
                    'target_name': target_name,
                    'consult_participants': [],
                }
                for p in participants:
                    info = resolve_participant(p.call_sid, **resolve_kwargs)
                    info['hold'] = p.hold
                    info['muted'] = p.muted
                    transfer_info['consult_participants'].append(info)
                result['transfer'] = transfer_info
                break

            except Exception as e:
                logger.warning(f"Error checking consult conference {consult_conf}: {e}")
                continue

    # Strategy 3: agent's call_log has a stored conference
    if not result['conference']:
        conf_name = db.get_call_conference(agent_call_sid)
        if conf_name:
            try:
                confs = twilio_list(twilio_service.client.conferences,
                    friendly_name=conf_name, status='in-progress', limit=1
                )
                if confs:
                    participants = twilio_list(twilio_service.client.conferences(confs[0].sid).participants)
                    if any(p.call_sid == agent_call_sid for p in participants):
                        result['conference'] = conf_name
                        child_sid = db.get_call_child_sid(agent_call_sid)
                        if child_sid:
                            result['customer_call_sid'] = child_sid

                        # Check transfer state — try child_sid, agent_call_sid,
                        # and original call SID extracted from conference name
                        transfer_key = child_sid or agent_call_sid
                        transfer_state = db.get_transfer_state_log(transfer_key)

                        # For blind transfers: conference is "call_{original_sid}_xfer"
                        # Extract original SID and look up customer info
                        original_call_sid = None
                        if conf_name.startswith('call_') and conf_name.endswith('_xfer'):
                            original_call_sid = conf_name[5:-5]  # strip "call_" and "_xfer"
                            if not transfer_state:
                                transfer_state = db.get_transfer_state_log(original_call_sid)

                        if transfer_state and transfer_state.get('transfer_status') in ('pending', 'consulting'):
                            consult_sid = transfer_state.get('transfer_consult_call_sid')
                            t_name = transfer_state.get('transfer_target_name')
                            if consult_sid and t_name:
                                transfer_names[consult_sid] = t_name

                        # For completed blind transfers, look up customer from
                        # the original call so we don't show the wrong name
                        customer_info = None
                        if original_call_sid:
                            customer_info = _get_customer_from_call_log(original_call_sid, db)

                        result['participants'] = []
                        for p in participants:
                            if p.call_sid == agent_call_sid:
                                info = resolve_participant(p.call_sid, **resolve_kwargs)
                            elif customer_info:
                                info = {'call_sid': p.call_sid, **customer_info}
                            else:
                                info = resolve_participant(p.call_sid, **resolve_kwargs)
                            info['hold'] = p.hold
                            info['muted'] = p.muted
                            result['participants'].append(info)
            except Exception as e:
                logger.warning(f"Failed to fetch conference state for {conf_name}: {e}")

    return result


def _get_customer_from_call_log(call_sid, db) -> dict | None:
    """Look up customer name/number from a call_log entry.

    Returns {'name': ..., 'role': 'customer'} or None.
    """
    try:
        direction = db.get_call_log_field(call_sid, 'direction')
        from_num = db.get_call_log_field(call_sid, 'from_number')
        to_num = db.get_call_log_field(call_sid, 'to_number')
        customer_name = db.get_call_log_field(call_sid, 'customer_name')

        customer_number = to_num if direction == 'outbound' else from_num
        if customer_number:
            return {'name': customer_name or customer_number, 'role': 'customer'}
    except Exception as e:
        logger.debug(f"Could not get customer from call_log {call_sid}: {e}")
    return None


def _build_transfer_info(transfer_state, transfer_names, resolve_kwargs) -> dict:
    """Build transfer info dict from transfer state."""
    target_name = transfer_state.get('transfer_target_name')
    consult_conf = transfer_state.get('transfer_consult_conference')

    transfer_info = {
        'status': transfer_state['transfer_status'],
        'target_name': target_name,
        'consult_participants': [],
    }
    if consult_conf:
        consult_parts = get_conference_participants(consult_conf, **resolve_kwargs)
        if consult_parts:
            transfer_info['consult_participants'] = consult_parts
    return transfer_info
