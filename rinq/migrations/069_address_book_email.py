"""Add email column to address_book so synced staff can be linked back to staff_extensions."""


def up(conn):
    conn.execute("""
        ALTER TABLE address_book ADD COLUMN email TEXT
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_address_book_email
            ON address_book(email)
    """)


def down(conn):
    # SQLite doesn't support DROP COLUMN before 3.35
    pass
