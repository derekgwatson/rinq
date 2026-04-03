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

# Must set multi-tenant before importing config
os.environ.setdefault('RINQ_MULTI_TENANT', 'true')

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
    sp.add_argument('--role', default='admin', choices=['admin', 'manager', 'user'])
    sp.set_defaults(func=add_user)

    # list-tenants
    sp = subparsers.add_parser('list-tenants', help='List all tenants')
    sp.set_defaults(func=list_tenants)

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
