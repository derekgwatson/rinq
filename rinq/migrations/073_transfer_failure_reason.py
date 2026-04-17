"""Add transfer_failure_reason so agents see the specific reason a transfer target couldn't be reached."""


def up(conn):
    conn.execute("ALTER TABLE queued_calls ADD COLUMN transfer_failure_reason TEXT")
    conn.execute("ALTER TABLE call_log ADD COLUMN transfer_failure_reason TEXT")


def down(conn):
    # SQLite doesn't support DROP COLUMN before 3.35
    pass
