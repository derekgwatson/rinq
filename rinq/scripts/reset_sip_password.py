"""Temporary script to reset SIP password directly via Twilio API.

Usage: venv/bin/python rinq/scripts/reset_sip_password.py
"""
from rinq.database.master import get_master_db
from twilio.rest import Client

tenant = dict(get_master_db().get_tenant('derek'))
client = Client(tenant['twilio_account_sid'], tenant['twilio_auth_token'])

password = 'Test-Rinq-Sip-99'
cred = client.sip.credential_lists(
    tenant['twilio_sip_credential_list_sid']
).credentials('CRd26135a5a29ce2fa095494d8ff449213').update(password=password)

print(f'Updated {cred.sid} — try Zoiper with password: {password}')
