"""Address book: unlisted entries.

A staff member's PRIVATE mobile (Peter's "Private mobile", 2026-11-17) is
synced in so Tina recognises their calls - extension 9000 from outside, the
takeover ring, the caller's name - but it must never be shown: not in
contacts, transfer targets or the admin address book. Unlisted rows are
found by number only.
"""


def up(conn):
    conn.execute("ALTER TABLE address_book ADD COLUMN unlisted INTEGER NOT NULL DEFAULT 0")


def down(conn):
    conn.execute("ALTER TABLE address_book DROP COLUMN unlisted")
