"""Add hide_mobile flag so staff can hide their mobile from the directory."""


def up(conn):
    conn.execute("""
        ALTER TABLE staff_extensions ADD COLUMN hide_mobile INTEGER NOT NULL DEFAULT 0
    """)


def down(conn):
    # SQLite doesn't support DROP COLUMN before 3.35
    pass
