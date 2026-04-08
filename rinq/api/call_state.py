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
        call_from = getattr(call, '_from', None) or getattr(call, 'from_', None)
        for identifier in [call.to, call_from]:
            if identifier and identifier in user_map:
                name_val = user_map[identifier]
                name = name_val['name'] if isinstance(name_val, dict) else name_val
                return {'call_sid': call_sid, 'name': name, 'role': 'agent'}
            if identifier:
                email, friendly = normalize_staff_identifier(identifier)
                if friendly:
                    return {'call_sid': call_sid, 'name': friendly, 'role': 'agent'}
        # Not a known staff member — show the phone number
        for num in [call.to, call_from]:
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

    Reads from call_participants table (source of truth). Falls back to
    legacy Twilio API resolution for calls that predate the table.

    Returns:
        Dict with in_call, conference, participants, transfer, customer_call_sid
    """
    db = get_db()

    result = {
        'in_call': True,
        'conference': None,
        'participants': [],
        'transfer': None,
        'customer_call_sid': None,
    }

    # Find agent's conference — check call_participants first, then call_log
    agent_participant = db.get_participant_by_sid(agent_call_sid)
    if agent_participant:
        conf_name = agent_participant['conference_name']
    else:
        conf_name = db.get_call_conference(agent_call_sid)

    if not conf_name:
        # No conference found — verify the call is still active
        try:
            twilio_service = get_twilio_service()
            twilio_service.client.calls(agent_call_sid).fetch()
        except Exception:
            return {"in_call": False}
        return result

    result['conference'] = conf_name

    # Get participants from DB
    participants = db.get_participants(conf_name)
    if participants:
        # Fast path: participants recorded in DB
        for p in participants:
            result['participants'].append({
                'call_sid': p['call_sid'],
                'name': p['name'] or 'Unknown',
                'role': p['role'],
                'hold': False,
                'muted': False,
            })
            if p['role'] == 'customer':
                result['customer_call_sid'] = p['call_sid']
    else:
        # Fallback for calls that predate call_participants table:
        # use legacy Twilio API resolution
        result = _get_call_state_legacy(agent_call_sid, caller_email, conf_name, db)

    # Check for active transfers
    customer_sid = result.get('customer_call_sid')
    if customer_sid:
        transfer_state = db.get_transfer_state(customer_sid)
        if not transfer_state:
            transfer_state = db.get_transfer_state_log(customer_sid)
        if transfer_state and transfer_state.get('transfer_status') in ('pending', 'consulting'):
            result['transfer'] = {
                'status': transfer_state['transfer_status'],
                'target_name': transfer_state.get('transfer_target_name'),
                'consult_participants': [],
            }
            # Get consult conference participants
            consult_conf = transfer_state.get('transfer_consult_conference')
            if consult_conf:
                consult_parts = db.get_participants(consult_conf)
                for p in consult_parts:
                    result['transfer']['consult_participants'].append({
                        'call_sid': p['call_sid'],
                        'name': p['name'] or 'Unknown',
                        'role': p['role'],
                        'hold': False,
                        'muted': False,
                    })

    # Also find customer_call_sid from child_sid if not set
    if not result.get('customer_call_sid'):
        child_sid = db.get_call_child_sid(agent_call_sid)
        if child_sid:
            result['customer_call_sid'] = child_sid

    return result


def _get_call_state_legacy(agent_call_sid: str, caller_email: str,
                            conf_name: str, db) -> dict:
    """Fallback call state resolution using Twilio API.

    Used for calls that started before the call_participants table existed.
    """
    twilio_service = get_twilio_service()
    user_map = build_user_map(db)

    result = {
        'in_call': True,
        'conference': conf_name,
        'participants': [],
        'transfer': None,
        'customer_call_sid': None,
    }

    try:
        confs = twilio_list(twilio_service.client.conferences,
            friendly_name=conf_name, status='in-progress', limit=1
        )
        if not confs:
            return result

        participants = twilio_list(twilio_service.client.conferences(confs[0].sid).participants)
        for p in participants:
            info = resolve_participant(
                p.call_sid,
                agent_call_sid=agent_call_sid,
                caller_email=caller_email,
                user_map=user_map,
                db=db,
                twilio_service=twilio_service,
            )
            info['hold'] = p.hold
            info['muted'] = p.muted
            result['participants'].append(info)
            if info.get('role') == 'customer':
                result['customer_call_sid'] = p.call_sid

    except Exception as e:
        logger.warning(f"Legacy call state fetch failed for {conf_name}: {e}")

    # Deduplicate
    if result.get('participants'):
        result['participants'] = _deduplicate_participants(result['participants'])

    return result


def _deduplicate_participants(participants: list[dict]) -> list[dict]:
    """Remove duplicate participants from the list.

    After transfers or extension calls, the same person can appear
    multiple times with different call SIDs (e.g., browser + SIP legs,
    or original + redirected call leg). Deduplicate by name, preferring
    a real name over a phone number.
    """
    seen = {}  # lowercase name -> index in result
    result = []

    for p in participants:
        name = (p.get('name') or 'Unknown').lower()

        if name in seen:
            # Duplicate — keep the one that's not on hold if possible
            prev = result[seen[name]]
            if prev.get('hold') and not p.get('hold'):
                result[seen[name]] = p
            continue

        # For phone numbers, check if we already have a named version of this customer
        if name.startswith('+') and p.get('role') == 'customer':
            has_named = any(
                r.get('role') == 'customer' and not r.get('name', '').startswith('+')
                for r in result
            )
            if has_named:
                continue

        seen[name] = len(result)
        result.append(p)

    return result


def _find_agent_in_conference(conf_name, agent_call_sid, twilio_service) -> list[dict] | None:
    """Check if an agent is in a conference. Returns participant list or None."""
    try:
        confs = twilio_list(twilio_service.client.conferences,
            friendly_name=conf_name, status='in-progress', limit=1
        )
        if not confs:
            return None
        participants = twilio_list(twilio_service.client.conferences(confs[0].sid).participants)
        if any(p.call_sid == agent_call_sid for p in participants):
            return [{'call_sid': p.call_sid, 'hold': p.hold, 'muted': p.muted} for p in participants]
    except Exception as e:
        logger.debug(f"Conference check for {conf_name} failed: {e}")
    return None


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
