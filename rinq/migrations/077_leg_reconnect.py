"""Auto-reconnect support for dropped call legs.

Two tables, both modelled on ring_attempts (DB-backed so state is correct
across gunicorn workers, cleaned up by the 5-minute queue cleanup cron):

- leg_intents: records that a leg ended on PURPOSE (user pressed End / Go back).
  A network-dropped leg can't post this, so ABSENCE of an intent == a drop.
  This is how we distinguish a genuine drop (reconnect) from a deliberate
  hangup (don't reconnect).

- reconnect_attempts: one active row per conference while we re-ring a dropped
  client:/SIP leg back into it. Event-driven via Twilio status callbacks; no
  background threads.

All behaviour gated by the per-tenant bot_settings flag 'auto_reconnect_enabled'
(default off) — these tables are inert until the flag is on.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leg_intents (
            call_sid TEXT PRIMARY KEY,
            intent TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconnect_attempts (
            id INTEGER PRIMARY KEY,
            conference_name TEXT NOT NULL UNIQUE,
            dropped_call_sid TEXT NOT NULL,
            target_to TEXT NOT NULL,
            from_number TEXT,
            role TEXT,
            name TEXT,
            original_call_sid TEXT,
            context TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'reconnecting',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reconnect_attempts_conf ON reconnect_attempts(conference_name)")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS reconnect_attempts")
    conn.execute("DROP TABLE IF EXISTS leg_intents")
