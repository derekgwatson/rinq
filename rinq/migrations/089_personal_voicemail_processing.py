"""Personal voicemail: finish a message in the background, and finish it later
if the first attempt died.

Twilio's Record callback used to wait while Tina downloaded, transcribed and
emailed the message - past Twilio's 15 s the caller could hear an
application error, and a killed request left a message with no audio and no
email that nothing ever retried. Now the callback is answered at once and a
background thread does the work; these columns let a second Twilio callback
(recordingStatusCallback) pick up a message the first one didn't finish.

- processing_started_at: a worker has it (a lease; a stale one can be retaken)
- processed_at: done - audio, words and email all attempted
- status_seen_at: Twilio's recording-status callback has arrived, so no
  later callback is coming
"""


def up(conn):
    conn.execute("ALTER TABLE personal_voicemails ADD COLUMN processing_started_at TEXT")
    conn.execute("ALTER TABLE personal_voicemails ADD COLUMN processed_at TEXT")
    conn.execute("ALTER TABLE personal_voicemails ADD COLUMN status_seen_at TEXT")
    # Every message taken before this ran was handled start to finish inline
    conn.execute("UPDATE personal_voicemails SET processed_at = created_at")


def down(conn):
    conn.execute("ALTER TABLE personal_voicemails DROP COLUMN status_seen_at")
    conn.execute("ALTER TABLE personal_voicemails DROP COLUMN processed_at")
    conn.execute("ALTER TABLE personal_voicemails DROP COLUMN processing_started_at")
