"""Enable voicemail escape on existing queues that don't have it configured.

Queues default to allow_voicemail_escape=0 (off), which means callers can wait
the full max_wait_time (~10 min) with no option to leave a message. This migration
enables voicemail escape on all existing queues and sets sensible defaults for
escape_announcement_delay (2 cycles ≈ 2 min) and escape_repeat_interval (3 cycles).

New queues created after this migration still default to 0 — this only backfills
existing queues that were never explicitly configured.
"""


def up(conn):
    conn.execute("""
        UPDATE queues
        SET allow_voicemail_escape = 1,
            escape_announcement_delay = CASE
                WHEN escape_announcement_delay IS NULL OR escape_announcement_delay = 0 THEN 2
                ELSE escape_announcement_delay
            END,
            escape_repeat_interval = CASE
                WHEN escape_repeat_interval IS NULL OR escape_repeat_interval = 0 THEN 3
                ELSE escape_repeat_interval
            END
        WHERE allow_voicemail_escape = 0
    """)


def down(conn):
    # Can't safely revert — would need to know the original per-queue value
    pass
