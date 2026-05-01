"""Add voicemail_destination_id to phone_numbers for direct-ring numbers."""


def up(conn):
    conn.execute("""
        ALTER TABLE phone_numbers
        ADD COLUMN voicemail_destination_id INTEGER REFERENCES voicemail_destinations(id)
    """)


def down(conn):
    conn.execute("""
        ALTER TABLE phone_numbers
        DROP COLUMN voicemail_destination_id
    """)
