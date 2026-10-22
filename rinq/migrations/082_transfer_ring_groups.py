"""Transfers ring every device a person has.

Background: a transfer to an extension rang only that person's browser
softphone. Anyone working from a desk phone or their mobile never heard it,
so the transfer failed back to the sender. Queue and direct calls already
rang browser + desk phone + screened mobile; transfers now do the same
(services/person_devices.py is the one list of devices for all three).

Ringing several legs for one transfer needs "first to answer wins, and if
every leg fails, fail the transfer once". The failure side can't key off
ring_attempts, because a browser tab that isn't open fails within ~100ms —
often before the creating code has stored the leg's SID — so a late-stored
leg would never be removed and the group would never empty. Instead we
count deaths by SID against how many legs were actually placed:

- transfer_ring_groups: one row per transfer attempt. expected starts high
  while the legs are being placed (so no early death can look like "all
  failed"), then is set to the number actually placed. winner_sid is set
  atomically by the first leg to answer; failed_fired is set atomically by
  the last leg to die, so the failure is handled exactly once. closed is
  set by Go back / Cancel so no leg can answer after it. context holds what
  the winner's bookkeeping needs.
- transfer_ring_legs: one row per leg, state 'ringing' | 'dead'. A death
  can arrive before the creator stores the leg; the store never touches
  state, so the death always sticks.

Gated by the per-tenant bot_settings flag 'transfer_ring_all_devices'
(default off) — inert until an admin turns it on at /admin/settings.
Cleaned up by the 5-minute queue cleanup cron.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transfer_ring_groups (
            group_key TEXT PRIMARY KEY,
            original_call_sid TEXT NOT NULL,
            expected INTEGER NOT NULL,
            winner_sid TEXT,
            failed_fired INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0,
            context TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfer_ring_groups_original "
        "ON transfer_ring_groups(original_call_sid)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfer_ring_groups_created "
        "ON transfer_ring_groups(created_at)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transfer_ring_legs (
            group_key TEXT NOT NULL,
            call_sid TEXT NOT NULL,
            kind TEXT,
            to_addr TEXT,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (group_key, call_sid)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfer_ring_legs_sid "
        "ON transfer_ring_legs(call_sid)"
    )


def down(conn):
    conn.execute("DROP TABLE IF EXISTS transfer_ring_legs")
    conn.execute("DROP TABLE IF EXISTS transfer_ring_groups")
