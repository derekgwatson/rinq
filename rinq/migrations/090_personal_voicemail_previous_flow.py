"""Personal voicemail lines remember what their number was on before.

Making a number a personal line takes it off its call flow and voicemail
box. Nothing recorded which ones, so removing the line (or moving it to
another number) left the old number with no call flow at all - callers
heard "no one is available". The line now keeps both, and they are put
back when the number stops being a personal line.

Lines made before this have nothing recorded; nothing is guessed for them.
"""


def up(conn):
    conn.execute("ALTER TABLE personal_voicemail_lines ADD COLUMN previous_call_flow_id INTEGER")
    conn.execute("ALTER TABLE personal_voicemail_lines ADD COLUMN previous_voicemail_destination_id INTEGER")


def down(conn):
    conn.execute("ALTER TABLE personal_voicemail_lines DROP COLUMN previous_voicemail_destination_id")
    conn.execute("ALTER TABLE personal_voicemail_lines DROP COLUMN previous_call_flow_id")
