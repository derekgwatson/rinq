"""Call state polling — reads from call_participants table.

The phone UI polls this every 3 seconds to show who's in the current call.
"""

import logging

from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service

logger = logging.getLogger(__name__)


def get_call_state(agent_call_sid: str, caller_email: str = None) -> dict:
    """Get the current call state for an agent.

    Reads from call_participants table (source of truth).

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

    participants = db.get_participants(conf_name) if conf_name else []
    logger.info(f"Call state poll: sid={agent_call_sid}, conf={conf_name}, participants={len(participants)}")

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

    # Check for active transfers by customer SID
    customer_sid = result.get('customer_call_sid')
    transfer_state = None

    if customer_sid:
        transfer_state = db.get_transfer_state(customer_sid)
        if not transfer_state:
            transfer_state = db.get_transfer_state_log(customer_sid)

    if transfer_state and transfer_state.get('transfer_status') in ('pending', 'consulting'):
        consult_call_sid = transfer_state.get('transfer_consult_call_sid')

        result['transfer'] = {
            'status': transfer_state['transfer_status'],
            'type': transfer_state.get('transfer_type'),
            'target_name': transfer_state.get('transfer_target_name'),
            'consult_participants': [],  # all participants are in the main conference now
        }

        # Flag the transfer target for warm transfers only — the cancel action
        # only makes sense when the customer is on hold and Agent 1 can abort.
        # Blind transfers are already committed; no card action is needed.
        if consult_call_sid and transfer_state.get('transfer_type') == 'warm':
            for p in result['participants']:
                if p['call_sid'] == consult_call_sid:
                    p['isTransferTarget'] = True

        # Mark customer as on hold during a warm transfer
        if transfer_state.get('transfer_type') == 'warm':
            for p in result['participants']:
                if p['role'] == 'customer':
                    p['hold'] = True

    # Also find customer_call_sid from child_sid if not set
    if not result.get('customer_call_sid'):
        child_sid = db.get_call_child_sid(agent_call_sid)
        if child_sid:
            result['customer_call_sid'] = child_sid

    return result


