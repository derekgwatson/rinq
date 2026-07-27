"""Track whether a user's SIP device actually rings, not just that we tried it.

The existing `sip_registered_at` column is stamped when we *initiate* a ring or
merely add a <Sip> target to TwiML — it means "we tried", not "a device
answered". That makes it useless (worse: actively misleading) for spotting a
desk phone that isn't logged in: the user with no registered handset gets the
freshest timestamp of anyone, because we keep trying to ring them.

Twilio exposes no API for live SIP registrations, so the only available proof
is the ring outcome. A leg that reaches `ringing` means the handset sent back a
180 — it is registered and audibly ringing. A leg that goes from `initiated`
straight to a terminal status without ever reaching `ringing` means Twilio had
nowhere to deliver the INVITE.

Two columns, both monotonic (only ever move forward) so out-of-order status
callbacks can't corrupt them — Twilio does deliver these out of order:

    sip_last_ringing_at       last time a SIP leg reached `ringing`
    sip_last_ring_attempt_at  last time we started ringing a SIP leg

A large gap between the two (attempts continuing, nothing ringing) is the
signature of an unregistered device.
"""


def up(conn):
    conn.execute("""
        ALTER TABLE staff_extensions ADD COLUMN sip_last_ringing_at TEXT
    """)
    conn.execute("""
        ALTER TABLE staff_extensions ADD COLUMN sip_last_ring_attempt_at TEXT
    """)


def down(conn):
    conn.execute("""
        ALTER TABLE staff_extensions DROP COLUMN sip_last_ringing_at
    """)
    conn.execute("""
        ALTER TABLE staff_extensions DROP COLUMN sip_last_ring_attempt_at
    """)
