"""Tenant-wide call recording, independent of which device answered.

Background: recording was only ever started by JavaScript in the browser
softphone page (phone.html). Staff on a SIP device — a desk phone or a SIP
softphone app — never ran that code, so none of their calls were recorded,
while their personal "record my calls" setting still read ON. Three staff had
zero recordings against ~680 answered calls.

This adds:

- recording_starts: one row per conversation we have started recording,
  keyed on the recorded leg's call SID. DB-backed rather than in-memory
  because gunicorn runs 3 workers (same reason as ring_attempts), and because
  several webhooks can race to start the same conversation's recording.
  Cleaned up by the 5-minute queue cleanup cron.

The behaviour itself is gated by the per-tenant bot_settings flag
'record_all_calls' (default off — see recording_service.RECORD_ALL_SETTING_KEY),
so this table is inert until an admin turns it on at /admin/storage.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recording_starts (
            call_sid TEXT PRIMARY KEY,
            conference_name TEXT,
            call_type TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recording_starts_created "
        "ON recording_starts(created_at)"
    )


def down(conn):
    conn.execute("DROP TABLE IF EXISTS recording_starts")
