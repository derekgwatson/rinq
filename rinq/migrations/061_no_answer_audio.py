"""
Add no_answer_audio_id to call_flows.

Separate audio prompt for open-hours voicemail (e.g. "sorry, we can't
come to the phone right now") vs closed-hours (e.g. "we're currently closed").
"""


def up(conn):
    conn.execute("""
        ALTER TABLE call_flows
        ADD COLUMN no_answer_audio_id INTEGER REFERENCES audio_files(id)
    """)
    conn.commit()


def down(conn):
    pass
