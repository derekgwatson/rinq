"""
Tenant resolution middleware.

Resolves the current tenant on each request and stores it in Flask's g object.

- Web routes: resolved from session (logged-in user's current tenant)
- Twilio webhooks: resolved from the To number via master DB lookup
- Auth/system routes: no tenant needed
"""

import logging
from flask import g, request, session
from rinq.database.master import get_master_db

logger = logging.getLogger(__name__)

# Routes that don't need tenant context
TENANT_EXEMPT_PREFIXES = (
    '/login', '/auth/', '/logout', '/health', '/info', '/static/',
)


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

    # If user is logged in but no tenant selected, try domain match first
    user_id = session.get('user_id')
    if user_id:
        # Try matching by domain
        host = request.host.split(':')[0]
        tenant = master_db.get_tenant_by_domain(host)
        if tenant:
            g.tenant = tenant
            session['tenant_id'] = tenant['id']
            return

        # Fall back to first tenant
        tenants = master_db.get_user_tenants(user_id)
        if tenants:
            g.tenant = tenants[0]
            session['tenant_id'] = tenants[0]['id']
            return

    # Twilio webhooks (no session): resolve from the called number
    if path.startswith('/api/voice/'):
        called = request.form.get('To') or request.form.get('Called') or request.args.get('called', '')
        if called:
            called = called.strip()
            if not called.startswith('+'):
                called = '+' + called
            tenant = master_db.get_tenant_for_number(called)
            if tenant:
                g.tenant = tenant
                return

        # Fallback: tenant_id in URL args
        tid = request.args.get('tenant_id')
        if tid:
            tenant = master_db.get_tenant(tid)
            if tenant:
                g.tenant = tenant
                return

        logger.warning(f"Could not resolve tenant for webhook: {path}")

    g.tenant = None
