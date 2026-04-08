"""Add permissions table for local role management.

Replaces the external Watson/Grant permission service with a local table
so team management works without the bot-team API.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            granted_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(email)
        )
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS permissions")
