"""Add integration_provider column to tenants."""


def up(conn):
    # 001 was later edited to create this column itself, so on a NEW install
    # this ALTER failed and the master database could never be built (found
    # by the first test run, 2026-10-28). Existing installs already ran it.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tenants)")}
    if 'integration_provider' not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN integration_provider TEXT DEFAULT 'none'")


def down(conn):
    pass  # SQLite can't drop columns
