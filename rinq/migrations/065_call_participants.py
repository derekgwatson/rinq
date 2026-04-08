"""Add call_participants table — source of truth for who is in each call.

Replaces the reverse-engineering approach in call_state.py where participants
were discovered from scattered DB entries and Twilio API calls every 3 seconds.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_participants (
            id INTEGER PRIMARY KEY,
            conference_name TEXT NOT NULL,
            call_sid TEXT NOT NULL,
            role TEXT NOT NULL,
            name TEXT,
            phone_number TEXT,
            email TEXT,
            joined_at TEXT NOT NULL DEFAULT (datetime('now')),
            left_at TEXT,
            UNIQUE(conference_name, call_sid)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_participants_conf ON call_participants(conference_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_participants_sid ON call_participants(call_sid)")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS call_participants")
