"""Voicemail box: optional "assign to" person.

A ticket-routed voicemail box could only name a GROUP, so a person's own
direct line (e.g. Brittany's 02 6192 6884, which customers ring back) filed
her voicemails into whatever group the box named and nobody in particular
owned them. With this set, the ticket is also assigned to that person —
Zelda notifies an assignee, and a group alone notifies nobody.
"""


def up(conn):
    conn.execute("ALTER TABLE voicemail_destinations ADD COLUMN ticket_assignee_email TEXT")


def down(conn):
    conn.execute("ALTER TABLE voicemail_destinations DROP COLUMN ticket_assignee_email")
