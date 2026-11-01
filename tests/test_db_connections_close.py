"""Every `with db._get_conn() as conn:` must CLOSE the connection.

sqlite3's own context manager only commits; it never closes. On do-rinq
(Python 3.14) the leftover handles took a worker past 1024 open files in under
a day, and incoming calls then failed with "unable to open database file" -
callers heard "an application error has occurred" (2026-11-02).
"""

import sqlite3

import pytest

from rinq.database.db import Database


def test_with_block_closes_the_connection(tmp_path):
    db = Database(tmp_path / 'rinq.db')
    with db._get_conn() as conn:
        conn.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_with_block_still_commits(tmp_path):
    db = Database(tmp_path / 'rinq.db')
    with db._get_conn() as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
    with db._get_conn() as conn:
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 1


def test_with_block_rolls_back_on_error_and_still_closes(tmp_path):
    db = Database(tmp_path / 'rinq.db')
    with db._get_conn() as conn:
        conn.execute("CREATE TABLE t (x)")
    with pytest.raises(RuntimeError):
        with db._get_conn() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
