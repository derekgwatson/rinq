"""Shared desk phones: a free-text note per phone.

Where the handset physically sits, its MAC, what's wrong with it — the things
an admin needs when a store rings about "the phone on the front counter" and
the phone's name alone doesn't say which one. Edited in place on
Admin -> Desk phones.

On users rather than a new table: the shared phone IS the users row, and
upsert_user (credential sync) only writes the columns it names, so the note
survives a sync.
"""


def up(conn):
    conn.execute("ALTER TABLE users ADD COLUMN notes TEXT")


def down(conn):
    conn.execute("ALTER TABLE users DROP COLUMN notes")
