"""Add sip_registered_at to staff_extensions for SIP device presence tracking."""


def up(conn):
    conn.execute("""
        ALTER TABLE staff_extensions ADD COLUMN sip_registered_at TEXT
    """)


def down(conn):
    # SQLite doesn't support DROP COLUMN before 3.35
    pass
