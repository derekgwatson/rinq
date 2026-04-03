"""Add email settings to tenants."""


def up(conn):
    conn.execute("ALTER TABLE tenants ADD COLUMN email_from TEXT")
    conn.execute("ALTER TABLE tenants ADD COLUMN recordings_group_email TEXT")


def down(conn):
    pass
