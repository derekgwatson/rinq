"""SIP domain and URI helpers.

Extracted from routes.py so services (transfer_service) can use these
without importing from the API layer.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service

logger = logging.getLogger(__name__)

# Cache for SIP domain to avoid repeated API calls (per-tenant)
_sip_domain_cache = {}
_sip_cache_lock = threading.Lock()


def get_sip_domain() -> str | None:
    """Get the SIP domain from Twilio (cached for 5 minutes, per-tenant).

    Returns the domain name like 'watson.sip.twilio.com' or None if not configured.
    """
    try:
        from flask import g
        tenant_id = getattr(g, 'tenant', {}).get('id', '_none') if hasattr(g, 'tenant') and g.tenant else '_none'
    except RuntimeError:
        tenant_id = '_none'

    with _sip_cache_lock:
        cached = _sip_domain_cache.get(tenant_id)
        if cached and cached['fetched_at']:
            age = datetime.now(timezone.utc) - cached['fetched_at']
            if age < timedelta(minutes=5):
                return cached['domain']

    # Fetch from Twilio (outside lock — don't hold lock during API calls)
    try:
        service = get_twilio_service()
        domains = service.get_sip_domains()
        if domains:
            domain_name = domains[0]['domain_name']
            with _sip_cache_lock:
                _sip_domain_cache[tenant_id] = {'domain': domain_name, 'fetched_at': datetime.now(timezone.utc)}
            return domain_name
    except Exception as e:
        logger.warning(f"Failed to get SIP domain: {e}")

    return None


def get_sip_uri_for_user(user_email: str, sip_domain: str) -> str | None:
    """Build a SIP URI for a user from their SIP credentials.

    Returns 'sip:username@domain.sip.twilio.com' or None if user has no SIP credentials.
    """
    db = get_db()
    user = db.get_user_by_email(user_email)
    if user and user.get('username'):
        return f"sip:{user['username']}@{sip_domain}"
    return None
