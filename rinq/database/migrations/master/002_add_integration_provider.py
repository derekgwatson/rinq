"""Add integration_provider column to tenants."""


def up(conn):
    conn.execute("ALTER TABLE tenants ADD COLUMN integration_provider TEXT DEFAULT 'none'")


def down(conn):
    pass  # SQLite can't drop columns
