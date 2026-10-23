"""Watch for failed desk-phone sign-ins and tell somebody when they matter.

Twilio records every failed SIP sign-in (error 32202, bad credentials) in its
debugger and tells nobody. In October 2026 three handsets at Hume and Wagga had
been failing every 15 seconds for months — one of them since March — and the
only way to find them was to go digging in Twilio by hand.

Failures split two ways, by where they come from:

* **From an address we don't know** — someone outside the stores is trying
  logins. A guessed desk-phone login can place calls on our Twilio bill (toll
  fraud), so this emails the tenant admins straight away. One email per address
  per COOLDOWN_HOURS, so a sustained attempt is one email, not hundreds.
* **From one of our stores** — almost always a handset still holding an old or
  deleted login (a leaver, a password that was regenerated). Not urgent, so it
  is tallied and sent as one weekly list, and shown on the Desk Phones page.

"Our stores" is a list of public addresses an admin keeps on the Desk Phones
page. An empty list is treated as "we know of nothing", which errs toward
telling someone.

The Twilio alert payload carries the phone's own address on the store network
inside SipCallId (``1_3361710822@10.0.2.160``) — that is what lets someone walk
up to the right handset, so it is kept and shown.
"""

import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

KNOWN_ADDRESSES_KEY = 'sip_known_addresses'
STATE_KEY = 'sip_auth_watch'

AUTH_FAILURE_CODES = {'32202'}
COOLDOWN_HOURS = 6
DIGEST_DAYS = 7
# Twilio's debugger can surface an alert a little after its timestamp, so each
# run looks back past the previous one and skips alerts it has already seen.
OVERLAP_MINUTES = 10
FIRST_RUN_LOOKBACK_MINUTES = 15
# Each alert needs its own fetch to get the source address. A stuck handset
# makes ~4 attempts a minute, so this comfortably covers a 10-minute run.
MAX_FETCH = 300


def _now():
    return datetime.now(timezone.utc)


def _load_json(db, key, default):
    try:
        raw = db.get_bot_setting(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


def get_known_addresses(db) -> list[dict]:
    """[{'ip': '193.119.49.51', 'label': 'Hume'}, ...]"""
    value = _load_json(db, KNOWN_ADDRESSES_KEY, [])
    return [a for a in value if isinstance(a, dict) and a.get('ip')]


def save_known_addresses(db, addresses: list[dict], updated_by: str) -> list[dict]:
    clean = []
    seen = set()
    for a in addresses or []:
        ip = (a.get('ip') or '').strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        clean.append({'ip': ip, 'label': (a.get('label') or '').strip()})
    db.set_bot_setting(KNOWN_ADDRESSES_KEY, json.dumps(clean), updated_by)
    return clean


def get_state(db) -> dict:
    state = _load_json(db, STATE_KEY, {})
    state.setdefault('seen', {})
    state.setdefault('alerted', {})
    state.setdefault('stale', {})
    return state


def _save_state(db, state: dict) -> None:
    try:
        db.set_bot_setting(STATE_KEY, json.dumps(state), 'tina')
    except Exception as e:
        logger.warning(f"Could not record desk-phone sign-in watch state: {e}")


def _parse_alert(alert) -> dict:
    """Pull who/where out of an alert's request variables."""
    v = dict(urllib.parse.parse_qsl(alert.request_variables or ''))
    sip_from = v.get('From', '')
    # '<sip:carol_hill@watsonblinds.sip.twilio.com:5060>;tag=...'
    username = sip_from.split('@')[0].split('sip:')[-1].lstrip('<') if '@' in sip_from else ''
    call_id = v.get('SipCallId', '')
    lan_ip = call_id.split('@')[-1] if '@' in call_id else ''
    return {
        'ip': v.get('SipSrcIp', ''),
        'username': username,
        'lan_ip': lan_ip,
        'user_agent': v.get('UserAgent', ''),
        'at': alert.date_created.isoformat() if alert.date_created else _now().isoformat(),
    }


def _admin_emails(tenant_id: str) -> list[str]:
    from rinq.services.transcription_alerts import _admin_emails as admins
    return admins(tenant_id)


def _send(tenant, subject: str, body: str) -> list[str]:
    """Email the tenant admins. Returns who it reached."""
    recipients = _admin_emails(tenant['id'])
    if not recipients:
        logger.error(f"{subject} — tenant {tenant['id']} has no admin to tell")
        return []
    from rinq.integrations import get_email_service
    email_service = get_email_service()
    if not email_service:
        logger.error(f"{subject} — no email service is configured to report it")
        return []
    reached = []
    for address in recipients:
        try:
            if email_service.send_email(to=address, subject=subject, text_body=body):
                reached.append(address)
        except Exception as e:
            logger.warning(f"Desk-phone sign-in alert to {address} failed: {e}")
    return reached


def _fmt(iso: str) -> str:
    try:
        import pytz
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(pytz.timezone('Australia/Sydney')).strftime('%-d %b %H:%M')
    except Exception:
        return iso


def _alert_unknown(tenant, ip: str, events: list[dict]) -> list[str]:
    product = tenant.get('product_name') or 'Rinq'
    logins = sorted({e['username'] for e in events if e['username']})
    agents = sorted({e['user_agent'] for e in events if e['user_agent']})
    subject = f"{product}: desk-phone logins are being tried from an unknown address"
    body = (
        f"{len(events)} failed desk-phone sign-in{'s' if len(events) != 1 else ''} "
        f"in the last few minutes came from {ip}, which is not one of the store "
        f"addresses listed on the Desk Phones page.\n\n"
        f"Logins tried: {', '.join(logins) or 'unknown'}\n"
        f"Device: {', '.join(agents) or 'unknown'}\n"
        f"First: {_fmt(min(e['at'] for e in events))}\n"
        f"Last:  {_fmt(max(e['at'] for e in events))}\n\n"
        "Why it matters: a desk-phone login that someone guesses can make calls "
        "— including international ones — that land on our phone bill.\n\n"
        "What to do:\n"
        "- If this is one of our own sites (a store's internet address can "
        "change), add it on the Desk Phones page and these stop.\n"
        "- If it isn't, press Regenerate on the Desk Phones page for each login "
        "listed above, then re-pair that person's phone.\n\n"
        f"You won't hear about this address again for {COOLDOWN_HOURS} hours.\n"
    )
    return _send(tenant, subject, body)


def _send_digest(tenant, stale: dict) -> list[str]:
    product = tenant.get('product_name') or 'Rinq'
    rows = sorted(stale.values(), key=lambda r: (r.get('label') or r['ip'], r['username']))
    lines = []
    for r in rows:
        where = r.get('label') or r['ip']
        phone = f" (phone at {r['lan_ip']})" if r.get('lan_ip') else ''
        lines.append(
            f"- {where}: signed in as '{r['username']}'{phone} — "
            f"{r['count']} failed attempts, last {_fmt(r['last'])}"
        )
    subject = f"{product}: {len(rows)} desk phone{'s' if len(rows) != 1 else ''} can't sign in"
    body = (
        "These desk phones at our stores have been failing to sign in this week. "
        "A phone that can't sign in can't make or take calls — usually it still "
        "holds the login of someone who has left, or a password that was "
        "regenerated.\n\n"
        + "\n".join(lines) +
        "\n\nFor each one: switch the phone off, disable that account on it, or "
        "set it up as a shared phone. The list is also on "
        "the Desk Phones page.\n"
    )
    return _send(tenant, subject, body)


def check(db, tenant, client) -> dict:
    """One pass: read new auth failures from Twilio, alert, tally, digest."""
    now = _now()
    state = get_state(db)
    known = {a['ip']: a.get('label', '') for a in get_known_addresses(db)}

    last = state.get('last_checked')
    try:
        since = datetime.fromisoformat(last) - timedelta(minutes=OVERLAP_MINUTES)
    except Exception:
        since = now - timedelta(minutes=FIRST_RUN_LOOKBACK_MINUTES)

    alerts = client.monitor.v1.alerts.list(start_date=since, log_level='error', limit=2000)
    fresh = [a for a in alerts
             if a.error_code in AUTH_FAILURE_CODES and a.sid not in state['seen']]

    events = []
    for a in fresh[:MAX_FETCH]:
        try:
            events.append(_parse_alert(client.monitor.v1.alerts(a.sid).fetch()))
        except Exception as e:
            logger.warning(f"Could not read Twilio alert {a.sid}: {e}")
            continue
        state['seen'][a.sid] = now.isoformat()

    unknown = {}
    for e in events:
        if e['ip'] in known:
            key = f"{e['ip']}|{e['username']}|{e['lan_ip']}"
            row = state['stale'].setdefault(key, {
                'ip': e['ip'], 'username': e['username'], 'lan_ip': e['lan_ip'],
                'user_agent': e['user_agent'], 'count': 0,
                'first': e['at'], 'last': e['at'],
            })
            row['label'] = known[e['ip']]
            row['count'] += 1
            row['first'] = min(row['first'], e['at'])
            row['last'] = max(row['last'], e['at'])
        else:
            unknown.setdefault(e['ip'] or 'unknown', []).append(e)

    alerted_ips = []
    for ip, ip_events in unknown.items():
        previous = state['alerted'].get(ip)
        try:
            recent = now - datetime.fromisoformat(previous) < timedelta(hours=COOLDOWN_HOURS)
        except Exception:
            recent = False
        if recent:
            continue
        reached = _alert_unknown(tenant, ip, ip_events)
        # Record the attempt even if no email got out — the log has the reason,
        # and retrying every run would turn a mail outage into a mail flood.
        state['alerted'][ip] = now.isoformat()
        alerted_ips.append(ip)
        logger.error(
            f"Desk-phone sign-ins failing from unknown address {ip} "
            f"({len(ip_events)} attempts) — alerted {', '.join(reached) or 'nobody'}"
        )

    # The first run starts the weekly clock rather than mailing on day one.
    digest_sent = False
    if 'digest_sent_at' not in state:
        state['digest_sent_at'] = now.isoformat()
    else:
        try:
            due = now - datetime.fromisoformat(state['digest_sent_at']) >= timedelta(days=DIGEST_DAYS)
        except Exception:
            due = True
        if due:
            week_ago = (now - timedelta(days=DIGEST_DAYS)).isoformat()
            current = {k: r for k, r in state['stale'].items() if r['last'] >= week_ago}
            if current:
                _send_digest(tenant, current)
                digest_sent = True
            state['digest_sent_at'] = now.isoformat()

    # Prune: seen alert ids only need to outlive the overlap window; the
    # failing-phone list keeps a fortnight so the page shows recent history.
    keep_seen = (now - timedelta(minutes=OVERLAP_MINUTES * 3)).isoformat()
    state['seen'] = {sid: t for sid, t in state['seen'].items() if t >= keep_seen}
    fortnight = (now - timedelta(days=14)).isoformat()
    state['stale'] = {k: r for k, r in state['stale'].items() if r['last'] >= fortnight}
    state['alerted'] = {ip: t for ip, t in state['alerted'].items() if t >= fortnight}
    if unknown:
        state['recent_unknown'] = {
            ip: {'count': len(ev), 'last': max(e['at'] for e in ev),
                 'logins': sorted({e['username'] for e in ev if e['username']})}
            for ip, ev in unknown.items()
        } | {ip: v for ip, v in state.get('recent_unknown', {}).items()
             if ip not in unknown and v['last'] >= fortnight}
    state['last_checked'] = now.isoformat()
    _save_state(db, state)

    return {
        'failures_read': len(events),
        'skipped_over_cap': max(0, len(fresh) - MAX_FETCH),
        'unknown_addresses': sorted(unknown),
        'alerted': alerted_ips,
        'digest_sent': digest_sent,
    }


def failing_phones(db) -> dict:
    """What the Desk Phones page shows: stale handsets + unknown sources."""
    state = get_state(db)
    known = {a['ip']: a.get('label', '') for a in get_known_addresses(db)}
    stale = []
    for r in state['stale'].values():
        stale.append({**r, 'label': known.get(r['ip']) or r.get('label') or r['ip']})
    stale.sort(key=lambda r: r['last'], reverse=True)
    unknown = [{'ip': ip, **v} for ip, v in state.get('recent_unknown', {}).items()
               if ip not in known]
    unknown.sort(key=lambda r: r['last'], reverse=True)
    for rows in (stale, unknown):
        for r in rows:
            try:
                r['last_dt'] = datetime.fromisoformat(r['last'])
            except Exception:
                r['last_dt'] = None
    return {'stale': stale, 'unknown': unknown, 'last_checked': state.get('last_checked')}
