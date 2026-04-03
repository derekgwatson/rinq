"""Add domain and branding fields to tenants."""


def up(conn):
    conn.execute("ALTER TABLE tenants ADD COLUMN domain TEXT")
    conn.execute("ALTER TABLE tenants ADD COLUMN product_name TEXT")


def down(conn):
    pass
