"""Live calls — what is happening on the phones right now, for /live.

Twilio is the source of truth for WHAT is live (its in-progress / ringing
call list); call_log supplies WHO (staff member, customer, queue). call_log
alone is not enough: it carries stale rows whose hang-up was never seen.

Twilio has one call per LEG, so a single conversation shows up as two or
more live calls (customer leg + staff leg, plus ring legs while phones ring).
This module folds legs into the things a person would count:

- talking:  a staff member is connected to a customer
- waiting:  a caller is in a queue, nobody has picked up
- starting: a caller is hearing the greeting, a menu, or a phone is ringing
            for them (extension, press-1 callback, direct line)

`twilio_live` (the raw leg count) is kept separately: it is the number that
says whether a deploy would cut anyone off, and it must count every leg,
including ones this page can't explain.
"""

import logging
import time
from datetime import datetime, timezone

from rinq.database.db import _parse_dt

logger = logging.getLogger(__name__)

# Several managers polling every few seconds must not each hit Twilio.
_CACHE_TTL = 4  # seconds
_cache = {}  # {tenant_id: (fetched_at, snapshot)}


def _seconds_since(ts) -> int | None:
    if not ts:
        return None
    try:
        return max(0, int((datetime.now(timezone.utc) - _parse_dt(str(ts).replace('Z', ''))).total_seconds()))
    except (ValueError, TypeError):
        return None


def _display_number(number: str) -> str:
    from rinq.services.phone import to_local
    n = (number or '').strip()
    if n.startswith('sip:'):
        n = n[4:].split('@')[0]
    if n.startswith('client:'):
        return ''
    return to_local(n) if n.startswith('+61') else n


def _staff_name(email: str, db, cache: dict) -> str:
    if not email:
        return ''
    if email.startswith('sip:'):
        return 'Desk phone ' + email[4:].split('@')[0]
    if email not in cache:
        user = db.get_user_by_email(email)
        cache[email] = (user.get('friendly_name') if user else None) or \
            email.split('@')[0].replace('.', ' ').title()
    return cache[email]


# Customer names by number, so a call that's been running for ten minutes
# isn't looked up in Clara on every refresh. {e164: (looked_up_at, name)}
_NAME_TTL = 600
_names = {}


def _customer_name(number: str, known: str | None) -> str:
    """Best name for a customer number: what the call already knows, else a
    (cached) Clara lookup — same lookup the phone screen uses. '' if unknown."""
    if known:
        return known
    if not number or not number.startswith('+'):
        return ''
    hit = _names.get(number)
    if hit and time.time() - hit[0] < _NAME_TTL:
        return hit[1]
    name = ''
    try:
        from rinq.services.caller_enrichment import get_enrichment_service
        name = (get_enrichment_service().enrich_caller(number) or {}).get('customer_name') or ''
    except Exception as e:
        logger.debug(f"Live calls: name lookup failed for {number}: {e}")
    _names[number] = (time.time(), name)
    return name


def _live_twilio_legs(client) -> list:
    """Every queued/ringing/in-progress leg on this tenant's account."""
    from rinq.services.twilio_service import twilio_list
    legs = []
    for status in ('queued', 'ringing', 'in-progress'):
        legs.extend(twilio_list(client.calls, status=status, limit=200))
    return legs


def _held_conferences(client) -> set[str]:
    """Friendly names of live conferences where someone is on hold."""
    from rinq.services.twilio_service import twilio_list
    held = set()
    for conf in twilio_list(client.conferences, status='in-progress', limit=50):
        name = conf.friendly_name or ''
        if name.startswith('hold_'):
            # Legacy hold rooms: the customer sits alone in hold_room_<sid>
            held.add(name)
            continue
        try:
            for p in twilio_list(client.conferences(conf.sid).participants, limit=20):
                if getattr(p, 'hold', False):
                    held.add(name)
                    break
        except Exception as e:
            logger.debug(f"Live calls: couldn't read participants of {name}: {e}")
    return held


def _call_log_rows(db, sids: list[str]) -> dict:
    if not sids:
        return {}
    with db._get_conn() as conn:
        placeholders = ','.join('?' * len(sids))
        rows = conn.execute(f"""
            SELECT cl.call_sid, cl.direction, cl.call_type, cl.from_number, cl.to_number,
                   cl.agent_email, cl.status, cl.started_at, cl.answered_at,
                   cl.customer_name, cl.queue_name, cl.conference_name,
                   u.staff_email AS resolved_email
            FROM call_log cl
            LEFT JOIN users u ON cl.agent_email = 'sip:' || u.username
            WHERE cl.call_sid IN ({placeholders})
        """, sids).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        if d.pop('resolved_email', None):
            d['agent_email'] = r['resolved_email']
        agent = d.get('agent_email') or ''
        if agent.startswith('sip:'):
            # A raw SIP address (e.g. a shared desk phone): show whoever is
            # logged in to it rather than the handset's name
            username = agent[4:].split('@')[0]
            d['agent_email'] = db.get_staff_email_for_sip_username(username) or agent
        out[d['call_sid']] = d
    return out


def _queued_names(db, sids: list[str]) -> dict:
    """queued_calls rows (any status) for these live call SIDs only."""
    if not sids:
        return {}
    with db._get_conn() as conn:
        rows = conn.execute(
            f"SELECT call_sid, customer_name FROM queued_calls WHERE call_sid IN ({','.join('?' * len(sids))})",
            sids).fetchall()
    return {r['call_sid']: dict(r) for r in rows}


def build_snapshot(db, client) -> dict:
    """Fold Twilio's live legs into talking / waiting / starting."""
    legs = _live_twilio_legs(client)
    leg_status = {leg.sid: leg.status for leg in legs}
    rows = _call_log_rows(db, list(leg_status))

    try:
        held = _held_conferences(client) if legs else set()
    except Exception as e:
        logger.warning(f"Live calls: hold check failed: {e}")
        held = set()

    waiting_sids = {q['call_sid']: q for q in db.get_queued_calls(status='waiting')}
    answered_queue = _queued_names(db, list(leg_status))
    names = {}
    talking, starting, waiting = [], [], []

    for sid, row in rows.items():
        direction = row.get('direction')
        agent = (row.get('agent_email') or '').strip()
        if row.get('call_type') == 'transfer':
            continue  # credit row for a transfer — the main row already shows the call
        if direction == 'outbound' and not agent:
            continue  # the customer's leg of a staff member's outbound call

        conf = row.get('conference_name') or ''
        on_hold = conf in held or f"hold_room_{sid}" in held or f"hold_{sid}" in held

        if direction == 'outbound':
            customer_number = row.get('to_number')
            answered = row.get('status') == 'answered' or bool(row.get('answered_at'))
            talking.append({
                'kind': 'outbound',
                'staff': _staff_name(agent, db, names),
                'customer': _customer_name(customer_number, row.get('customer_name')) or _display_number(customer_number),
                'number': _display_number(customer_number),
                'queue': None,
                'state': 'on hold' if on_hold else ('talking' if answered else 'dialling'),
                'seconds': _seconds_since(row.get('answered_at') or row.get('started_at')),
            })
            continue

        # Inbound (customer's leg)
        customer_number = row.get('from_number')
        if '@' in agent and row.get('status') in ('answered', 'transferred'):
            # The queue record carries the name the waiting list showed
            queued_name = (answered_queue.get(sid) or {}).get('customer_name')
            talking.append({
                'kind': 'inbound',
                'staff': _staff_name(agent, db, names),
                'customer': _customer_name(customer_number, row.get('customer_name') or queued_name)
                            or _display_number(customer_number),
                'number': _display_number(customer_number),
                'queue': row.get('queue_name'),
                'state': 'on hold' if on_hold else 'talking',
                'seconds': _seconds_since(row.get('answered_at') or row.get('started_at')),
            })
        elif sid in waiting_sids:
            q = waiting_sids[sid]
            waiting.append({
                'customer': q.get('customer_name') or row.get('customer_name') or _display_number(customer_number),
                'number': _display_number(customer_number),
                'queue': q.get('queue_name') or row.get('queue_name'),
                'queue_id': q.get('queue_id'),
                'priority': q.get('priority'),
                'seconds': _seconds_since(q.get('enqueued_at')),
            })
        else:
            starting.append({
                'customer': row.get('customer_name') or _display_number(customer_number),
                'number': _display_number(customer_number),
                'called': _display_number(row.get('to_number')),
                'seconds': _seconds_since(row.get('started_at')),
            })

    talking.sort(key=lambda c: -(c['seconds'] or 0))
    waiting.sort(key=lambda c: -(c['seconds'] or 0))
    starting.sort(key=lambda c: -(c['seconds'] or 0))

    return {
        'talking': talking,
        'waiting': waiting,
        'starting': starting,
        'queues': _queue_summary(db, waiting),
        'twilio_live': len(legs),
        'generated_at': datetime.now(timezone.utc).isoformat(),
    }


def _queue_summary(db, waiting: list) -> list:
    """Every active queue with its waiting count, longest wait and pause state."""
    from rinq.api.routes import _is_queue_paused
    by_queue = {}
    for w in waiting:
        by_queue.setdefault(w.get('queue_id'), []).append(w)

    out = []
    for q in db.get_queues():
        if not q.get('is_active', 1):
            continue
        ws = by_queue.get(q['id'], [])
        out.append({
            'name': q.get('name'),
            'waiting': len(ws),
            'longest_seconds': max((w['seconds'] or 0) for w in ws) if ws else 0,
            'paused': _is_queue_paused(q),
        })
    out.sort(key=lambda q: (-q['waiting'], q['name'] or ''))
    return out


def get_snapshot(db, tenant_id: str) -> dict:
    """build_snapshot with a short per-tenant cache. Never raises."""
    now = time.time()
    cached = _cache.get(tenant_id)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        from rinq.services.twilio_service import get_twilio_service
        snap = build_snapshot(db, get_twilio_service().client)
        snap['error'] = None
    except Exception as e:
        logger.exception(f"Live calls snapshot failed: {e}")
        snap = {'talking': [], 'waiting': [], 'starting': [], 'queues': [],
                'twilio_live': None, 'generated_at': datetime.now(timezone.utc).isoformat(),
                'error': "Couldn't reach Twilio — this isn't the live picture."}
    _cache[tenant_id] = (now, snap)
    return snap
