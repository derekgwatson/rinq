"""Add alternate_caller_id to staff_extensions for the *99 toggle feature."""


def up(conn):
    conn.execute("""
        ALTER TABLE staff_extensions ADD COLUMN alternate_caller_id TEXT
    """)


def down(conn):
    conn.execute("""
        ALTER TABLE staff_extensions DROP COLUMN alternate_caller_id
    """)
