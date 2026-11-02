"""Print LIVE <n>: Twilio calls queued, ringing or in progress, across every
active account (master + tenant subaccounts). Read-only.

Used by the deploy (.github/workflows/deploy.yml) to wait until nobody is on
a call before Tina restarts - a restart drops calls that are connecting or
being transferred (Derek, 2026-10-21 / 2026-11-02). Checks Twilio, not our
tables: call_log and call_participants can carry stale rows.

The workflow pipes this file from origin/main into the venv's python, so it
must stay standalone (no rinq imports). Exits 2 if Twilio can't be asked -
the deploy treats that as "not clear" rather than guessing.
"""
import os
import sys

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv('/var/www/rinq/.env')
try:
    client = Client(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'])
    live = 0
    for account in client.api.accounts.list(status='active'):
        sub = client.api.accounts(account.sid)
        for status in ('queued', 'ringing', 'in-progress'):
            live += len(sub.calls.list(status=status, limit=50))
except Exception as e:  # noqa: BLE001 - any failure means "couldn't check"
    print(f"UNKNOWN {type(e).__name__}: {e}")
    sys.exit(2)
print(f"LIVE {live}")
