"""Add voicemail_on_no_answer to queues.

When set, a queue that rings its agents and gets no answer sends the caller
straight to voicemail instead of holding them in the queue. Intended for
desk-phone teams who don't watch the browser dashboard (e.g. Chris's stores),
where a held caller just rings an unwatched desk until max_wait_time expires.
Default off — existing queues keep the hold-and-wait behaviour.
"""


def up(conn):
    conn.execute("""
        ALTER TABLE queues ADD COLUMN voicemail_on_no_answer INTEGER NOT NULL DEFAULT 0
    """)


def down(conn):
    conn.execute("""
        ALTER TABLE queues DROP COLUMN voicemail_on_no_answer
    """)
