"""Shared desk phones: the handset logs in as itself, people log in to it.

Until now every SIP credential belonged to one person, so moving someone to a
different desk meant re-entering a login on the handset — at a store, on a
store network, usually by asking staff to read settings off a screen.

A shared desk phone is a SIP credential that belongs to the PHONE ("Hume desk
3"). It is set up once and never touched again. Who is sitting at it is a row
in desk_phone_logins, changed by dialling *50 + extension on the handset (*51
logs out) or from Admin -> Desk phones.

- users.is_shared_device: marks the credential as a phone, not a person. A
  flag rather than "staff_email IS NULL" because orphaned personal
  credentials also have no staff_email, and they are not phones anyone set up.
- desk_phone_logins: one row per occupied phone. staff_email is UNIQUE —
  logging in at a second phone moves you off the first, so calls never ring a
  desk you have walked away from.

Everything that turns a SIP username into a person goes through
db.get_staff_email_for_sip_username(), and everything that rings a person goes
through services/person_devices.py, so both follow the login.
"""


def up(conn):
    conn.execute("""
        ALTER TABLE users ADD COLUMN is_shared_device INTEGER NOT NULL DEFAULT 0
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desk_phone_logins (
            device_username TEXT PRIMARY KEY,
            staff_email TEXT NOT NULL UNIQUE,
            logged_in_at TEXT NOT NULL,
            logged_in_by TEXT
        )
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS desk_phone_logins")
    conn.execute("ALTER TABLE users DROP COLUMN is_shared_device")
