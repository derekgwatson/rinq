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

    # Check for active transfers — look up by customer SID first, then by
    # conference name (Agent 2 in consult conference won't have a customer)
    customer_sid = result.get('customer_call_sid')
    transfer_state = None
    main_conference = None

    if customer_sid:
        transfer_state = db.get_transfer_state(customer_sid)
        if not transfer_state:
            transfer_state = db.get_transfer_state_log(customer_sid)

    # Agent 2 scenario: we're in the consult conference, no customer visible.
    # Check if our conference IS someone's consult conference.
    if not transfer_state and conf_name:
        transfer_state = _find_transfer_by_consult_conf(db, conf_name)

    if transfer_state and transfer_state.get('transfer_status') in ('pending', 'consulting'):
        main_conference = transfer_state.get('conference_name')
        consult_conf = transfer_state.get('transfer_consult_conference')

        result['transfer'] = {
            'status': transfer_state['transfer_status'],
            'target_name': transfer_state.get('transfer_target_name'),
            'consult_participants': [],
        }

        # Get consult conference participants
        if consult_conf and consult_conf != conf_name:
            consult_parts = db.get_participants(consult_conf)
            for p in consult_parts:
                result['transfer']['consult_participants'].append({
                    'call_sid': p['call_sid'],
                    'name': p['name'] or 'Unknown',
                    'role': p['role'],
                    'hold': False,
                    'muted': False,
                })

        # If we're Agent 2 (in consult conf), include main conference
        # participants so the customer appears in the call panel
        if main_conference and main_conference != conf_name:
            main_parts = db.get_participants(main_conference)
            for p in main_parts:
                if not any(ep['call_sid'] == p['call_sid'] for ep in result['participants']):
                    is_on_hold = (transfer_state['transfer_status'] == 'consulting'
                                  and p['role'] == 'customer')
                    result['participants'].append({
                        'call_sid': p['call_sid'],
                        'name': p['name'] or 'Unknown',
                        'role': p['role'],
                        'hold': is_on_hold,
                        'muted': False,
                    })
                    if p['role'] == 'customer':
                        result['customer_call_sid'] = p['call_sid']

        # Mark customer as on hold in main participant list during consult
        if transfer_state['transfer_status'] == 'consulting':
            for p in result['participants']:
                if p['role'] == 'customer':
                    p['hold'] = True

    # Also find customer_call_sid from child_sid if not set
    if not result.get('customer_call_sid'):
        child_sid = db.get_call_child_sid(agent_call_sid)
        if child_sid:
            result['customer_call_sid'] = child_sid

    return result


def _find_transfer_by_consult_conf(db, conf_name: str) -> dict | None:
    """Find a transfer where conf_name is the consult conference.

    This handles Agent 2's perspective — they're in the consult conference
    and need to find the transfer to see the main conference participants.
    """
    # Check queued_calls first, then call_log
    with db._get_conn() as conn:
        for table in ('queued_calls', 'call_log'):
            row = conn.execute(f"""
                SELECT transfer_status, transfer_type, transfer_target,
                       transfer_target_name, transfer_consult_call_sid,
                       transfer_consult_conference, transferred_by, transferred_at,
                       conference_name
                FROM {table}
                WHERE transfer_consult_conference = ?
                AND transfer_status IN ('pending', 'consulting')
            """, (conf_name,)).fetchone()
            if row:
                return dict(row)
    return None
