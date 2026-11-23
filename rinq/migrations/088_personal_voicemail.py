"""Personal voicemail lines - a person's own message bank, kept apart from Tina.

Rohan's mobile (Datavoice) has no visual voicemail, so his unanswered calls
divert to a Tina number. Calls to a personal line never reach call_log or
recording_log: no report, leaderboard, recordings page or Audrey sees them.
Messages are listed only to the line's owner, on their My Numbers page.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE personal_voicemail_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL UNIQUE,
            owner_email TEXT NOT NULL,
            email_copies INTEGER NOT NULL DEFAULT 1,
            greeting_token TEXT,
            greeting_ext TEXT,
            created_at TEXT NOT NULL,
            created_by TEXT,
            updated_at TEXT NOT NULL,
            updated_by TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE personal_voicemails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_id INTEGER NOT NULL REFERENCES personal_voicemail_lines(id),
            recording_sid TEXT NOT NULL UNIQUE,
            from_number TEXT,
            caller_name TEXT,
            forwarded_from TEXT,
            duration_seconds INTEGER,
            transcription TEXT,
            transcription_problem TEXT,
            audio_file TEXT,
            emailed_at TEXT,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        )
    """)
    conn.execute("CREATE INDEX idx_personal_voicemails_line ON personal_voicemails(line_id, created_at)")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS personal_voicemails")
    conn.execute("DROP TABLE IF EXISTS personal_voicemail_lines")
