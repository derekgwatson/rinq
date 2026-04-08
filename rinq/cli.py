"""
Tina/Rinq CLI for tenant management.

Usage:
    python -m tina.cli setup-tenant --id watson --name "Watson Blinds" --email derek@watsonblinds.com.au
    python -m tina.cli add-user --tenant watson --email user@example.com
    python -m tina.cli list-tenants
    python -m tina.cli register-number --tenant watson --number +61261234567
"""

import argparse
import os
import sys
from pathlib import Path

# Add parent directory for shared imports
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from rinq.config import config
from rinq.database.master import get_master_db


def setup_tenant(args):
    """Create a new tenant and provision their database."""
    master_db = get_master_db()

    existing = master_db.get_tenant(args.id)
    if existing:
        print(f"Tenant '{args.id}' already exists: {existing['name']}")
        return

    # Build kwargs from args and env vars
    kwargs = {
        'twilio_account_sid': args.twilio_sid or os.environ.get('TWILIO_ACCOUNT_SID'),
        'twilio_auth_token': args.twilio_token or os.environ.get('TWILIO_AUTH_TOKEN'),
        'twilio_api_key': os.environ.get('TWILIO_API_KEY'),
        'twilio_api_secret': os.environ.get('TWILIO_API_SECRET'),
        'twilio_twiml_app_sid': os.environ.get('TWILIO_TWIML_APP_SID'),
        'twilio_default_caller_id': os.environ.get('TWILIO_DEFAULT_CALLER_ID'),
        'twilio_sip_credential_list_sid': os.environ.get('TWILIO_SIP_CREDENTIAL_LIST_SID'),
        'webhook_base_url': args.webhook_url,
        'integration_provider': args.integrations or 'none',
    }

    master_db.create_tenant(tenant_id=args.id, name=args.name, **kwargs)
    print(f"Created tenant: {args.id} ({args.name})")

    # Provision tenant database (triggers migrations)
    from rinq.database.db import Database
    db_dir = os.path.join(config.tenants_dir, args.id)
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, 'rinq.db')
    Database(db_path=db_path)
    print(f"Tenant database provisioned at: {db_path}")

    # Add admin user if email provided
    if args.email:
        user = master_db.get_or_create_user(email=args.email, name=args.name)
        master_db.add_user_to_tenant(args.id, user['id'], role='admin')
        print(f"Added {args.email} as admin of tenant {args.id}")


def add_user(args):
    """Add a user to a tenant."""
    master_db = get_master_db()

    tenant = master_db.get_tenant(args.tenant)
    if not tenant:
        print(f"Tenant '{args.tenant}' not found")
        sys.exit(1)

    user = master_db.get_or_create_user(email=args.email, name=args.name or args.email)
    master_db.add_user_to_tenant(args.tenant, user['id'], role=args.role)
    print(f"Added {args.email} as {args.role} of tenant {args.tenant}")


def list_tenants(args):
    """List all tenants."""
    master_db = get_master_db()
    tenants = master_db.get_tenants()
    if not tenants:
        print("No tenants configured.")
        return
    for t in tenants:
        users = master_db.get_tenant_users(t['id'])
        user_count = len(users)
        print(f"  {t['id']}: {t['name']} ({user_count} users, integrations: {t.get('integration_provider', 'none')})")


def setup_sip(args):
    """Set up SIP for a tenant that was provisioned before SIP auto-setup."""
    master_db = get_master_db()
    tenant = master_db.get_tenant(args.tenant)
    if not tenant:
        print(f"Tenant '{args.tenant}' not found")
        sys.exit(1)

    if tenant.get('twilio_sip_credential_list_sid'):
        print(f"Tenant '{args.tenant}' already has SIP configured: {tenant['twilio_sip_credential_list_sid']}")
        if not args.force:
            sys.exit(0)

    sid = tenant.get('twilio_account_sid')
    token = tenant.get('twilio_auth_token')
    if not sid or not token:
        print(f"Tenant '{args.tenant}' has no Twilio credentials")
        sys.exit(1)

    from twilio.rest import Client
    from twilio.base.exceptions import TwilioException, TwilioRestException
    client = Client(sid, token)
    base_url = tenant.get('webhook_base_url') or config.webhook_base_url

    # Create credential list (or reuse existing)
    from rinq.services.twilio_service import twilio_list
    existing_lists = twilio_list(client.sip.credential_lists)
    if existing_lists:
        cred_list = existing_lists[0]
        print(f"Using existing credential list: {cred_list.sid}")
    else:
        cred_list = client.sip.credential_lists.create(
            friendly_name=f"{tenant['name']} Users"
        )
        print(f"Created credential list: {cred_list.sid}")

    # Fetch existing SIP domains (SDK .list() is unreliable, use raw REST)
    import requests as req
    domain_resp = req.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/SIP/Domains.json",
        auth=(sid, token)
    )
    domain_resp.raise_for_status()
    existing_domains = _find_list(domain_resp.json())

    voice_url = f"{base_url}/api/voice/outbound"
    domain = None
    domain_name = None

    if existing_domains:
        domain_data = existing_domains[0]
        domain = client.sip.domains(domain_data['sid'])
        domain_name = domain_data['domain_name']
        if domain_data.get('voice_url') != voice_url:
            domain.update(voice_url=voice_url, voice_method='POST')
            print(f"Updated voice URL on {domain_name}")
        print(f"Using existing SIP domain: {domain_name}")
    else:
        sip_slug = args.tenant.replace('_', '-')
        # SIP domain names are globally unique across all Twilio accounts,
        # so suffix with the subaccount SID fragment to avoid collisions
        domain_prefix = f"{sip_slug}-{sid[-6:].lower()}"
        domain = client.sip.domains.create(
            domain_name=f"{domain_prefix}.sip.twilio.com",
            friendly_name=f"{tenant['name']} SIP",
            voice_url=voice_url,
            voice_method='POST',
            sip_registration=True,
        )
        domain_name = domain.domain_name
        print(f"Created SIP domain: {domain_name}")

    # Link credential list for calls and registrations
    try:
        domain.auth.calls.credential_list_mappings.create(credential_list_sid=cred_list.sid)
        print(f"Linked credential list for calls")
    except (TwilioRestException, TwilioException):
        print(f"Credential list already linked for calls")
    try:
        domain.auth.registrations.credential_list_mappings.create(credential_list_sid=cred_list.sid)
        print(f"Linked credential list for registrations")
    except (TwilioRestException, TwilioException):
        print(f"Credential list already linked for registrations")

    # Update tenant record
    updates = {'twilio_sip_credential_list_sid': cred_list.sid}
    if domain_name:
        updates['twilio_sip_domain'] = domain_name
    master_db.update_tenant(args.tenant, **updates)
    print(f"Done — SIP configured for tenant '{args.tenant}'")


def _find_list(resp):
    """Find the list payload in a Twilio REST response (key varies by endpoint)."""
    for key, val in resp.items():
        if isinstance(val, list):
            return val
    return []


def check_sip(args):
    """Diagnose SIP registration issues for a tenant."""
    master_db = get_master_db()
    tenant = master_db.get_tenant(args.tenant)
    if not tenant:
        print(f"Tenant '{args.tenant}' not found")
        sys.exit(1)

    sid = tenant.get('twilio_account_sid')
    token = tenant.get('twilio_auth_token')
    cred_list_sid = tenant.get('twilio_sip_credential_list_sid')
    sip_domain = tenant.get('twilio_sip_domain')

    print(f"Tenant: {args.tenant}")
    print(f"Account SID: {sid}")
    print(f"Credential List SID: {cred_list_sid or 'NOT SET'}")
    print(f"SIP Domain (DB): {sip_domain or 'NOT SET'}")
    print()

    if not sid or not token:
        print("ERROR: No Twilio credentials")
        sys.exit(1)

    from twilio.rest import Client
    from twilio.base.exceptions import TwilioException, TwilioRestException
    client = Client(sid, token)

    # Check domains
    print("--- SIP Domains ---")
    import requests as req
    resp = req.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/SIP/Domains.json",
        auth=(sid, token)
    ).json()
    domains = resp.get('domains', [])
    if not domains:
        print("  NONE — no SIP domains found")
    for d in domains:
        print(f"  {d['domain_name']} (SID: {d['sid']})")

        # Check credential list mappings for calls
        calls_resp = req.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/SIP/Domains/{d['sid']}/Auth/Calls/CredentialListMappings.json",
            auth=(sid, token)
        ).json()
        call_mappings = _find_list(calls_resp)
        print(f"    Calls auth: {len(call_mappings)} credential list(s)")
        for m in call_mappings:
            match = ' ← MATCH' if m.get('sid') == cred_list_sid else ''
            print(f"      {m.get('sid')} ({m.get('friendly_name')}){match}")

        # Check credential list mappings for registrations
        reg_resp = req.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/SIP/Domains/{d['sid']}/Auth/Registrations/CredentialListMappings.json",
            auth=(sid, token)
        ).json()
        reg_mappings = _find_list(reg_resp)
        print(f"    Registration auth: {len(reg_mappings)} credential list(s)")
        for m in reg_mappings:
            match = ' ← MATCH' if m.get('sid') == cred_list_sid else ''
            print(f"      {m.get('sid')} ({m.get('friendly_name')}){match}")

    # Check credentials in list
    if cred_list_sid:
        print(f"\n--- Credentials in {cred_list_sid} ---")
        try:
            creds_resp = req.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/SIP/CredentialLists/{cred_list_sid}/Credentials.json",
                auth=(sid, token)
            ).json()
            creds = _find_list(creds_resp)
            if not creds:
                print("  NONE — no credentials found")
            else:
                for c in creds:
                    print(f"  {c.get('username')} (SID: {c.get('sid')})")
        except Exception as e:
            print(f"  ERROR fetching credentials: {e}")


def register_number(args):
    """Register a phone number to a tenant."""
    master_db = get_master_db()

    tenant = master_db.get_tenant(args.tenant)
    if not tenant:
        print(f"Tenant '{args.tenant}' not found")
        sys.exit(1)

    number = args.number.strip()
    if not number.startswith('+'):
        number = '+' + number

    master_db.register_phone_number(number, args.tenant)
    print(f"Registered {number} -> tenant {args.tenant}")


def main():
    parser = argparse.ArgumentParser(description='Tina/Rinq Tenant Management CLI')
    subparsers = parser.add_subparsers(dest='command')

    # setup-tenant
    sp = subparsers.add_parser('setup-tenant', help='Create a new tenant')
    sp.add_argument('--id', required=True, help='Tenant ID (slug)')
    sp.add_argument('--name', required=True, help='Tenant display name')
    sp.add_argument('--email', help='Admin user email')
    sp.add_argument('--twilio-sid', help='Twilio Account SID (or use TWILIO_ACCOUNT_SID env)')
    sp.add_argument('--twilio-token', help='Twilio Auth Token (or use TWILIO_AUTH_TOKEN env)')
    sp.add_argument('--webhook-url', help='Webhook base URL for this tenant')
    sp.add_argument('--integrations', help='Integration provider (watson, none)', default='none')
    sp.set_defaults(func=setup_tenant)

    # add-user
    sp = subparsers.add_parser('add-user', help='Add user to tenant')
    sp.add_argument('--tenant', required=True, help='Tenant ID')
    sp.add_argument('--email', required=True, help='User email')
    sp.add_argument('--name', help='User display name')
    sp.add_argument('--role', default='admin')
    sp.set_defaults(func=add_user)

    # list-tenants
    sp = subparsers.add_parser('list-tenants', help='List all tenants')
    sp.set_defaults(func=list_tenants)

    # setup-sip
    sp = subparsers.add_parser('setup-sip', help='Set up SIP for an existing tenant')
    sp.add_argument('--tenant', required=True, help='Tenant ID')
    sp.add_argument('--force', action='store_true', help='Recreate even if already configured')
    sp.set_defaults(func=setup_sip)

    # check-sip
    sp = subparsers.add_parser('check-sip', help='Diagnose SIP registration issues')
    sp.add_argument('--tenant', required=True, help='Tenant ID')
    sp.set_defaults(func=check_sip)

    # register-number
    sp = subparsers.add_parser('register-number', help='Register phone number to tenant')
    sp.add_argument('--tenant', required=True, help='Tenant ID')
    sp.add_argument('--number', required=True, help='Phone number (E.164 format)')
    sp.set_defaults(func=register_number)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
