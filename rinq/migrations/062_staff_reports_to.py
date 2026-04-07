"""
Add reports_to column to staff_extensions.

Stores the manager's email address so we can build the reporting
hierarchy locally instead of depending on Peter (Watson bot-team).
"""


def up(conn):
    conn.execute("""
        ALTER TABLE staff_extensions
        ADD COLUMN reports_to TEXT
    """)
    conn.commit()


def down(conn):
    pass
