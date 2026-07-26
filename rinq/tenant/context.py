"""
Tenant context management.

Provides access to the current tenant and tenant-scoped database
from any point in a request lifecycle.
"""

import os
from flask import g

# Cache Database instances per tenant (they're lightweight - just a db_path)
_tenant_dbs = {}


def get_current_tenant():
    """Get the current tenant dict from request context.

    Returns None for tenant-exempt routes.
    """
    return getattr(g, 'tenant', None)


def get_tenant_db():
    """Get the Database instance for the current tenant.

    In multi-tenant mode, returns a tenant-scoped database.
    Raises RuntimeError if no tenant is in context.
    """
    tenant = get_current_tenant()
    if not tenant:
        raise RuntimeError("No tenant in request context")

    tenant_id = tenant['id']
    if tenant_id not in _tenant_dbs:
        from rinq.database.db import Database
        from rinq.config import config
        db_dir = os.path.join(config.tenants_dir, tenant_id)
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, 'rinq.db')
        _tenant_dbs[tenant_id] = Database(db_path=db_path)

    return _tenant_dbs[tenant_id]


def get_tenant_twilio_config():
    """Get full Twilio config dict for the current tenant."""
    tenant = get_current_tenant()
    if not tenant:
        raise RuntimeError("No tenant in request context")
    from rinq.config import config
    return {
        'twilio_account_sid': tenant.get('twilio_account_sid'),
        'twilio_auth_token': tenant.get('twilio_auth_token'),
        'twilio_api_key': tenant.get('twilio_api_key'),
        'twilio_api_secret': tenant.get('twilio_api_secret'),
        'twilio_twiml_app_sid': tenant.get('twilio_twiml_app_sid'),
        'twilio_default_caller_id': tenant.get('twilio_default_caller_id'),
        'twilio_sip_credential_list_sid': tenant.get('twilio_sip_credential_list_sid'),
        'webhook_base_url': tenant.get('webhook_base_url') or config.webhook_base_url,
    }


def get_twilio_config(key: str, default=None):
    """Get a Twilio config value from the current tenant.

    Args:
        key: The tenant column name, e.g. 'twilio_default_caller_id'
        default: Fallback if the tenant hasn't configured this value
    """
    tenant = get_current_tenant()
    if not tenant:
        raise RuntimeError(f"No tenant in context when reading {key}")
    val = tenant.get(key)
    return val if val is not None else default


def iter_tenant_contexts():
    """Yield every provisioned tenant with `g.tenant` bound for that iteration.

    Cron jobs POST to the unix socket with no session, and `resolve_tenant()`
    only resolves from a session or from phone numbers on /api/voice|sip paths.
    Everything else is left with `g.tenant = None`, so the first `get_db()`
    inside the handler raises RuntimeError and the job dies silently (cron
    redirects output to /dev/null). Endpoints that run system-wide work should
    loop over this instead of relying on the ambient tenant.

    Tenants with no database on disk are skipped rather than provisioned, so
    iterating never creates an empty DB as a side effect. The previous
    `g.tenant` is restored when the loop finishes.
    """
    from rinq.config import config
    from rinq.database.master import get_master_db

    previous = getattr(g, 'tenant', None)
    try:
        for tenant in get_master_db().get_tenants():
            db_path = os.path.join(config.tenants_dir, tenant['id'], 'rinq.db')
            if not os.path.exists(db_path):
                continue
            g.tenant = tenant
            yield tenant
    finally:
        g.tenant = previous
