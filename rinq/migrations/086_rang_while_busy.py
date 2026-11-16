"""Rang while you were on a call.

A customer returning a staff member's call is offered "press 1 for Brittany"
only when Brittany can answer. When she is on another call there is no offer,
and the caller goes to the queue or Stuart - and Brittany never learns they
rang until a message reaches her (Alison, 16 Nov 2026: rang twice during
Brittany's 13-minute call, got Stuart, then rang a third time).

One row per staff member + caller: Tina notes it when the offer is skipped
because the person is on a call, and the phone page shows it to them once
their call ends, with a Call back button.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rang_while_busy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_email TEXT NOT NULL,
            from_number TEXT NOT NULL,
            first_rang_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_rang_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            times INTEGER NOT NULL DEFAULT 1,
            dismissed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rang_while_busy_staff "
                 "ON rang_while_busy(staff_email, last_rang_at)")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS rang_while_busy")
