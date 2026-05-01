"""
Migration 075: Rename Zendesk-specific columns to vendor-neutral names

Makes the voicemail/ticket routing layer independent of any specific
ticket provider. The routing_type value 'zendesk' is renamed to 'ticket'
so it describes the behaviour (create a ticket) rather than the vendor.
"""


def up(conn):
    conn.execute("ALTER TABLE voicemail_destinations RENAME COLUMN zendesk_group_id TO ticket_group_id")
    conn.execute("ALTER TABLE recording_log RENAME COLUMN zendesk_ticket_id TO ticket_id")
    conn.execute("UPDATE voicemail_destinations SET routing_type = 'ticket' WHERE routing_type = 'zendesk'")
    conn.commit()


def down(conn):
    conn.execute("ALTER TABLE voicemail_destinations RENAME COLUMN ticket_group_id TO zendesk_group_id")
    conn.execute("ALTER TABLE recording_log RENAME COLUMN ticket_id TO zendesk_ticket_id")
    conn.execute("UPDATE voicemail_destinations SET routing_type = 'zendesk' WHERE routing_type = 'ticket'")
    conn.commit()
