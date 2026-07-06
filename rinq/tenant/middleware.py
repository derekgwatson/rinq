"""
Tenant resolution middleware.

Resolves the current tenant on each request and stores it in Flask's g object.

- Web routes: resolved from session (logged-in user's current tenant)
- Twilio webhooks: resolved from the To number via master DB lookup
- Auth/system routes: no tenant needed
"""

import logging
import os
from flask import g, request, session
from rinq.database.master import get_master_db

logger = logging.getLogger(__name__)

# Routes that don't need tenant context
TENANT_EXEMPT_PREFIXES = (
    '/login', '/auth/', '/logout', '/health', '/info', '/static/',
)

# Media-TwiML endpoints exempt from signature validation. Twilio fetches
# these as conference wait/hold URLs with no call parameters, so tenant
# resolution yields None and there is no auth token to validate against —
# under enforce mode they would 403 and hold audio would go silent. Both
# return a static <Play> and change no state, so unsigned access is harmless.
SIGNATURE_EXEMPT_PATHS = (
    '/api/voice/hold-music',
    '/api/voice/ringback',
)


def _check_twilio_signature(tenant):
    """Validate X-Twilio-Signature on unauthenticated webhook requests.

    /api/voice/* and /api/sip/* accept unauthenticated POSTs (Twilio can't
    log in), and tenant resolution uses attacker-controllable form fields —
    so without this check anyone on the internet can execute call-flow
    logic (including outbound dials) under a tenant's context.

    Only applies to requests that look like they came from Twilio: no
    session, no API key, arrived via the nginx proxy. Browser-softphone
    calls to the same paths carry a session cookie; cron hits the unix
    socket (no X-Forwarded-For); both skip this check.

    Rollout is controlled by RINQ_TWILIO_SIGNATURE_MODE:
      - 'log' (default): validate and log failures, but allow the request.
        Watch the logs for false negatives (URL reconstruction behind the
        proxy), then switch to enforce.
      - 'enforce': reject invalid/missing signatures with 403.
      - 'off': skip entirely.

    Returns True if the request may proceed.
    """
    mode = os.environ.get('RINQ_TWILIO_SIGNATURE_MODE', 'log').lower()
    if mode == 'off':
        return True
    if request.path in SIGNATURE_EXEMPT_PATHS:
        return True
    if session.get('user_id') or request.headers.get('X-API-Key'):
        return True  # app caller, not Twilio
    if not request.headers.get('X-Forwarded-For'):
        return True  # unix-socket cron / local process

    signature = request.headers.get('X-Twilio-Signature', '')
    auth_token = (tenant or {}).get('twilio_auth_token')
    valid = False
    if signature and auth_token:
        try:
            from twilio.request_validator import RequestValidator
            valid = RequestValidator(auth_token).validate(
                request.url, request.form, signature)
        except Exception as e:
            logger.warning(f"Twilio signature validation errored for {request.path}: {e}")
    if valid:
        return True

    detail = 'missing' if not signature else ('no tenant auth token' if not auth_token else 'invalid')
    msg = (f"Twilio signature {detail} for {request.method} {request.path} "
           f"(tenant={(tenant or {}).get('id')}, from={request.remote_addr})")
    if mode == 'enforce':
        logger.warning(msg + " — rejected")
        return False
    logger.warning(msg + " — allowed (RINQ_TWILIO_SIGNATURE_MODE=log)")
    return True


def resolve_tenant():
    """Flask before_request handler to resolve current tenant."""
    path = request.path

    # Skip tenant resolution for auth and system routes
    if any(path.startswith(prefix) for prefix in TENANT_EXEMPT_PREFIXES):
        g.tenant = None
        return

    master_db = get_master_db()

    # First, try resolving from session
    tenant_id = session.get('tenant_id')
    if tenant_id:
        tenant = master_db.get_tenant(tenant_id)
        if tenant:
            g.tenant = tenant
            return
        # Tenant was deleted/deactivated — drop the stale id so we don't
        # repeat the failed lookup on every request
        session.pop('tenant_id', None)

    # If user is logged in but no tenant selected, try domain match first
    user_id = session.get('user_id')
    if user_id:
        # Try matching by domain — but only adopt that tenant if the user is
        # actually a member of it. Without the membership check, a tenant-A
        # user browsing tenant B's domain would be handed tenant B's context
        # (and the bogus tenant_id persisted into their session).
        host = request.host.split(':')[0]
        tenant = master_db.get_tenant_by_domain(host)
        if tenant:
            if master_db.get_user_tenant_data(user_id, tenant['id']):
                g.tenant = tenant
                session['tenant_id'] = tenant['id']
                return
            logger.warning(
                f"User {session.get('user_email', user_id)} is not a member of "
                f"tenant '{tenant['id']}' (domain {host}) — not adopting it"
            )

        # Fall back to the first tenant the user belongs to
        tenants = master_db.get_user_tenants(user_id)
        if tenants:
            g.tenant = tenants[0]
            session['tenant_id'] = tenants[0]['id']
            return

    # Twilio webhooks (no session): resolve from phone numbers in the request
    if path.startswith('/api/voice/') or path.startswith('/api/sip/'):
        g.tenant = _resolve_webhook_tenant(master_db, path)
        if not _check_twilio_signature(g.tenant):
            from flask import Response
            return Response('Forbidden', status=403)
        return

    g.tenant = None


def _resolve_webhook_tenant(master_db, path):
    """Resolve the tenant for a Twilio webhook request, or None."""
    # Try all number fields — To, Called, From — any might be a registered number
    for field in ('To', 'Called', 'From', 'CallerId'):
        value = request.form.get(field) or request.args.get(field.lower(), '')
        if not value:
            continue
        value = value.strip()
        # SIP URI (e.g. sip:derekgg@derek-c1012a.sip.twilio.com) — resolve by SIP domain
        if '@' in value:
            sip_domain = value.split('@', 1)[1].split(';')[0]  # strip ;transport=UDP etc
            tenant = master_db.get_tenant_by_sip_domain(sip_domain)
            if tenant:
                return tenant
            continue
        if not value.startswith('+'):
            value = '+' + value
        tenant = master_db.get_tenant_for_number(value)
        if tenant:
            return tenant

    # Fallback: resolve from Twilio AccountSid (present on all webhooks)
    account_sid = request.form.get('AccountSid') or request.args.get('AccountSid')
    if account_sid:
        tenant = master_db.get_tenant_by_account_sid(account_sid)
        if tenant:
            return tenant

    # Last resort: if only one tenant has Twilio configured, use it.
    # (Signature validation still runs against this tenant's auth token,
    # so once enforce mode is on this can't be used to forge requests.)
    tenants = [t for t in master_db.get_tenants() if t.get('twilio_account_sid')]
    if len(tenants) == 1:
        logger.warning(f"Webhook tenant resolved via single-tenant fallback: {path}")
        return tenants[0]

    logger.warning(f"Could not resolve tenant for webhook: {path}")
    return None
