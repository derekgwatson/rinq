"""
Tenant provisioning service.

Handles the full onboarding flow:
1. Create Twilio subaccount
2. Create TwiML App
3. Create tenant record
4. Provision tenant database
"""

import os
import logging
from twilio.rest import Client

from rinq.config import config
from rinq.database.master import get_master_db

logger = logging.getLogger(__name__)


def get_master_twilio_client():
    """Get Twilio client using the master account credentials."""
    sid = os.environ.get('TWILIO_ACCOUNT_SID') or config.twilio_account_sid
    token = os.environ.get('TWILIO_AUTH_TOKEN') or config.twilio_auth_token
    if not sid or not token:
        raise ValueError("Master Twilio credentials not configured")
    return Client(sid, token)


def provision_tenant(tenant_id: str, tenant_name: str, admin_email: str,
                     webhook_base_url: str = None) -> dict:
    """Provision a new tenant with Twilio subaccount.

    Args:
        tenant_id: URL-safe slug for the tenant
        tenant_name: Display name
        admin_email: Email of the first admin user
        webhook_base_url: Webhook URL for this tenant (default: rinq.cc)

    Returns:
        Dict with tenant info or error
    """
    master_db = get_master_db()

    # Check if tenant already exists
    if master_db.get_tenant(tenant_id):
        return {'success': False, 'error': f"Tenant '{tenant_id}' already exists"}

    try:
        # 1. Create Twilio subaccount
        master_client = get_master_twilio_client()
        subaccount = master_client.api.accounts.create(
            friendly_name=f"Rinq - {tenant_name}"
        )
        logger.info(f"Created Twilio subaccount: {subaccount.sid} for tenant {tenant_id}")

        # 2. Create a TwiML App in the subaccount
        sub_client = Client(subaccount.sid, subaccount.auth_token)
        base_url = webhook_base_url or config.webhook_base_url or 'https://rinq.cc'

        twiml_app = sub_client.applications.create(
            friendly_name=f"{tenant_name} Phone",
            voice_url=f"{base_url}/api/voice/client-incoming",
            voice_method='POST',
        )
        logger.info(f"Created TwiML App: {twiml_app.sid}")

        # 3. Create API key for access tokens (browser softphone)
        api_key = sub_client.new_keys.create(friendly_name=f"{tenant_name} Browser Key")
        logger.info(f"Created API key: {api_key.sid}")

        # 4. Create SIP credential list and domain
        cred_list = sub_client.sip.credential_lists.create(
            friendly_name=f"{tenant_name} Users"
        )
        logger.info(f"Created SIP credential list: {cred_list.sid}")

        # SIP domain names are globally unique — suffix with account SID fragment
        sip_slug = tenant_id.replace('_', '-')
        domain_prefix = f"{sip_slug}-{subaccount.sid[-6:].lower()}"
        sip_domain = sub_client.sip.domains.create(
            domain_name=f"{domain_prefix}.sip.twilio.com",
            friendly_name=f"{tenant_name} SIP",
            voice_url=f"{base_url}/api/voice/outbound",
            voice_method='POST',
            sip_registration=True,
        )
        logger.info(f"Created SIP domain: {sip_domain.domain_name}")

        # Link credential list for both calls and registrations
        sub_client.sip.domains(sip_domain.sid) \
            .auth.calls.credential_list_mappings.create(credential_list_sid=cred_list.sid)
        sub_client.sip.domains(sip_domain.sid) \
            .auth.registrations.credential_list_mappings.create(credential_list_sid=cred_list.sid)
        logger.info(f"Linked credential list to SIP domain for calls and registrations")

        # 5. Create tenant record
        master_db.create_tenant(
            tenant_id=tenant_id,
            name=tenant_name,
            twilio_account_sid=subaccount.sid,
            twilio_auth_token=subaccount.auth_token,
            twilio_api_key=api_key.sid,
            twilio_api_secret=api_key.secret,
            twilio_twiml_app_sid=twiml_app.sid,
            twilio_sip_credential_list_sid=cred_list.sid,
            twilio_sip_domain=sip_domain.domain_name,
            webhook_base_url=base_url,
        )

        # 5. Provision tenant database
        from rinq.database.db import Database
        db_dir = os.path.join(config.tenants_dir, tenant_id)
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, 'rinq.db')
        Database(db_path=db_path)
        logger.info(f"Tenant database provisioned: {db_path}")

        # 6. Add admin user
        user = master_db.get_or_create_user(email=admin_email, name=tenant_name)
        master_db.add_user_to_tenant(tenant_id, user['id'], role='admin')
        logger.info(f"Added {admin_email} as admin of tenant {tenant_id}")

        return {
            'success': True,
            'tenant_id': tenant_id,
            'tenant_name': tenant_name,
            'twilio_account_sid': subaccount.sid,
            'twiml_app_sid': twiml_app.sid,
        }

    except Exception as e:
        logger.exception(f"Tenant provisioning failed: {e}")
        return {'success': False, 'error': str(e)}
