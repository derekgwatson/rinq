"""Add can_view_all_recordings flag to tenant_users."""


def up(conn):
    conn.execute("""
        ALTER TABLE tenant_users
        ADD COLUMN can_view_all_recordings INTEGER DEFAULT 0
    """)


def down(conn):
    conn.execute("""
        ALTER TABLE tenant_users
        DROP COLUMN can_view_all_recordings
    """)
