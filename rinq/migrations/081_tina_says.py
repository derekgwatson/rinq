"""Tina Says — a daily, cheeky one-liner on the phone page built from call data.

- tina_says_runs: one row per local date. Claimed with INSERT OR IGNORE before
  generating, so only one gunicorn worker (of 3) ever generates a day's lines.
- tina_says_lines: the day's lines. `source` is 'llm' (written by Claude from
  the facts, then checked) or 'template' (the built-in fallback). Kept, not
  purged: the last week's lines are fed back so Tina doesn't repeat herself.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tina_says_runs (
            say_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            model TEXT,
            error TEXT,
            facts_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tina_says_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            say_date TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            kind TEXT,
            text TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tina_says_lines_date ON tina_says_lines(say_date)"
    )


def down(conn):
    conn.execute("DROP TABLE IF EXISTS tina_says_lines")
    conn.execute("DROP TABLE IF EXISTS tina_says_runs")
