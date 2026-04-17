"""
Database operations for Rinq.

Manages:
- Phone numbers and their forwarding rules
- User/extension mappings
- Call recording logs
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta


def _parse_dt(value: str) -> datetime:
    """Parse a DB timestamp string as UTC-aware datetime.

    DB stores naive UTC strings like '2026-04-11 10:15:00'.
    This ensures they're timezone-aware so subtraction from
    datetime.now(timezone.utc) works correctly.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

try:
    from shared.migrations import MigrationRunner
except ImportError:
    from rinq.vendor.migrations import MigrationRunner

from rinq.database.stats_mixin import StatsMixin
from rinq.database.call_log_mixin import CallLogMixin


class Database(StatsMixin, CallLogMixin):
    """SQLite database for Rinq."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = Path(__file__).parent / "rinq.db"
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """Initialize database with migrations."""
        migrations_dir = Path(__file__).parent.parent / "migrations"
        runner = MigrationRunner(
            db_path=str(self.db_path),
            migrations_dir=str(migrations_dir)
        )
        runner.run_pending_migrations(verbose=True)

    def _get_conn(self):
        """Get database connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # =========================================================================
    # Shared helpers
    # =========================================================================

    @staticmethod
    def _fill_hourly(rows, hour_key='hour'):
        """Build a 24-hour list from query rows, zero-filling missing hours."""
        hour_data = {row[hour_key]: row for row in rows}
        result = []
        for hour in range(24):
            if hour in hour_data:
                row = hour_data[hour]
                result.append({
                    'hour': hour,
                    'label': f"{hour:02d}:00",
                    'total_calls': row['total_calls'] or 0,
                    'answered_calls': row['answered_calls'] or 0,
                    'abandoned_calls': row['abandoned_calls'] or 0,
                    'timeout_calls': row['timeout_calls'] or 0,
                })
            else:
                result.append({
                    'hour': hour,
                    'label': f"{hour:02d}:00",
                    'total_calls': 0,
                    'answered_calls': 0,
                    'abandoned_calls': 0,
                    'timeout_calls': 0,
                })
        return result

    # =========================================================================
    # Phone Numbers
    # =========================================================================

    def get_phone_numbers(self) -> list[dict]:
        """Get all phone numbers."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM phone_numbers
                ORDER BY friendly_name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_phone_number(self, sid: str) -> dict | None:
        """Get a phone number by SID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM phone_numbers WHERE sid = ?
            """, (sid,)).fetchone()
            return dict(row) if row else None

    def get_phone_number_by_number(self, phone_number: str) -> dict | None:
        """Get a phone number by E.164 number."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM phone_numbers WHERE phone_number = ?
            """, (phone_number,)).fetchone()
            return dict(row) if row else None

    def upsert_phone_number(self, data: dict) -> None:
        """Insert or update a phone number."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO phone_numbers (sid, phone_number, friendly_name, forward_to, is_active, synced_at)
                VALUES (:sid, :phone_number, :friendly_name, :forward_to, :is_active, :synced_at)
                ON CONFLICT(sid) DO UPDATE SET
                    phone_number = :phone_number,
                    friendly_name = :friendly_name,
                    forward_to = :forward_to,
                    is_active = :is_active,
                    synced_at = :synced_at
            """, data)
            conn.commit()

    def update_forward_to(self, sid: str, forward_to: str, updated_by: str) -> None:
        """Update the forwarding number for a phone number."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE phone_numbers
                SET forward_to = ?, updated_at = ?, updated_by = ?
                WHERE sid = ?
            """, (forward_to, now, updated_by, sid))
            conn.commit()

    def update_browser_ring(self, sid: str, ring_browser: bool, browser_identity: str, updated_by: str) -> None:
        """Update browser ring settings for a phone number."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE phone_numbers
                SET ring_browser = ?, browser_identity = ?, updated_at = ?, updated_by = ?
                WHERE sid = ?
            """, (1 if ring_browser else 0, browser_identity or None, now, updated_by, sid))
            conn.commit()

    def update_phone_number_section(self, sid: str, section: str, updated_by: str) -> None:
        """Update the section for a phone number."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE phone_numbers
                SET section = ?, updated_at = ?, updated_by = ?
                WHERE sid = ?
            """, (section or None, now, updated_by, sid))
            conn.commit()

    def get_phone_numbers_by_section(self, section: str) -> list[dict]:
        """Get all phone numbers for a section."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM phone_numbers
                WHERE section = ? AND is_active = 1
                ORDER BY friendly_name
            """, (section,)).fetchall()
            return [dict(row) for row in rows]

    def remove_phone_numbers_not_in(self, valid_sids: set) -> int:
        """Remove phone numbers whose SID is not in the given set.

        Used during sync to remove numbers that no longer exist in Twilio.
        Returns the number of removed rows.
        """
        if not valid_sids:
            # If Twilio returns no numbers, don't delete everything
            # (could be an API error)
            return 0

        with self._get_conn() as conn:
            # Get count of rows to delete
            placeholders = ','.join('?' * len(valid_sids))
            cursor = conn.execute(f"""
                SELECT COUNT(*) FROM phone_numbers
                WHERE sid NOT IN ({placeholders})
            """, tuple(valid_sids))
            count = cursor.fetchone()[0]

            if count > 0:
                conn.execute(f"""
                    DELETE FROM phone_numbers
                    WHERE sid NOT IN ({placeholders})
                """, tuple(valid_sids))
                conn.commit()

            return count

    # =========================================================================
    # Verified Caller IDs (external numbers verified in Twilio but not owned)
    # =========================================================================

    def get_verified_caller_ids(self, active_only: bool = True) -> list[dict]:
        """Get all verified caller IDs.

        Args:
            active_only: If True, only return active caller IDs
        """
        with self._get_conn() as conn:
            if active_only:
                rows = conn.execute("""
                    SELECT * FROM verified_caller_ids
                    WHERE is_active = 1
                    ORDER BY friendly_name
                """).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM verified_caller_ids
                    ORDER BY friendly_name
                """).fetchall()
            return [dict(row) for row in rows]

    def get_verified_caller_id(self, phone_number: str) -> dict | None:
        """Get a verified caller ID by phone number."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM verified_caller_ids WHERE phone_number = ?
            """, (phone_number,)).fetchone()
            return dict(row) if row else None

    def get_verified_caller_ids_by_section(self, section: str) -> list[dict]:
        """Get all verified caller IDs for a section."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM verified_caller_ids
                WHERE section = ? AND is_active = 1
                ORDER BY friendly_name
            """, (section,)).fetchall()
            return [dict(row) for row in rows]

    def add_verified_caller_id(
        self,
        phone_number: str,
        friendly_name: str,
        section: str | None = None,
        notes: str | None = None,
        created_by: str | None = None
    ) -> int:
        """Add a new verified caller ID.

        Returns the ID of the new row.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO verified_caller_ids
                (phone_number, friendly_name, section, notes, is_active, created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (phone_number, friendly_name, section, notes, now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_verified_caller_id(
        self,
        phone_number: str,
        friendly_name: str | None = None,
        section: str | None = None,
        is_active: bool | None = None,
        notes: str | None = None,
        updated_by: str | None = None
    ) -> bool:
        """Update a verified caller ID.

        Returns True if a row was updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        updates = []
        params = []

        if friendly_name is not None:
            updates.append("friendly_name = ?")
            params.append(friendly_name)
        if section is not None:
            updates.append("section = ?")
            params.append(section if section else None)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if is_active else 0)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes if notes else None)

        if not updates:
            return False

        updates.append("updated_at = ?")
        params.append(now)
        updates.append("updated_by = ?")
        params.append(updated_by)
        params.append(phone_number)

        with self._get_conn() as conn:
            cursor = conn.execute(f"""
                UPDATE verified_caller_ids
                SET {', '.join(updates)}
                WHERE phone_number = ?
            """, params)
            conn.commit()
            return cursor.rowcount > 0

    def delete_verified_caller_id(self, phone_number: str) -> bool:
        """Delete a verified caller ID.

        Returns True if a row was deleted.
        """
        with self._get_conn() as conn:
            cursor = conn.execute("""
                DELETE FROM verified_caller_ids WHERE phone_number = ?
            """, (phone_number,))
            conn.commit()
            return cursor.rowcount > 0

    def deactivate_verified_caller_ids_not_in(self, phone_numbers: set) -> int:
        """Mark verified caller IDs as inactive if not in the given set.

        Used during sync to deactivate numbers that are no longer in Twilio.
        Returns the number of rows deactivated.
        """
        if not phone_numbers:
            # If empty set, deactivate all
            with self._get_conn() as conn:
                cursor = conn.execute("""
                    UPDATE verified_caller_ids SET is_active = 0 WHERE is_active = 1
                """)
                conn.commit()
                return cursor.rowcount

        with self._get_conn() as conn:
            placeholders = ','.join('?' * len(phone_numbers))
            cursor = conn.execute(f"""
                UPDATE verified_caller_ids
                SET is_active = 0
                WHERE is_active = 1 AND phone_number NOT IN ({placeholders})
            """, list(phone_numbers))
            conn.commit()
            return cursor.rowcount

    # =========================================================================
    # Users (SIP credentials for staff)
    # =========================================================================

    def get_users(self) -> list[dict]:
        """Get all users."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM users
                ORDER BY friendly_name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_user(self, sid: str) -> dict | None:
        """Get a user by SID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM users WHERE sid = ?
            """, (sid,)).fetchone()
            return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict | None:
        """Get a user by email (linked to staff)."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM users WHERE staff_email = ?
            """, (email,)).fetchone()
            return dict(row) if row else None

    def upsert_user(self, data: dict) -> None:
        """Insert or update a user."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO users (sid, username, friendly_name, staff_email, is_active, synced_at)
                VALUES (:sid, :username, :friendly_name, :staff_email, :is_active, :synced_at)
                ON CONFLICT(sid) DO UPDATE SET
                    username = :username,
                    friendly_name = :friendly_name,
                    staff_email = :staff_email,
                    is_active = :is_active,
                    synced_at = :synced_at
            """, data)
            conn.commit()

    def deactivate_users_not_in(self, valid_sids: set) -> int:
        """Deactivate users that are not in the given set of SIDs.

        Returns the count of deactivated users.
        """
        if not valid_sids:
            return 0

        with self._get_conn() as conn:
            # Get count of active users not in the set
            placeholders = ",".join("?" * len(valid_sids))
            cursor = conn.execute(f"""
                UPDATE users
                SET is_active = 0
                WHERE is_active = 1
                AND sid NOT IN ({placeholders})
            """, tuple(valid_sids))
            conn.commit()
            return cursor.rowcount

    def delete_user(self, sid: str) -> bool:
        """Delete a user by SID."""
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM users WHERE sid = ?", (sid,))
            conn.commit()
            return cursor.rowcount > 0

    def update_user_friendly_name(self, sid: str, friendly_name: str) -> bool:
        """Update a user's friendly name."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                UPDATE users SET friendly_name = ? WHERE sid = ?
            """, (friendly_name, sid))
            conn.commit()
            return cursor.rowcount > 0

    def update_user_password(self, sid: str, password: str) -> bool:
        """Store a user's SIP password locally."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                UPDATE users SET password = ? WHERE sid = ?
            """, (password, sid))
            conn.commit()
            return cursor.rowcount > 0

    def get_user_password(self, sid: str) -> str | None:
        """Get a user's stored SIP password."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT password FROM users WHERE sid = ?
            """, (sid,)).fetchone()
            return row['password'] if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        """Get a user by SIP username.

        Used to look up the user when a SIP device makes an outbound call.
        """
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM users WHERE username = ?
            """, (username,)).fetchone()
            return dict(row) if row else None

    def update_user_default_caller_id(self, sid: str, caller_id: str | None, updated_by: str) -> bool:
        """Set a user's default caller ID for outbound calls.

        Args:
            sid: The user's Twilio SID
            caller_id: E.164 phone number to use as caller ID, or None to clear
            updated_by: Who is making this change
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                UPDATE users
                SET default_caller_id = ?, updated_at = ?, updated_by = ?
                WHERE sid = ?
            """, (caller_id, now, updated_by, sid))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Phone Assignments (linking staff to phone numbers)
    # =========================================================================

    def get_assignments(self) -> list[dict]:
        """Get all phone assignments with phone number details."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    pa.*,
                    pn.phone_number,
                    pn.friendly_name
                FROM phone_assignments pa
                JOIN phone_numbers pn ON pa.phone_number_sid = pn.sid
                ORDER BY pn.friendly_name, pa.staff_email
            """).fetchall()
            return [dict(row) for row in rows]

    def get_assignments_for_user(self, email: str) -> list[dict]:
        """Get phone assignments for a specific user."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    pa.*,
                    pn.phone_number,
                    pn.friendly_name,
                    pn.forward_to
                FROM phone_assignments pa
                JOIN phone_numbers pn ON pa.phone_number_sid = pn.sid
                WHERE pa.staff_email = ?
                ORDER BY pn.friendly_name
            """, (email,)).fetchall()
            return [dict(row) for row in rows]

    def get_assignments_for_number(self, phone_number_sid: str) -> list[dict]:
        """Get all users assigned to a phone number."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM phone_assignments
                WHERE phone_number_sid = ?
                ORDER BY staff_email
            """, (phone_number_sid,)).fetchall()
            return [dict(row) for row in rows]

    def get_receivers_for_number(self, phone_number_sid: str) -> list[str]:
        """Get list of emails that should receive calls for a number."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT staff_email FROM phone_assignments
                WHERE phone_number_sid = ? AND can_receive = 1
            """, (phone_number_sid,)).fetchall()
            return [row['staff_email'] for row in rows]

    def add_assignment(self, phone_number_sid: str, staff_email: str,
                       can_receive: bool, can_make: bool, created_by: str) -> int:
        """Add a phone assignment."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO phone_assignments
                    (phone_number_sid, staff_email, can_receive, can_make,
                     created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (phone_number_sid, staff_email,
                  1 if can_receive else 0, 1 if can_make else 0,
                  now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_assignment(self, assignment_id: int, can_receive: bool,
                          can_make: bool, updated_by: str) -> None:
        """Update a phone assignment."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE phone_assignments
                SET can_receive = ?, can_make = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (1 if can_receive else 0, 1 if can_make else 0,
                  now, updated_by, assignment_id))
            conn.commit()

    def remove_assignment(self, assignment_id: int) -> None:
        """Remove a phone assignment."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM phone_assignments WHERE id = ?", (assignment_id,))
            conn.commit()

    # =========================================================================
    # Call Recordings
    # =========================================================================

    def log_recording(self, data: dict) -> int:
        """Log a call recording.

        Data can include:
            - recording_sid, call_sid, from_number, to_number
            - duration_seconds, recording_url
            - emailed_to, emailed_at, deleted_from_twilio
            - google_message_id (link to Google Group message)
            - call_type ('inbound', 'outbound', 'internal', 'voicemail')
            - staff_email (who was on the call)
            - local_file_path (temporary local cache for playback)
            - caller_name (customer name from Clara lookup)
            - staff_name (friendly display name for staff)
        """
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO recording_log (
                    recording_sid, call_sid, from_number, to_number,
                    duration_seconds, recording_url, emailed_to, emailed_at,
                    deleted_from_twilio, created_at, google_message_id,
                    call_type, staff_email, local_file_path, caller_name,
                    staff_name
                ) VALUES (
                    :recording_sid, :call_sid, :from_number, :to_number,
                    :duration_seconds, :recording_url, :emailed_to, :emailed_at,
                    :deleted_from_twilio, :created_at, :google_message_id,
                    :call_type, :staff_email, :local_file_path, :caller_name,
                    :staff_name
                )
            """, {
                'recording_sid': data.get('recording_sid'),
                'call_sid': data.get('call_sid'),
                'from_number': data.get('from_number'),
                'to_number': data.get('to_number'),
                'duration_seconds': data.get('duration_seconds'),
                'recording_url': data.get('recording_url'),
                'emailed_to': data.get('emailed_to'),
                'emailed_at': data.get('emailed_at'),
                'deleted_from_twilio': data.get('deleted_from_twilio', 0),
                'created_at': data.get('created_at'),
                'google_message_id': data.get('google_message_id'),
                'call_type': data.get('call_type'),
                'staff_email': data.get('staff_email'),
                'local_file_path': data.get('local_file_path'),
                'caller_name': data.get('caller_name'),
                'staff_name': data.get('staff_name'),
            })
            conn.commit()
            return cursor.lastrowid

    def get_recording(self, recording_id: int) -> dict | None:
        """Get a single recording by ID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM recording_log WHERE id = ?
            """, (recording_id,)).fetchone()
            return dict(row) if row else None

    def get_recording_by_sid(self, recording_sid: str) -> dict | None:
        """Get a recording by Twilio recording SID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM recording_log WHERE recording_sid = ?
            """, (recording_sid,)).fetchone()
            return dict(row) if row else None

    def get_recording_log(self, limit: int = 100, call_type: str = None,
                          exclude_voicemail: bool = False,
                          staff_emails: list[str] = None) -> list[dict]:
        """Get recent recording log entries.

        Args:
            limit: Max number of records to return
            call_type: Filter by call type ('inbound', 'outbound', etc.)
            exclude_voicemail: If True, exclude voicemail recordings
            staff_emails: If provided, only return recordings for these staff
        """
        with self._get_conn() as conn:
            query = "SELECT * FROM recording_log WHERE 1=1"
            params = []

            if staff_emails:
                placeholders = ','.join('?' for _ in staff_emails)
                query += f" AND staff_email IN ({placeholders})"
                params.extend(e.lower() for e in staff_emails)

            if call_type:
                query += " AND call_type = ?"
                params.append(call_type)

            if exclude_voicemail:
                query += " AND (call_type IS NULL OR call_type != 'voicemail')"

            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_recordings_for_staff(self, staff_email: str, limit: int = 100) -> list[dict]:
        """Get recordings for a specific staff member."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM recording_log
                WHERE staff_email = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (staff_email.lower(), limit)).fetchall()
            return [dict(row) for row in rows]

    def mark_recording_deleted(self, recording_sid: str) -> None:
        """Mark a recording as deleted from Twilio."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET deleted_from_twilio = 1
                WHERE recording_sid = ?
            """, (recording_sid,))
            conn.commit()

    def update_recording_google_message(self, recording_sid: str,
                                         google_message_id: str) -> None:
        """Update a recording with the Google Group message ID."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET google_message_id = ?
                WHERE recording_sid = ?
            """, (google_message_id, recording_sid))
            conn.commit()

    def update_recording_local_file(self, recording_sid: str,
                                     local_file_path: str) -> None:
        """Update a recording's local file path and mark as accessed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET local_file_path = ?, last_accessed_at = ?
                WHERE recording_sid = ?
            """, (local_file_path, now, recording_sid))
            conn.commit()

    def update_recording_last_accessed(self, recording_sid: str) -> None:
        """Update the last_accessed_at timestamp when a recording is played."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET last_accessed_at = ?
                WHERE recording_sid = ?
            """, (now, recording_sid))
            conn.commit()

    def get_stale_recordings(self, days: int = 30) -> list[dict]:
        """Get recordings not accessed in the given number of days.

        Returns recordings that have local files but haven't been accessed
        recently. Used for cache cleanup.

        Args:
            days: Number of days since last access (default 30)

        Returns:
            List of recording dicts with local_file_path set
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM recording_log
                WHERE local_file_path IS NOT NULL
                  AND local_file_path != ''
                  AND (last_accessed_at IS NULL OR last_accessed_at < ?)
                ORDER BY last_accessed_at ASC
            """, (cutoff,)).fetchall()
            return [dict(row) for row in rows]

    def get_undeleted_voicemails(self, hours: int = 1) -> list[dict]:
        """Get voicemail recordings not yet deleted from Twilio.

        Returns voicemails older than the given hours that haven't been
        marked as deleted. Used for cleanup of recordings where transcription
        callback never arrived.

        Args:
            hours: Minimum age in hours (default 1)

        Returns:
            List of recording dicts
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM recording_log
                WHERE deleted_from_twilio = 0
                  AND zendesk_ticket_id IS NOT NULL
                  AND created_at < ?
                ORDER BY created_at ASC
            """, (cutoff,)).fetchall()
            return [dict(row) for row in rows]

    def clear_recording_local_file(self, recording_sid: str) -> None:
        """Clear the local file path after purging a recording's cache."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET local_file_path = NULL
                WHERE recording_sid = ?
            """, (recording_sid,))
            conn.commit()

    def update_recording_drive_file(self, recording_sid: str,
                                     drive_file_id: str) -> None:
        """Update a recording's Google Drive file ID."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET drive_file_id = ?
                WHERE recording_sid = ?
            """, (drive_file_id, recording_sid))
            conn.commit()

    def update_recording_ticket(self, recording_sid: str,
                                 zendesk_ticket_id: int) -> None:
        """Update a recording's Zendesk ticket ID (for voicemails)."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET zendesk_ticket_id = ?
                WHERE recording_sid = ?
            """, (zendesk_ticket_id, recording_sid))
            conn.commit()

    def update_recording_transcription(self, recording_sid: str,
                                        transcription: str) -> dict | None:
        """Update a recording's transcription text.

        Returns the recording row so caller can access zendesk_ticket_id.
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET transcription = ?
                WHERE recording_sid = ?
            """, (transcription, recording_sid))
            conn.commit()
            # Return the updated record
            row = conn.execute("""
                SELECT * FROM recording_log WHERE recording_sid = ?
            """, (recording_sid,)).fetchone()
            return dict(row) if row else None

    # =========================================================================
    # User Recording Settings
    # =========================================================================

    def get_user_recording_default(self, email: str) -> bool:
        """Get whether a user has call recording enabled by default.

        Returns True (recording enabled) by default if not explicitly set.
        """
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT record_calls_default FROM users WHERE staff_email = ?
            """, (email.lower(),)).fetchone()
            if row is None:
                return True  # Default to enabled
            return bool(row['record_calls_default'])

    def set_user_recording_default(self, email: str, enabled: bool,
                                    updated_by: str) -> None:
        """Set whether a user has call recording enabled by default."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE users
                SET record_calls_default = ?, updated_at = ?, updated_by = ?
                WHERE staff_email = ?
            """, (1 if enabled else 0, now, updated_by, email.lower()))
            conn.commit()

    # =========================================================================
    # Activity Log
    # =========================================================================

    def log_activity(self, action: str, target: str, details: str, performed_by: str) -> None:
        """Log an activity."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO activity_log (action, target, details, performed_by, performed_at)
                VALUES (?, ?, ?, ?, ?)
            """, (action, target, details, performed_by, datetime.now(timezone.utc).isoformat()))
            conn.commit()

    def get_activity_log(self, limit: int = 100) -> list[dict]:
        """Get recent activity log entries."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM activity_log
                ORDER BY performed_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    # =========================================================================
    # Audio Files
    # =========================================================================

    def get_audio_files(self, file_type: str = None) -> list[dict]:
        """Get all audio files, optionally filtered by type."""
        with self._get_conn() as conn:
            if file_type:
                rows = conn.execute("""
                    SELECT * FROM audio_files WHERE file_type = ? AND is_active = 1
                    ORDER BY name
                """, (file_type,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM audio_files WHERE is_active = 1
                    ORDER BY file_type, name
                """).fetchall()
            return [dict(row) for row in rows]

    def get_audio_file(self, audio_id: int) -> dict | None:
        """Get an audio file by ID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM audio_files WHERE id = ?
            """, (audio_id,)).fetchone()
            return dict(row) if row else None

    def create_audio_file(self, data: dict, created_by: str) -> int:
        """Create a new audio file record."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO audio_files (name, description, file_type, file_url, file_path,
                                         duration_seconds, tts_text, tts_provider, tts_voice, tts_settings,
                                         created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (data['name'], data.get('description'), data['file_type'],
                  data.get('file_url'), data.get('file_path'), data.get('duration_seconds'),
                  data.get('tts_text'), data.get('tts_provider'), data.get('tts_voice'), data.get('tts_settings'),
                  now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_audio_file(self, audio_id: int, data: dict, updated_by: str) -> None:
        """Update an audio file's metadata (name, description, type, spoken text)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE audio_files
                SET name = ?, description = ?, file_type = ?, tts_text = ?,
                    updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (data['name'], data.get('description'), data['file_type'],
                  data.get('tts_text'), now, updated_by, audio_id))
            conn.commit()

    def deactivate_audio_file(self, audio_id: int) -> None:
        """Soft delete an audio file by setting is_active = 0."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE audio_files SET is_active = 0, updated_at = ?
                WHERE id = ?
            """, (now, audio_id))
            conn.commit()

    # =========================================================================
    # Schedules
    # =========================================================================

    def get_schedules(self) -> list[dict]:
        """Get all schedules with their holidays."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM schedules WHERE is_active = 1
                ORDER BY name
            """).fetchall()
            schedules = [dict(row) for row in rows]

            # Load holidays for each schedule
            for schedule in schedules:
                holidays = conn.execute("""
                    SELECT sh.*, af.name as audio_name, af.file_url as audio_url
                    FROM schedule_holidays sh
                    LEFT JOIN audio_files af ON sh.audio_id = af.id
                    WHERE sh.schedule_id = ?
                    ORDER BY sh.date
                """, (schedule['id'],)).fetchall()
                schedule['holidays'] = [dict(h) for h in holidays]

            return schedules

    def get_schedule(self, schedule_id: int) -> dict | None:
        """Get a schedule by ID with its holidays."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM schedules WHERE id = ?
            """, (schedule_id,)).fetchone()
            if not row:
                return None
            schedule = dict(row)

            # Get holidays with audio info
            holidays = conn.execute("""
                SELECT sh.*, af.name as audio_name, af.file_url as audio_url
                FROM schedule_holidays sh
                LEFT JOIN audio_files af ON sh.audio_id = af.id
                WHERE sh.schedule_id = ?
                ORDER BY sh.date
            """, (schedule_id,)).fetchall()
            schedule['holidays'] = [dict(h) for h in holidays]
            return schedule

    def create_schedule(self, data: dict, created_by: str) -> int:
        """Create a new schedule."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO schedules (name, description, timezone, business_hours,
                                       default_closure_action, default_closure_audio_id,
                                       default_closure_forward_to,
                                       created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (data['name'], data.get('description'), data.get('timezone', 'Australia/Sydney'),
                  data.get('business_hours'),
                  data.get('default_closure_action'), data.get('default_closure_audio_id'),
                  data.get('default_closure_forward_to'),
                  now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_schedule(self, schedule_id: int, data: dict, updated_by: str) -> None:
        """Update a schedule."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE schedules
                SET name = ?, description = ?, timezone = ?, business_hours = ?,
                    default_closure_action = ?, default_closure_audio_id = ?,
                    default_closure_forward_to = ?,
                    updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (data['name'], data.get('description'), data.get('timezone'),
                  data.get('business_hours'),
                  data.get('default_closure_action'), data.get('default_closure_audio_id'),
                  data.get('default_closure_forward_to'),
                  now, updated_by, schedule_id))
            conn.commit()

    def get_call_flows_using_schedule(self, schedule_id: int) -> list[dict]:
        """Get all call flows that use this schedule.

        Returns list of dicts with call flow id and name.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT id, name FROM call_flows
                WHERE schedule_id = ? AND is_active = 1
            """, (schedule_id,)).fetchall()
            return [dict(row) for row in rows]

    def delete_schedule(self, schedule_id: int) -> None:
        """Delete a schedule and its holidays.

        Raises ValueError if schedule is used by any call flows.
        """
        # Check if schedule is in use
        call_flows = self.get_call_flows_using_schedule(schedule_id)
        if call_flows:
            flow_names = ', '.join(cf['name'] for cf in call_flows)
            raise ValueError(f"Cannot delete: schedule is used by call flow(s): {flow_names}")

        with self._get_conn() as conn:
            # Delete holidays first (foreign key)
            conn.execute("DELETE FROM schedule_holidays WHERE schedule_id = ?", (schedule_id,))
            # Unlink from any templates
            conn.execute("DELETE FROM template_schedule_links WHERE schedule_id = ?", (schedule_id,))
            # Delete the schedule
            conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            conn.commit()

    def add_schedule_holiday(self, schedule_id: int, name: str, date: str,
                             is_recurring: bool, created_by: str,
                             audio_id: int = None, recurrence: str = 'once',
                             day_of_week: int = None, start_time: str = None,
                             end_time: str = None, action: str = None,
                             forward_to: str = None) -> int:
        """Add a holiday/closure to a schedule.

        Args:
            schedule_id: The schedule to add the holiday to
            name: Holiday name (e.g., "Christmas Day", "Saturday Showroom")
            date: Date string - "2024-12-25" for specific date (NULL for weekly)
            is_recurring: Legacy field - use recurrence instead
            created_by: Audit trail
            audio_id: Optional FK to audio_files for closure-specific message
            recurrence: 'once' (specific date) or 'weekly' (repeats)
            day_of_week: 0-6 (Mon-Sun) for weekly recurrence
            start_time: HH:MM for time-based closures (NULL = all day)
            end_time: HH:MM for time-based closures
            action: 'message', 'voicemail', 'forward' (NULL = use schedule default)
            forward_to: Phone number for forward action
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO schedule_holidays
                    (schedule_id, name, date, is_recurring, audio_id, recurrence,
                     day_of_week, start_time, end_time, action, forward_to, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (schedule_id, name, date, 1 if is_recurring else 0, audio_id,
                  recurrence, day_of_week, start_time, end_time, action, forward_to, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_schedule_holiday(self, holiday_id: int, data: dict, updated_by: str) -> None:
        """Update a holiday/closure.

        Args:
            holiday_id: ID of the holiday to update
            data: Dict with fields to update (name, date, audio_id, recurrence,
                  day_of_week, start_time, end_time, action, forward_to)
            updated_by: Audit trail
        """
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE schedule_holidays
                SET name = ?, date = ?, audio_id = ?, recurrence = ?,
                    day_of_week = ?, start_time = ?, end_time = ?, action = ?, forward_to = ?
                WHERE id = ?
            """, (data['name'], data.get('date'), data.get('audio_id'),
                  data.get('recurrence', 'once'), data.get('day_of_week'),
                  data.get('start_time'), data.get('end_time'), data.get('action'),
                  data.get('forward_to'), holiday_id))
            conn.commit()

    def get_schedule_holiday(self, holiday_id: int) -> dict | None:
        """Get a single holiday/closure by ID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT sh.*, af.name as audio_name, af.file_url as audio_url
                FROM schedule_holidays sh
                LEFT JOIN audio_files af ON sh.audio_id = af.id
                WHERE sh.id = ?
            """, (holiday_id,)).fetchone()
            return dict(row) if row else None

    def remove_schedule_holiday(self, holiday_id: int) -> None:
        """Remove a holiday from a schedule."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM schedule_holidays WHERE id = ?", (holiday_id,))
            conn.commit()

    def get_effective_closure_settings(self, closure: dict, schedule: dict) -> dict:
        """Get effective settings for a closure, applying schedule defaults.

        Returns a dict with:
            action: The effective action (closure's or schedule default)
            audio_id: The effective audio_id
            audio_name: The effective audio name
            forward_to: The effective forward_to
            uses_defaults: True if any settings come from schedule defaults

        Args:
            closure: The closure/holiday dict
            schedule: The schedule dict (must include default_closure_* fields)
        """
        # Determine effective action
        effective_action = closure.get('action')
        if not effective_action:
            effective_action = schedule.get('default_closure_action')

        # Determine effective audio
        effective_audio_id = closure.get('audio_id')
        effective_audio_name = closure.get('audio_name')
        if not effective_audio_id and effective_action in ('message', 'voicemail'):
            effective_audio_id = schedule.get('default_closure_audio_id')
            # Need to look up audio name if using schedule default
            if effective_audio_id:
                audio = self.get_audio_file(effective_audio_id)
                if audio:
                    effective_audio_name = audio.get('name')

        # Determine effective forward_to
        effective_forward_to = closure.get('forward_to')
        if not effective_forward_to and effective_action == 'forward':
            effective_forward_to = schedule.get('default_closure_forward_to')

        # Check if any settings came from defaults
        uses_defaults = (
            (not closure.get('action') and schedule.get('default_closure_action')) or
            (not closure.get('audio_id') and schedule.get('default_closure_audio_id')) or
            (not closure.get('forward_to') and effective_action == 'forward'
             and schedule.get('default_closure_forward_to'))
        )

        return {
            'action': effective_action,
            'audio_id': effective_audio_id,
            'audio_name': effective_audio_name,
            'forward_to': effective_forward_to,
            'uses_defaults': uses_defaults,
        }

    # =========================================================================
    # Holiday Templates
    # =========================================================================

    def get_holiday_templates(self) -> list[dict]:
        """Get all holiday templates with their items and linked schedules."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM holiday_templates WHERE is_active = 1
                ORDER BY name
            """).fetchall()
            templates = [dict(row) for row in rows]

            # Load items and linked schedules for each template
            for template in templates:
                items = conn.execute("""
                    SELECT * FROM holiday_template_items
                    WHERE template_id = ?
                    ORDER BY date
                """, (template['id'],)).fetchall()
                template['items'] = [dict(item) for item in items]

                # Get linked schedules
                linked = conn.execute("""
                    SELECT s.id, s.name FROM schedules s
                    JOIN template_schedule_links tsl ON s.id = tsl.schedule_id
                    WHERE tsl.template_id = ?
                    ORDER BY s.name
                """, (template['id'],)).fetchall()
                template['linked_schedules'] = [dict(s) for s in linked]

            return templates

    def get_holiday_template(self, template_id: int) -> dict | None:
        """Get a holiday template by ID with its items and linked schedules."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM holiday_templates WHERE id = ?
            """, (template_id,)).fetchone()
            if not row:
                return None
            template = dict(row)

            items = conn.execute("""
                SELECT * FROM holiday_template_items
                WHERE template_id = ?
                ORDER BY date
            """, (template_id,)).fetchall()
            template['items'] = [dict(item) for item in items]

            # Get linked schedules
            linked = conn.execute("""
                SELECT s.id, s.name FROM schedules s
                JOIN template_schedule_links tsl ON s.id = tsl.schedule_id
                WHERE tsl.template_id = ?
                ORDER BY s.name
            """, (template_id,)).fetchall()
            template['linked_schedules'] = [dict(s) for s in linked]

            return template

    def create_holiday_template(self, name: str, description: str, created_by: str,
                                 source_url: str = None, data_as_at: str = None) -> int:
        """Create a new holiday template.

        Args:
            name: Template name
            description: Optional description
            created_by: Actor creating the template
            source_url: URL of the source document (e.g., government holiday list)
            data_as_at: Date when the source data was accurate (YYYY-MM-DD)
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO holiday_templates (name, description, source_url, data_as_at,
                                              created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, description, source_url, data_as_at, now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_holiday_template(self, template_id: int, name: str, description: str, updated_by: str,
                                source_url: str = None, data_as_at: str = None) -> None:
        """Update a holiday template.

        Args:
            template_id: ID of template to update
            name: New name
            description: New description
            updated_by: Actor updating the template
            source_url: URL of the source document
            data_as_at: Date when the source data was accurate (YYYY-MM-DD)
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE holiday_templates
                SET name = ?, description = ?, source_url = ?, data_as_at = ?,
                    updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (name, description, source_url, data_as_at, now, updated_by, template_id))
            conn.commit()

    def delete_holiday_template(self, template_id: int) -> None:
        """Delete a holiday template and its items."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM holiday_template_items WHERE template_id = ?", (template_id,))
            conn.execute("DELETE FROM holiday_templates WHERE id = ?", (template_id,))
            conn.commit()

    def clone_holiday_template(self, template_id: int, new_name: str, created_by: str) -> int:
        """Clone a holiday template with all its items.

        Args:
            template_id: The ID of the template to clone
            new_name: Name for the cloned template
            created_by: Actor creating the clone

        Returns:
            The ID of the new cloned template
        """
        template = self.get_holiday_template(template_id)
        if not template:
            raise ValueError(f"Template {template_id} not found")

        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            # Create the new template (copy source_url and data_as_at)
            cursor = conn.execute("""
                INSERT INTO holiday_templates (name, description, source_url, data_as_at,
                                              created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (new_name, template.get('description'), template.get('source_url'),
                  template.get('data_as_at'), now, created_by, now, created_by))
            new_template_id = cursor.lastrowid

            # Clone all items
            for item in template.get('items', []):
                conn.execute("""
                    INSERT INTO holiday_template_items (template_id, name, date, is_recurring, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (new_template_id, item['name'], item['date'], item['is_recurring'], now, created_by))

            conn.commit()
            return new_template_id

    def clone_schedule(self, schedule_id: int, new_name: str, created_by: str) -> int:
        """Clone a schedule with its business hours, closure defaults, and holidays.

        Args:
            schedule_id: The ID of the schedule to clone
            new_name: Name for the cloned schedule
            created_by: Actor creating the clone

        Returns:
            The ID of the new cloned schedule
        """
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            raise ValueError(f"Schedule {schedule_id} not found")

        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            # Create the new schedule (copy description, timezone, business_hours, closure defaults)
            cursor = conn.execute("""
                INSERT INTO schedules (name, description, timezone, business_hours,
                                      default_closure_action, default_closure_audio_id,
                                      default_closure_forward_to,
                                      created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (new_name, schedule.get('description'), schedule.get('timezone'),
                  schedule.get('business_hours'),
                  schedule.get('default_closure_action'), schedule.get('default_closure_audio_id'),
                  schedule.get('default_closure_forward_to'),
                  now, created_by, now, created_by))
            new_schedule_id = cursor.lastrowid

            # Clone all holidays
            for holiday in schedule.get('holidays', []):
                conn.execute("""
                    INSERT INTO schedule_holidays (schedule_id, name, date, audio_id, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (new_schedule_id, holiday['name'], holiday['date'],
                      holiday.get('audio_id'), now, created_by))

            conn.commit()
            return new_schedule_id

    def add_template_item(self, template_id: int, name: str, date: str,
                          is_recurring: bool, created_by: str) -> int:
        """Add a holiday item to a template."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO holiday_template_items (template_id, name, date, is_recurring, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (template_id, name, date, 1 if is_recurring else 0, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def remove_template_item(self, item_id: int) -> None:
        """Remove a holiday item from a template."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM holiday_template_items WHERE id = ?", (item_id,))
            conn.commit()

    def update_template_item(self, item_id: int, name: str, date: str, updated_by: str) -> None:
        """Update a holiday item in a template."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE holiday_template_items
                SET name = ?, date = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (name, date, now, updated_by, item_id))
            conn.commit()

    # =========================================================================
    # Template-Schedule Links
    # =========================================================================

    def link_template_to_schedule(self, template_id: int, schedule_id: int, created_by: str) -> bool:
        """Link a template to a schedule.

        Returns:
            True if link was created, False if already existed
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO template_schedule_links (template_id, schedule_id, created_at, created_by)
                    VALUES (?, ?, ?, ?)
                """, (template_id, schedule_id, now, created_by))
                conn.commit()
                return True
            except Exception:
                # Already linked (UNIQUE constraint)
                return False

    def unlink_template_from_schedule(self, template_id: int, schedule_id: int) -> bool:
        """Remove a link between a template and a schedule.

        Returns:
            True if link was removed, False if didn't exist
        """
        with self._get_conn() as conn:
            cursor = conn.execute("""
                DELETE FROM template_schedule_links
                WHERE template_id = ? AND schedule_id = ?
            """, (template_id, schedule_id))
            conn.commit()
            return cursor.rowcount > 0

    def get_linked_schedules(self, template_id: int) -> list[dict]:
        """Get all schedules linked to a template."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT s.*, tsl.created_at as linked_at
                FROM schedules s
                JOIN template_schedule_links tsl ON s.id = tsl.schedule_id
                WHERE tsl.template_id = ?
                ORDER BY s.name
            """, (template_id,)).fetchall()
            return [dict(row) for row in rows]

    def get_linked_template_ids(self, schedule_id: int) -> list[int]:
        """Get IDs of all templates linked to a schedule."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT template_id FROM template_schedule_links
                WHERE schedule_id = ?
            """, (schedule_id,)).fetchall()
            return [row['template_id'] for row in rows]

    def get_unlinked_schedules(self, template_id: int) -> list[dict]:
        """Get all schedules NOT linked to a template."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT s.* FROM schedules s
                WHERE s.is_active = 1
                AND s.id NOT IN (
                    SELECT schedule_id FROM template_schedule_links
                    WHERE template_id = ?
                )
                ORDER BY s.name
            """, (template_id,)).fetchall()
            return [dict(row) for row in rows]

    def get_template_sync_preview(self, template_id: int, schedule_ids: list[int] = None) -> dict:
        """Preview what would happen if we sync a template to schedules.

        If no schedule_ids provided, uses only linked schedules (not all schedules).

        Returns:
            {
                'template': {...},
                'schedules': [
                    {
                        'id': 1,
                        'name': 'Canberra Store',
                        'status': 'ok' | 'missing' | 'extra',
                        'missing': [{'name': 'Easter Monday', 'date': '...'}],
                        'matched': [{'name': 'Christmas', ...}],
                        'extra': [{'name': 'Office Closed', ...}]  # ad-hoc, not in template
                    }
                ],
                'has_linked_schedules': bool  # True if template has linked schedules
            }
        """
        template = self.get_holiday_template(template_id)
        if not template:
            return {'error': 'Template not found'}

        with self._get_conn() as conn:
            # Get schedules to check - default to linked schedules only
            if schedule_ids:
                placeholders = ','.join('?' * len(schedule_ids))
                schedules = conn.execute(f"""
                    SELECT * FROM schedules WHERE id IN ({placeholders}) AND is_active = 1
                """, schedule_ids).fetchall()
                has_linked_schedules = True  # Explicit selection
            else:
                # Use only linked schedules
                schedules = conn.execute("""
                    SELECT s.* FROM schedules s
                    JOIN template_schedule_links tsl ON s.id = tsl.schedule_id
                    WHERE tsl.template_id = ? AND s.is_active = 1
                    ORDER BY s.name
                """, (template_id,)).fetchall()
                has_linked_schedules = len(schedules) > 0

            results = []
            for schedule in schedules:
                schedule_dict = dict(schedule)

                # Get this schedule's holidays
                holidays = conn.execute("""
                    SELECT * FROM schedule_holidays WHERE schedule_id = ?
                """, (schedule['id'],)).fetchall()
                holidays = [dict(h) for h in holidays]

                # Build lookup by (name, date) for matching
                schedule_holiday_keys = {(h['name'], h['date']): h for h in holidays}
                template_item_keys = {(i['name'], i['date']): i for i in template['items']}

                missing = []
                matched = []
                extra = []

                # Check which template items are missing from schedule
                for item in template['items']:
                    key = (item['name'], item['date'])
                    if key in schedule_holiday_keys:
                        matched.append(item)
                    else:
                        missing.append(item)

                # Check which schedule holidays are not in template (ad-hoc)
                for holiday in holidays:
                    key = (holiday['name'], holiday['date'])
                    if key not in template_item_keys:
                        extra.append(holiday)

                # Determine overall status
                if missing:
                    status = 'missing'
                elif extra:
                    status = 'has_extra'  # All template items present, but has additional
                else:
                    status = 'ok'

                results.append({
                    'id': schedule_dict['id'],
                    'name': schedule_dict['name'],
                    'status': status,
                    'missing': missing,
                    'matched': matched,
                    'extra': extra,
                })

            return {
                'template': template,
                'schedules': results,
                'has_linked_schedules': has_linked_schedules,
            }

    def apply_template_to_schedules(self, template_id: int, schedule_ids: list[int],
                                     created_by: str) -> dict:
        """Apply a template to schedules - adds missing holidays.

        Returns:
            {
                'added': [{'schedule': 'Canberra', 'holiday': 'Easter Monday'}, ...],
                'skipped': [{'schedule': 'Sydney', 'holiday': 'Christmas', 'reason': 'already exists'}, ...]
            }
        """
        template = self.get_holiday_template(template_id)
        if not template:
            return {'error': 'Template not found'}

        now = datetime.now(timezone.utc).isoformat()
        added = []
        skipped = []

        with self._get_conn() as conn:
            for schedule_id in schedule_ids:
                # Get schedule name
                schedule = conn.execute(
                    "SELECT name FROM schedules WHERE id = ?", (schedule_id,)
                ).fetchone()
                if not schedule:
                    continue
                schedule_name = schedule['name']

                # Get existing holidays for this schedule
                existing = conn.execute("""
                    SELECT name, date FROM schedule_holidays WHERE schedule_id = ?
                """, (schedule_id,)).fetchall()
                existing_keys = {(row['name'], row['date']) for row in existing}

                # Add missing template items
                for item in template['items']:
                    key = (item['name'], item['date'])
                    if key in existing_keys:
                        skipped.append({
                            'schedule': schedule_name,
                            'holiday': item['name'],
                            'reason': 'already exists'
                        })
                    else:
                        conn.execute("""
                            INSERT INTO schedule_holidays
                                (schedule_id, name, date, is_recurring, template_item_id, created_at, created_by)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (schedule_id, item['name'], item['date'],
                              item['is_recurring'], item['id'], now, created_by))
                        added.append({
                            'schedule': schedule_name,
                            'holiday': item['name']
                        })

            conn.commit()

        return {'added': added, 'skipped': skipped}

    # =========================================================================
    # Queues
    # =========================================================================

    def get_queues(self) -> list[dict]:
        """Get all queues with member counts."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT q.*,
                       (SELECT COUNT(*) FROM queue_members qm
                        WHERE qm.queue_id = q.id AND qm.is_active = 1) as member_count
                FROM queues q
                WHERE q.is_active = 1
                ORDER BY q.name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_queue(self, queue_id: int) -> dict | None:
        """Get a queue by ID with its members."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM queues WHERE id = ?
            """, (queue_id,)).fetchone()
            if not row:
                return None
            queue = dict(row)

            # Get members
            members = conn.execute("""
                SELECT * FROM queue_members WHERE queue_id = ? AND is_active = 1
                ORDER BY priority DESC, user_email
            """, (queue_id,)).fetchall()
            queue['members'] = [dict(m) for m in members]
            return queue

    def create_queue(self, data: dict, created_by: str) -> int:
        """Create a new queue."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO queues (name, description, hold_music_id, position_announcement,
                                    announcement_interval, ring_strategy, ring_timeout,
                                    offer_callback, callback_threshold, allow_self_service,
                                    reject_action, allow_voicemail_escape,
                                    welcome_audio_id, callback_reminder_audio_id,
                                    escape_announcement_delay, escape_repeat_interval,
                                    created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (data['name'], data.get('description'), data.get('hold_music_id'),
                  data.get('position_announcement', 1), data.get('announcement_interval', 60),
                  data.get('ring_strategy', 'simultaneous'), data.get('ring_timeout', 30),
                  data.get('offer_callback', 0), data.get('callback_threshold', 60),
                  1 if data.get('allow_self_service') else 0,
                  data.get('reject_action', 'continue'),
                  1 if data.get('allow_voicemail_escape') else 0,
                  data.get('welcome_audio_id'), data.get('callback_reminder_audio_id'),
                  data.get('escape_announcement_delay', 60),
                  data.get('escape_repeat_interval', 120),
                  now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_queue(self, queue_id: int, data: dict, updated_by: str) -> None:
        """Update a queue."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE queues
                SET name = ?, description = ?, hold_music_id = ?, position_announcement = ?,
                    announcement_interval = ?, ring_strategy = ?, ring_timeout = ?,
                    offer_callback = ?, callback_threshold = ?, allow_self_service = ?,
                    reject_action = ?, allow_voicemail_escape = ?,
                    welcome_audio_id = ?, callback_reminder_audio_id = ?,
                    escape_announcement_delay = ?, escape_repeat_interval = ?,
                    max_wait_time = ?,
                    updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (data['name'], data.get('description'), data.get('hold_music_id'),
                  data.get('position_announcement', 1), data.get('announcement_interval', 60),
                  data.get('ring_strategy', 'simultaneous'), data.get('ring_timeout', 30),
                  data.get('offer_callback', 0), data.get('callback_threshold', 60),
                  1 if data.get('allow_self_service') else 0,
                  data.get('reject_action', 'continue'),
                  1 if data.get('allow_voicemail_escape') else 0,
                  data.get('welcome_audio_id'), data.get('callback_reminder_audio_id'),
                  data.get('escape_announcement_delay', 60),
                  data.get('escape_repeat_interval', 120),
                  data.get('max_wait_time'),
                  now, updated_by, queue_id))
            conn.commit()

    # =========================================================================
    # Queue Managers
    # =========================================================================

    def get_queue_managers(self, queue_id: int) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM queue_managers WHERE queue_id = ? ORDER BY user_email
            """, (queue_id,)).fetchall()
            return [dict(r) for r in rows]

    def add_queue_manager(self, queue_id: int, user_email: str, created_by: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO queue_managers (queue_id, user_email, created_at, created_by)
                VALUES (?, ?, ?, ?)
            """, (queue_id, user_email.lower(), now, created_by))
            conn.commit()

    def remove_queue_manager(self, queue_id: int, user_email: str) -> None:
        with self._get_conn() as conn:
            conn.execute("""
                DELETE FROM queue_managers WHERE queue_id = ? AND user_email = ?
            """, (queue_id, user_email.lower()))
            conn.commit()

    def get_queues_for_manager(self, user_email: str) -> list[dict]:
        """Get all queues a user can manage (via queue_managers), with pause status."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT q.*
                FROM queues q
                JOIN queue_managers qm ON q.id = qm.queue_id
                WHERE qm.user_email = ? AND q.is_active = 1
                ORDER BY q.name
            """, (user_email.lower(),)).fetchall()
            return [dict(r) for r in rows]

    def is_queue_manager(self, queue_id: int, user_email: str) -> bool:
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT 1 FROM queue_managers WHERE queue_id = ? AND user_email = ?
            """, (queue_id, user_email.lower())).fetchone()
            return row is not None

    def schedule_queue_pause(self, queue_id: int, paused_from: str, paused_until: str, updated_by: str) -> None:
        """Set a scheduled pause window on a queue (UTC ISO strings)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE queues SET paused_from = ?, paused_until = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (paused_from, paused_until, now, updated_by, queue_id))
            conn.commit()

    def clear_queue_pause(self, queue_id: int, updated_by: str) -> None:
        """Remove any scheduled pause window from a queue."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE queues SET paused_from = NULL, paused_until = NULL, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (now, updated_by, queue_id))
            conn.commit()

    def delete_queue(self, queue_id: int) -> None:
        """Delete a queue and its members."""
        with self._get_conn() as conn:
            # Delete members first
            conn.execute("DELETE FROM queue_members WHERE queue_id = ?", (queue_id,))
            # Delete the queue
            conn.execute("DELETE FROM queues WHERE id = ?", (queue_id,))
            conn.commit()

    # =========================================================================
    # Queue Members
    # =========================================================================

    def get_queues_for_user(self, email: str) -> list[dict]:
        """Get all queues that a user is an active member of."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT q.id, q.name FROM queues q
                JOIN queue_members qm ON q.id = qm.queue_id
                WHERE qm.user_email = ? AND qm.is_active = 1 AND q.is_active = 1
                ORDER BY q.name
            """, (email.lower(),)).fetchall()
            return [dict(row) for row in rows]

    def get_recent_answered_queued_calls(self, limit: int = 10) -> list[dict]:
        """Get recently answered queued calls (for conference lookup)."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM queued_calls
                WHERE status = 'answered' AND conference_name IS NOT NULL
                ORDER BY enqueued_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_queue_members(self, queue_id: int) -> list[dict]:
        """Get all active members of a queue."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM queue_members
                WHERE queue_id = ? AND is_active = 1
                ORDER BY priority DESC, user_email
            """, (queue_id,)).fetchall()
            return [dict(row) for row in rows]

    def add_queue_member(self, queue_id: int, user_email: str, priority: int,
                         created_by: str) -> int:
        """Add a member to a queue."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO queue_members (queue_id, user_email, priority,
                                           created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (queue_id, user_email.lower(), priority, now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def remove_queue_member(self, member_id: int) -> None:
        """Remove a member from a queue."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM queue_members WHERE id = ?", (member_id,))
            conn.commit()

    def set_queue_member_active(self, member_id: int, is_active: bool, updated_by: str) -> None:
        """Set a queue member's active status (e.g., on break)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE queue_members
                SET is_active = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (1 if is_active else 0, now, updated_by, member_id))
            conn.commit()

    def get_all_queue_members(self) -> list[dict]:
        """Get all unique queue members across all queues.

        Returns distinct user_email entries from any queue.
        Used for building transfer target lists.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT DISTINCT user_email
                FROM queue_members
                WHERE is_active = 1
                ORDER BY user_email
            """).fetchall()
            return [dict(row) for row in rows]

    # =========================================================================
    # User Ring Settings
    # =========================================================================

    def update_user_ring_settings(self, email: str, ring_browser: bool, ring_sip: bool,
                                   updated_by: str) -> bool:
        """Update a user's ring settings.

        Args:
            email: User's email address
            ring_browser: Whether to ring browser softphone
            ring_sip: Whether to ring SIP devices (desk phone, Zoiper, etc.)
            updated_by: Who made the change

        Returns:
            True if updated, False if user not found
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                UPDATE users
                SET ring_browser = ?, ring_sip = ?, updated_at = ?, updated_by = ?
                WHERE staff_email = ?
            """, (1 if ring_browser else 0, 1 if ring_sip else 0, now, updated_by, email.lower()))
            conn.commit()
            return cursor.rowcount > 0

    def get_user_ring_settings(self, email: str) -> dict:
        """Get a user's ring settings.

        Returns dict with ring_browser and ring_sip booleans.
        Defaults to True for both if user not found.
        DND overrides everything — returns False for all if DND is enabled.
        """
        # Check DND first — if enabled, nothing should ring
        ext = self.get_staff_extension(email)
        if ext and ext.get('dnd_enabled'):
            return {'ring_browser': False, 'ring_sip': False, 'dnd': True}

        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT ring_browser, ring_sip FROM users WHERE staff_email = ?
            """, (email.lower(),)).fetchone()
            if row:
                return {
                    'ring_browser': bool(row['ring_browser']) if row['ring_browser'] is not None else True,
                    'ring_sip': bool(row['ring_sip']) if row['ring_sip'] is not None else True,
                }
            return {'ring_browser': True, 'ring_sip': True}

    # =========================================================================
    # Call Flows
    # =========================================================================

    def get_call_flows(self) -> list[dict]:
        """Get all call flows."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT cf.*,
                       s.name as schedule_name,
                       q.name as queue_name
                FROM call_flows cf
                LEFT JOIN schedules s ON cf.schedule_id = s.id
                LEFT JOIN queues q ON cf.open_queue_id = q.id
                WHERE cf.is_active = 1
                ORDER BY cf.name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_call_flow(self, flow_id: int) -> dict | None:
        """Get a call flow by ID with related objects."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT cf.*,
                       s.name as schedule_name,
                       q.name as queue_name,
                       ga.name as greeting_name, ga.file_url as greeting_url,
                       ca.name as closed_audio_name, ca.file_url as closed_audio_url,
                       epa.name as extension_prompt_audio_name, epa.file_url as extension_prompt_audio_url
                FROM call_flows cf
                LEFT JOIN schedules s ON cf.schedule_id = s.id
                LEFT JOIN queues q ON cf.open_queue_id = q.id
                LEFT JOIN audio_files ga ON cf.greeting_audio_id = ga.id
                LEFT JOIN audio_files ca ON cf.closed_audio_id = ca.id
                LEFT JOIN audio_files epa ON cf.extension_prompt_audio_id = epa.id
                WHERE cf.id = ?
            """, (flow_id,)).fetchone()
            return dict(row) if row else None

    def create_call_flow(self, data: dict, created_by: str) -> int:
        """Create a new call flow."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO call_flows (name, description, greeting_audio_id, schedule_id,
                                        open_action, open_queue_id, open_forward_number, open_audio_id,
                                        closed_action, closed_audio_id, closed_forward_number,
                                        closed_message_parts,
                                        voicemail_email, voicemail_destination_id,
                                        extension_prompt_audio_id, extension_no_answer_action,
                                        extension_invalid_audio_id,
                                        created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (data['name'], data.get('description'), data.get('greeting_audio_id'),
                  data.get('schedule_id'), data.get('open_action', 'queue'),
                  data.get('open_queue_id'), data.get('open_forward_number'), data.get('open_audio_id'),
                  data.get('closed_action', 'message'), data.get('closed_audio_id'),
                  data.get('closed_forward_number'),
                  data.get('closed_message_parts'),
                  data.get('voicemail_email'),
                  data.get('voicemail_destination_id'),
                  data.get('extension_prompt_audio_id'), data.get('extension_no_answer_action', 'voicemail'),
                  data.get('extension_invalid_audio_id'),
                  now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_call_flow(self, flow_id: int, data: dict, updated_by: str) -> None:
        """Update a call flow."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE call_flows
                SET name = ?, description = ?, greeting_audio_id = ?, schedule_id = ?,
                    open_action = ?, open_queue_id = ?, open_forward_number = ?, open_audio_id = ?,
                    open_no_answer_action = ?, no_answer_audio_id = ?,
                    closed_action = ?, closed_audio_id = ?, closed_forward_number = ?,
                    closed_message_parts = ?,
                    voicemail_email = ?, voicemail_destination_id = ?,
                    extension_prompt_audio_id = ?, extension_no_answer_action = ?,
                    extension_invalid_audio_id = ?,
                    updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (data['name'], data.get('description'), data.get('greeting_audio_id'),
                  data.get('schedule_id'), data.get('open_action', 'queue'),
                  data.get('open_queue_id'), data.get('open_forward_number'), data.get('open_audio_id'),
                  data.get('open_no_answer_action', 'ai_receptionist'),
                  data.get('no_answer_audio_id'),
                  data.get('closed_action', 'message'), data.get('closed_audio_id'),
                  data.get('closed_forward_number'),
                  data.get('closed_message_parts'),
                  data.get('voicemail_email'),
                  data.get('voicemail_destination_id'),
                  data.get('extension_prompt_audio_id'), data.get('extension_no_answer_action', 'voicemail'),
                  data.get('extension_invalid_audio_id'),
                  now, updated_by, flow_id))
            conn.commit()

    def delete_call_flow(self, flow_id: int) -> bool:
        """Delete a call flow.

        Returns True if deleted, False if call flow is still assigned to phone numbers.
        """
        with self._get_conn() as conn:
            # Check if any phone numbers use this call flow
            count = conn.execute(
                "SELECT COUNT(*) FROM phone_numbers WHERE call_flow_id = ?",
                (flow_id,)
            ).fetchone()[0]
            if count > 0:
                return False

            conn.execute("DELETE FROM call_flows WHERE id = ?", (flow_id,))
            conn.commit()
            return True

    def set_phone_number_call_flow(self, phone_sid: str, call_flow_id: int | None,
                                    updated_by: str) -> None:
        """Set the call flow for a phone number."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE phone_numbers
                SET call_flow_id = ?, updated_at = ?, updated_by = ?
                WHERE sid = ?
            """, (call_flow_id, now, updated_by, phone_sid))
            conn.commit()

    # =========================================================================
    # Call Routing - Full lookup for incoming calls
    # =========================================================================

    def get_call_routing(self, phone_number: str) -> dict | None:
        """Get full call routing info for an incoming call.

        Returns a dict with:
        - phone: The phone number record
        - call_flow: The call flow (if assigned)
        - schedule: The schedule with holidays (if assigned)
        - queue: The queue with members (if open_action = 'queue')
        - user_settings: Dict of user_email -> {ring_browser, ring_sip}
        """
        with self._get_conn() as conn:
            # Get phone number with call flow
            phone_row = conn.execute("""
                SELECT pn.*, cf.id as flow_id, cf.name as flow_name,
                       cf.greeting_audio_id, cf.schedule_id,
                       cf.open_action, cf.open_queue_id, cf.open_forward_number,
                       cf.open_no_answer_action, cf.no_answer_audio_id,
                       cf.closed_action, cf.closed_audio_id, cf.closed_forward_number,
                       cf.voicemail_email, cf.voicemail_destination_id,
                       cf.extension_prompt_audio_id, cf.extension_no_answer_action,
                       ga.file_url as greeting_url
                FROM phone_numbers pn
                LEFT JOIN call_flows cf ON pn.call_flow_id = cf.id
                LEFT JOIN audio_files ga ON cf.greeting_audio_id = ga.id
                WHERE pn.phone_number = ?
            """, (phone_number,)).fetchone()

            if not phone_row:
                return None

            result = {
                'phone': {
                    'sid': phone_row['sid'],
                    'phone_number': phone_row['phone_number'],
                    'friendly_name': phone_row['friendly_name'],
                    'forward_to': phone_row['forward_to'],  # Legacy fallback
                },
                'call_flow': None,
                'schedule': None,
                'queue': None,
                'user_settings': {},
            }

            # If no call flow, use phone_assignments for routing
            if not phone_row['flow_id']:
                # Load assigned receivers
                receivers = conn.execute("""
                    SELECT staff_email FROM phone_assignments
                    WHERE phone_number_sid = ? AND can_receive = 1
                """, (phone_row['sid'],)).fetchall()

                if receivers:
                    result['assignments'] = [row['staff_email'] for row in receivers]

                    # Load ring settings for each assigned user
                    for row in receivers:
                        email = row['staff_email']
                        user = conn.execute("""
                            SELECT ring_browser, ring_sip FROM users
                            WHERE staff_email = ?
                        """, (email,)).fetchone()
                        if user:
                            result['user_settings'][email] = {
                                'ring_browser': bool(user['ring_browser']),
                                'ring_sip': bool(user['ring_sip']),
                            }
                        else:
                            result['user_settings'][email] = {
                                'ring_browser': True,
                                'ring_sip': True,
                            }

                return result

            result['call_flow'] = {
                'id': phone_row['flow_id'],
                'name': phone_row['flow_name'],
                'greeting_audio_id': phone_row['greeting_audio_id'],
                'greeting_url': phone_row['greeting_url'],
                'open_action': phone_row['open_action'],
                'open_queue_id': phone_row['open_queue_id'],
                'open_forward_number': phone_row['open_forward_number'],
                'open_no_answer_action': phone_row['open_no_answer_action'],
                'no_answer_audio_id': phone_row['no_answer_audio_id'],
                'closed_action': phone_row['closed_action'],
                'closed_audio_id': phone_row['closed_audio_id'],
                'closed_forward_number': phone_row['closed_forward_number'],
                'voicemail_email': phone_row['voicemail_email'],
                'voicemail_destination_id': phone_row['voicemail_destination_id'],
                'extension_prompt_audio_id': phone_row['extension_prompt_audio_id'],
                'extension_no_answer_action': phone_row['extension_no_answer_action'],
            }

            # Get schedule if assigned
            if phone_row['schedule_id']:
                schedule_row = conn.execute("""
                    SELECT * FROM schedules WHERE id = ?
                """, (phone_row['schedule_id'],)).fetchone()
                if schedule_row:
                    schedule = dict(schedule_row)
                    # Get holidays with audio info for holiday-specific messages
                    holidays = conn.execute("""
                        SELECT sh.*, af.file_url as audio_url
                        FROM schedule_holidays sh
                        LEFT JOIN audio_files af ON sh.audio_id = af.id
                        WHERE sh.schedule_id = ?
                    """, (phone_row['schedule_id'],)).fetchall()
                    schedule['holidays'] = [dict(h) for h in holidays]
                    result['schedule'] = schedule

            # Get queue if open_action = 'queue'
            if phone_row['open_action'] == 'queue' and phone_row['open_queue_id']:
                queue_row = conn.execute("""
                    SELECT q.*, hm.file_url as hold_music_url
                    FROM queues q
                    LEFT JOIN audio_files hm ON q.hold_music_id = hm.id
                    WHERE q.id = ?
                """, (phone_row['open_queue_id'],)).fetchone()
                if queue_row:
                    queue = dict(queue_row)
                    members = conn.execute("""
                        SELECT * FROM queue_members
                        WHERE queue_id = ? AND is_active = 1
                        ORDER BY priority DESC
                    """, (phone_row['open_queue_id'],)).fetchall()
                    queue['members'] = [dict(m) for m in members]
                    result['queue'] = queue

                    # Get ring settings for all queue members
                    for member in members:
                        email = member['user_email']
                        user = conn.execute("""
                            SELECT ring_browser, ring_sip FROM users
                            WHERE staff_email = ?
                        """, (email,)).fetchone()
                        if user:
                            result['user_settings'][email] = {
                                'ring_browser': bool(user['ring_browser']),
                                'ring_sip': bool(user['ring_sip']),
                            }
                        else:
                            # Default: ring everything if user not found
                            result['user_settings'][email] = {
                                'ring_browser': True,
                                'ring_sip': True,
                            }

            return result

    # =========================================================================
    # Callback Requests
    # =========================================================================

    def create_callback_request(self, queue_id: int, customer_phone: str,
                                 customer_name: str = None, call_sid: str = None) -> int:
        """Create a new callback request."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO callback_requests (queue_id, customer_phone, customer_name,
                                               original_call_sid, requested_at)
                VALUES (?, ?, ?, ?, ?)
            """, (queue_id, customer_phone, customer_name, call_sid, now))
            conn.commit()
            return cursor.lastrowid

    def get_pending_callbacks(self, queue_id: int = None) -> list[dict]:
        """Get pending callback requests."""
        with self._get_conn() as conn:
            if queue_id:
                rows = conn.execute("""
                    SELECT cr.*, q.name as queue_name
                    FROM callback_requests cr
                    JOIN queues q ON cr.queue_id = q.id
                    WHERE cr.status = 'pending' AND cr.queue_id = ?
                    ORDER BY cr.priority DESC, cr.requested_at ASC
                """, (queue_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT cr.*, q.name as queue_name
                    FROM callback_requests cr
                    JOIN queues q ON cr.queue_id = q.id
                    WHERE cr.status = 'pending'
                    ORDER BY cr.priority DESC, cr.requested_at ASC
                """).fetchall()
            return [dict(row) for row in rows]

    def claim_callback(self, callback_id: int, agent_email: str) -> bool:
        """Claim a callback request (set to in_progress)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                UPDATE callback_requests
                SET status = 'in_progress', agent_email = ?, last_attempt_at = ?,
                    attempt_count = attempt_count + 1, updated_at = ?, updated_by = ?
                WHERE id = ? AND status = 'pending'
            """, (agent_email, now, now, f"session:{agent_email}", callback_id))
            conn.commit()
            return cursor.rowcount > 0

    def complete_callback(self, callback_id: int, call_sid: str = None) -> None:
        """Mark a callback as completed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE callback_requests
                SET status = 'completed', completed_at = ?, callback_call_sid = ?, updated_at = ?
                WHERE id = ?
            """, (now, call_sid, now, callback_id))
            conn.commit()

    def fail_callback(self, callback_id: int, notes: str = None) -> None:
        """Mark a callback as failed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE callback_requests
                SET status = 'failed', notes = ?, updated_at = ?
                WHERE id = ?
            """, (notes, now, callback_id))
            conn.commit()

    # =========================================================================
    # TTS Settings
    # =========================================================================

    def get_tts_settings(self) -> dict:
        """Get all TTS settings as a dict."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT setting_key, setting_value FROM tts_settings
            """).fetchall()
            return {row['setting_key']: row['setting_value'] for row in rows}

    def get_tts_setting(self, key: str, default: str = None) -> str | None:
        """Get a single TTS setting value."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT setting_value FROM tts_settings WHERE setting_key = ?
            """, (key,)).fetchone()
            return row['setting_value'] if row else default

    def set_tts_setting(self, key: str, value: str, updated_by: str) -> None:
        """Set a TTS setting value."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO tts_settings (setting_key, setting_value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
            """, (key, value, now, updated_by))
            conn.commit()

    # =========================================================================
    # Staff Extensions
    # =========================================================================

    def get_staff_extension(self, email: str) -> dict | None:
        """Get a staff member's extension settings."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM staff_extensions WHERE email = ?
            """, (email.lower(),)).fetchone()
            return dict(row) if row else None

    def update_staff_extension_caller_id(self, email: str, caller_id: str | None, updated_by: str) -> bool:
        """Set a staff member's default caller ID for outbound calls."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                UPDATE staff_extensions
                SET default_caller_id = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (caller_id, now, updated_by, email.lower()))
            conn.commit()
            return cursor.rowcount > 0

    def get_staff_extension_by_ext(self, extension: str) -> dict | None:
        """Get a staff member by their extension number."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM staff_extensions WHERE extension = ?
            """, (extension,)).fetchone()
            return dict(row) if row else None

    def get_all_staff_extensions(self) -> list[dict]:
        """Get all staff extensions."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM staff_extensions ORDER BY email
            """).fetchall()
            return [dict(row) for row in rows]

    def get_active_staff_extensions(self) -> list[dict]:
        """Get staff extensions that are actively using Rinq."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM staff_extensions
                WHERE is_active = 1
                ORDER BY extension
            """).fetchall()
            return [dict(row) for row in rows]

    def get_visible_staff_extensions(self) -> list[dict]:
        """Get staff extensions that are visible in PAM."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM staff_extensions
                WHERE show_in_pam = 1
                ORDER BY extension
            """).fetchall()
            return [dict(row) for row in rows]

    def set_staff_extension_active(self, email: str, is_active: bool, updated_by: str) -> None:
        """Toggle whether a staff member is actively using Rinq."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET is_active = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (1 if is_active else 0, now, updated_by, email.lower()))
            conn.commit()

    def set_dnd(self, email: str, enabled: bool, updated_by: str) -> None:
        """Toggle do-not-disturb for a staff member."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET dnd_enabled = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (1 if enabled else 0, now, updated_by, email.lower()))
            conn.commit()

    def update_heartbeat(self, email: str) -> None:
        """Update the last_heartbeat timestamp for a staff member."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET last_heartbeat = ?
                WHERE email = ?
            """, (now, email.lower()))
            conn.commit()

    def get_next_extension(self) -> str:
        """Get the next available 4-digit extension number (1000+)."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT MAX(CAST(extension AS INTEGER)) as max_ext
                FROM staff_extensions
            """).fetchone()
            max_ext = row['max_ext'] if row and row['max_ext'] else 999
            return str(max(1000, max_ext + 1))

    def set_extension_number(self, email: str, new_extension: str, updated_by: str) -> dict:
        """Set a staff member's extension number. Returns dict with success/error."""
        new_extension = new_extension.strip()
        if not new_extension.isdigit() or len(new_extension) != 4:
            return {'success': False, 'error': 'Extension must be exactly 4 digits.'}
        if new_extension.startswith('0'):
            return {'success': False, 'error': 'Extension cannot start with zero.'}

        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            # Check uniqueness
            existing = conn.execute(
                "SELECT email FROM staff_extensions WHERE extension = ? AND email != ?",
                (new_extension, email.lower())
            ).fetchone()
            if existing:
                return {'success': False, 'error': f'Extension {new_extension} is already taken.'}

            conn.execute("""
                UPDATE staff_extensions
                SET extension = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (new_extension, now, updated_by, email.lower()))
            conn.commit()
        return {'success': True}

    def create_staff_extension(self, email: str, created_by: str, extension: str = None) -> dict:
        """Create a new staff extension, optionally with a specific extension number."""
        now = datetime.now(timezone.utc).isoformat()
        if extension:
            # Check it's not already taken
            with self._get_conn() as conn:
                existing = conn.execute(
                    "SELECT email FROM staff_extensions WHERE extension = ?",
                    (extension,)
                ).fetchone()
                if existing:
                    # Collision - fall back to auto-assign
                    extension = self.get_next_extension()
        else:
            extension = self.get_next_extension()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO staff_extensions (email, extension, created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (email.lower(), extension, now, created_by, now, created_by))
            conn.commit()
        return self.get_staff_extension(email)

    def update_staff_extension(self, email: str, data: dict, updated_by: str) -> None:
        """Update a staff member's extension settings."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET forward_to = ?,
                    forward_mode = ?,
                    hide_mobile = ?,
                    updated_at = ?,
                    updated_by = ?
                WHERE email = ?
            """, (
                data.get('forward_to'),
                data.get('forward_mode', 'always'),
                1 if data.get('hide_mobile') else 0,
                now,
                updated_by,
                email.lower()
            ))
            conn.commit()

    def update_staff_reports_to(self, email: str, reports_to: str | None, updated_by: str) -> bool:
        """Set who a staff member reports to (manager email)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                UPDATE staff_extensions
                SET reports_to = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (reports_to.lower().strip() if reports_to else None, now, updated_by, email.lower()))
            conn.commit()
            return cursor.rowcount > 0

    def update_staff_name_audio(self, email: str, name_audio_path: str,
                                name_audio_text: str, updated_by: str) -> None:
        """Update the name audio path and text for a staff extension."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET name_audio_path = ?, name_audio_text = ?,
                    updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (name_audio_path, name_audio_text, now, updated_by, email.lower()))
            conn.commit()

    def clear_staff_name_audio(self, email: str, updated_by: str) -> None:
        """Clear name audio for a staff extension (triggers fallback to <Say>)."""
        self.update_staff_name_audio(email, None, None, updated_by)

    def update_staff_extension_number(self, email: str, new_extension: str,
                                      updated_by: str) -> dict:
        """Update a staff member's extension number.

        Returns dict with success/error.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            # Check the new extension isn't already taken
            existing = conn.execute(
                "SELECT email FROM staff_extensions WHERE extension = ? AND email != ?",
                (new_extension, email.lower())
            ).fetchone()
            if existing:
                return {'success': False, 'error': f'Extension {new_extension} is already assigned to {existing["email"]}'}

            conn.execute("""
                UPDATE staff_extensions
                SET extension = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (new_extension, now, updated_by, email.lower()))
            conn.commit()
            return {'success': True}

    def stamp_sip_activity(self, email: str) -> None:
        """Record that a SIP device was used by this staff member.

        Called when we see a SIP call (inbound ring or outbound dial).
        Used to distinguish real desk phone users from people who just have credentials.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE staff_extensions SET sip_registered_at = ? WHERE email = ?",
                (now, email.lower())
            )
            conn.commit()

    def get_or_create_staff_extension(self, email: str, created_by: str) -> dict:
        """Get a staff member's extension, creating one if it doesn't exist."""
        ext = self.get_staff_extension(email)
        if ext is None:
            ext = self.create_staff_extension(email, created_by)
        return ext

    def get_staff_usage_signals(self) -> dict:
        """Get usage signals for all staff with extensions.

        Returns a dict keyed by email with signals:
        - has_sip_user: bool - has active SIP credentials
        - in_queue_count: int - number of queues they're in
        - has_phone_assignment: bool - has a direct number assigned
        - total_calls: int - total calls in call_log
        - last_call_at: str - timestamp of most recent call
        """
        with self._get_conn() as conn:
            # Get all staff emails from extensions
            extensions = conn.execute(
                "SELECT email FROM staff_extensions"
            ).fetchall()
            emails = [row['email'] for row in extensions]

            if not emails:
                return {}

            signals = {email: {
                'has_sip_user': False,
                'in_queue_count': 0,
                'has_phone_assignment': False,
                'total_calls': 0,
                'last_call_at': None,
            } for email in emails}

            # SIP users
            for row in conn.execute(
                "SELECT staff_email FROM users WHERE is_active = 1 AND staff_email IS NOT NULL"
            ).fetchall():
                email = row['staff_email'].lower()
                if email in signals:
                    signals[email]['has_sip_user'] = True

            # Queue memberships
            for row in conn.execute(
                "SELECT user_email, COUNT(*) as cnt FROM queue_members GROUP BY user_email"
            ).fetchall():
                email = row['user_email'].lower()
                if email in signals:
                    signals[email]['in_queue_count'] = row['cnt']

            # Phone assignments
            for row in conn.execute(
                "SELECT DISTINCT staff_email FROM phone_assignments WHERE staff_email IS NOT NULL"
            ).fetchall():
                email = row['staff_email'].lower()
                if email in signals:
                    signals[email]['has_phone_assignment'] = True

            # Call history (agent_email in call_log)
            for row in conn.execute("""
                SELECT agent_email, COUNT(*) as cnt, MAX(started_at) as last_call
                FROM call_log
                WHERE agent_email IS NOT NULL
                GROUP BY agent_email
            """).fetchall():
                email = row['agent_email'].lower()
                if email in signals:
                    signals[email]['total_calls'] = row['cnt']
                    signals[email]['last_call_at'] = row['last_call']

            return signals

    def auto_activate_staff(self, updated_by: str = 'system:auto-activate') -> list[str]:
        """Auto-activate staff who have usage signals and aren't locked.

        Activates staff where:
        - is_active_locked = 0 (not manually overridden)
        - is_active = 0 (not already active)
        - Has at least one of: call history, queue membership, or phone assignment

        Returns list of emails that were activated.
        """
        signals = self.get_staff_usage_signals()
        now = datetime.now(timezone.utc).isoformat()
        activated = []

        with self._get_conn() as conn:
            for email, sig in signals.items():
                # Check if they have strong signals
                has_signals = (
                    sig['total_calls'] > 0 or
                    sig['in_queue_count'] > 0 or
                    sig['has_phone_assignment']
                )

                if not has_signals:
                    continue

                # Check current state
                ext = conn.execute(
                    "SELECT is_active, is_active_locked FROM staff_extensions WHERE email = ?",
                    (email,)
                ).fetchone()

                if ext and not ext['is_active'] and not ext['is_active_locked']:
                    conn.execute("""
                        UPDATE staff_extensions
                        SET is_active = 1, updated_at = ?, updated_by = ?
                        WHERE email = ?
                    """, (now, updated_by, email))
                    activated.append(email)

            if activated:
                conn.commit()

        return activated

    def set_staff_extension_active_locked(self, email: str, is_active: bool,
                                          locked: bool, updated_by: str) -> None:
        """Set staff active status with lock flag.

        When locked=True, auto-activation won't change the status.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET is_active = ?, is_active_locked = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (1 if is_active else 0, 1 if locked else 0, now, updated_by, email.lower()))
            conn.commit()

    # =========================================================================
    # Self-Service Queue Membership
    # =========================================================================

    def get_self_service_queues(self) -> list[dict]:
        """Get all queues that allow self-service membership."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT q.*,
                       (SELECT COUNT(*) FROM queue_members qm
                        WHERE qm.queue_id = q.id AND qm.is_active = 1) as member_count
                FROM queues q
                WHERE q.is_active = 1 AND q.allow_self_service = 1
                ORDER BY q.name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_user_queue_memberships(self, email: str) -> list[dict]:
        """Get all queue memberships for a user."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT qm.*, q.name as queue_name, q.description as queue_description,
                       q.allow_self_service
                FROM queue_members qm
                JOIN queues q ON qm.queue_id = q.id
                WHERE qm.user_email = ? AND qm.is_active = 1 AND q.is_active = 1
                ORDER BY q.name
            """, (email.lower(),)).fetchall()
            return [dict(row) for row in rows]

    def is_user_in_queue(self, queue_id: int, email: str) -> bool:
        """Check if a user is an active member of a queue."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT 1 FROM queue_members
                WHERE queue_id = ? AND user_email = ? AND is_active = 1
            """, (queue_id, email.lower())).fetchone()
            return row is not None

    def toggle_queue_membership(self, queue_id: int, email: str, updated_by: str) -> bool:
        """Toggle a user's membership in a queue. Returns True if now a member."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            # Check if already a member
            row = conn.execute("""
                SELECT id, is_active FROM queue_members
                WHERE queue_id = ? AND user_email = ?
            """, (queue_id, email.lower())).fetchone()

            if row:
                # Toggle active status
                new_status = 0 if row['is_active'] else 1
                conn.execute("""
                    UPDATE queue_members
                    SET is_active = ?, updated_at = ?, updated_by = ?
                    WHERE id = ?
                """, (new_status, now, updated_by, row['id']))
                conn.commit()
                return new_status == 1
            else:
                # Add as new member
                conn.execute("""
                    INSERT INTO queue_members (queue_id, user_email, priority,
                                               created_at, created_by, updated_at, updated_by)
                    VALUES (?, ?, 0, ?, ?, ?, ?)
                """, (queue_id, email.lower(), now, updated_by, now, updated_by))
                conn.commit()
                return True

    # =========================================================================
    # Voicemail Destinations
    # =========================================================================

    def get_voicemail_destinations(self) -> list[dict]:
        """Get all active voicemail destinations."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM voicemail_destinations
                WHERE is_active = 1
                ORDER BY name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_voicemail_destination(self, destination_id: int) -> dict | None:
        """Get a single voicemail destination by ID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM voicemail_destinations WHERE id = ?
            """, (destination_id,)).fetchone()
            return dict(row) if row else None

    def get_voicemail_destination_by_email(self, email: str) -> dict | None:
        """Get a voicemail destination by email address.

        Used to look up zendesk_group_id when processing voicemails.
        """
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM voicemail_destinations
                WHERE email = ? AND is_active = 1
            """, (email,)).fetchone()
            return dict(row) if row else None

    def create_voicemail_destination(self, data: dict, created_by: str) -> int:
        """Create a new voicemail destination."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO voicemail_destinations (name, email, description, zendesk_group_id,
                    routing_type, created_at, created_by, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (data['name'], data.get('email'), data.get('description'),
                  data.get('zendesk_group_id'), data.get('routing_type', 'email'),
                  now, created_by, now, created_by))
            conn.commit()
            return cursor.lastrowid

    def update_voicemail_destination(self, destination_id: int, data: dict,
                                     updated_by: str) -> None:
        """Update a voicemail destination."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE voicemail_destinations
                SET name = ?, email = ?, description = ?, zendesk_group_id = ?,
                    routing_type = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (data['name'], data.get('email'), data.get('description'),
                  data.get('zendesk_group_id'), data.get('routing_type', 'email'),
                  now, updated_by, destination_id))
            conn.commit()

    def delete_voicemail_destination(self, destination_id: int) -> bool:
        """Delete a voicemail destination.

        Returns True if deleted, False if still in use by call flows.
        """
        with self._get_conn() as conn:
            # Check if any call flows use this destination (by ID or legacy email match)
            count = conn.execute("""
                SELECT COUNT(*) FROM call_flows
                WHERE voicemail_destination_id = ?
                   OR voicemail_email = (
                       SELECT email FROM voicemail_destinations WHERE id = ?
                   )
            """, (destination_id, destination_id)).fetchone()[0]
            if count > 0:
                return False

            conn.execute("""
                DELETE FROM voicemail_destinations WHERE id = ?
            """, (destination_id,))
            conn.commit()
            return True

    # =========================================================================
    # Queued Calls - Track callers waiting in queue with enriched data
    # =========================================================================

    def add_queued_call(self, data: dict) -> int:
        """Add a call to the queue with enriched caller data.

        Args:
            data: Dict with call_sid, queue_id, caller_number, called_number,
                  and optional enriched fields (customer_id, customer_name, etc.)

        Returns:
            The ID of the new queued_call record
        """
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO queued_calls (
                    call_sid, queue_id, queue_name, caller_number, called_number,
                    customer_id, customer_name, customer_email,
                    order_data, priority, priority_reason, status, enqueued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?)
            """, (
                data['call_sid'],
                data['queue_id'],
                data.get('queue_name'),
                data['caller_number'],
                data['called_number'],
                data.get('customer_id'),
                data.get('customer_name'),
                data.get('customer_email'),
                data.get('order_data'),  # JSON string
                data.get('priority', 'normal'),
                data.get('priority_reason'),
                now
            ))
            conn.commit()
            return cursor.lastrowid

    def get_queued_calls(self, queue_id: int = None, status: str = 'waiting') -> list[dict]:
        """Get queued calls, optionally filtered by queue and/or status.

        Args:
            queue_id: Filter to specific queue (None for all queues)
            status: Filter by status (default 'waiting')

        Returns:
            List of queued call records, ordered by priority then wait time
        """
        with self._get_conn() as conn:
            if queue_id:
                rows = conn.execute("""
                    SELECT * FROM queued_calls
                    WHERE queue_id = ? AND status = ?
                    ORDER BY
                        CASE priority
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'normal' THEN 3
                            ELSE 4
                        END,
                        enqueued_at ASC
                """, (queue_id, status)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM queued_calls
                    WHERE status = ?
                    ORDER BY
                        CASE priority
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'normal' THEN 3
                            ELSE 4
                        END,
                        enqueued_at ASC
                """, (status,)).fetchall()
            return [dict(row) for row in rows]

    def get_queued_call_by_sid(self, call_sid: str) -> dict | None:
        """Get a queued call by its Twilio call SID."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM queued_calls WHERE call_sid = ?
            """, (call_sid,)).fetchone()
            return dict(row) if row else None

    def update_queued_call_status(self, call_sid: str, status: str,
                                   answered_by: str = None) -> None:
        """Update the status of a queued call.

        Args:
            call_sid: Twilio call SID
            status: New status (answered, abandoned, timeout)
            answered_by: Agent email if status is 'answered'
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            # Get enqueued_at to calculate wait time
            row = conn.execute("""
                SELECT enqueued_at FROM queued_calls WHERE call_sid = ?
            """, (call_sid,)).fetchone()

            wait_seconds = None
            if row and row['enqueued_at']:
                try:
                    enqueued = _parse_dt(row['enqueued_at'])
                    wait_seconds = int((datetime.now(timezone.utc) - enqueued).total_seconds())
                except (ValueError, TypeError):
                    pass

            if status == 'answered':
                conn.execute("""
                    UPDATE queued_calls
                    SET status = ?, answered_at = ?, answered_by = ?,
                        ended_at = ?, wait_seconds = ?
                    WHERE call_sid = ?
                """, (status, now, answered_by, now, wait_seconds, call_sid))
            else:
                conn.execute("""
                    UPDATE queued_calls
                    SET status = ?, ended_at = ?, wait_seconds = ?
                    WHERE call_sid = ?
                """, (status, now, wait_seconds, call_sid))
            conn.commit()

    def claim_queued_call(self, call_sid: str, answered_by: str) -> bool:
        """Atomically claim a queued call for an agent.

        Uses a single UPDATE with WHERE status='waiting' so only the first
        agent to call this wins. Returns True if the call was claimed,
        False if it was already claimed by someone else or doesn't exist.

        Args:
            call_sid: Twilio call SID of the customer
            answered_by: Identifier of the agent who answered

        Returns:
            True if this agent claimed the call, False otherwise
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            # Calculate wait time
            row = conn.execute("""
                SELECT enqueued_at FROM queued_calls
                WHERE call_sid = ? AND status = 'waiting'
            """, (call_sid,)).fetchone()

            if not row:
                return False

            wait_seconds = None
            if row['enqueued_at']:
                try:
                    enqueued = _parse_dt(row['enqueued_at'])
                    wait_seconds = int((datetime.now(timezone.utc) - enqueued).total_seconds())
                except (ValueError, TypeError):
                    pass

            # Atomic update — only succeeds if status is still 'waiting'
            cursor = conn.execute("""
                UPDATE queued_calls
                SET status = 'answered', answered_at = ?, answered_by = ?,
                    ended_at = ?, wait_seconds = ?
                WHERE call_sid = ? AND status = 'waiting'
            """, (now, answered_by, now, wait_seconds, call_sid))
            conn.commit()

            return cursor.rowcount > 0

    def set_call_conference(self, call_sid: str, conference_name: str) -> None:
        """Store the conference name for a call (used for hold/unhold).

        Stores in both queued_calls and call_log so the lookup has a
        reliable fallback regardless of which table the call lives in.
        If no record exists in either table (e.g. transferred call with
        agent's browser SID), inserts a minimal call_log entry.

        Args:
            call_sid: Twilio call SID
            conference_name: The conference room name
        """
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE queued_calls SET conference_name = ? WHERE call_sid = ?",
                (conference_name, call_sid)
            )
            cursor = conn.execute(
                "UPDATE call_log SET conference_name = ? WHERE call_sid = ?",
                (conference_name, call_sid)
            )
            if cursor.rowcount == 0:
                # No existing record — insert a minimal one so get_call_conference
                # can find it (happens for transferred calls with agent's SID)
                now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                conn.execute(
                    "INSERT OR IGNORE INTO call_log (call_sid, conference_name, status, direction, from_number, to_number, started_at) VALUES (?, ?, 'in-progress', 'internal', '', '', ?)",
                    (call_sid, conference_name, now)
                )
            conn.commit()

    def get_call_conference(self, call_sid: str) -> str | None:
        """Get the conference name for a call.

        Checks queued_calls first, then call_log.

        Args:
            call_sid: Twilio call SID

        Returns:
            Conference name or None if not set
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT conference_name FROM queued_calls WHERE call_sid = ?",
                (call_sid,)
            ).fetchone()
            if row and row['conference_name']:
                return row['conference_name']
            # Check call_log
            row = conn.execute(
                "SELECT conference_name FROM call_log WHERE call_sid = ?",
                (call_sid,)
            ).fetchone()
            return row['conference_name'] if row else None

    def set_call_child_sid(self, call_sid: str, child_call_sid: str) -> None:
        """Store the child call SID for an outbound call (used for hold).

        Args:
            call_sid: The agent's parent call SID
            child_call_sid: The child call SID (outbound leg to external party)
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE call_log SET child_call_sid = ? WHERE call_sid = ?",
                (child_call_sid, call_sid)
            )
            if cursor.rowcount == 0:
                now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                conn.execute(
                    "INSERT OR IGNORE INTO call_log (call_sid, child_call_sid, status, direction, from_number, to_number, started_at) VALUES (?, ?, 'in-progress', 'internal', '', '', ?)",
                    (call_sid, child_call_sid, now)
                )
            conn.commit()

    def get_call_child_sid(self, call_sid: str) -> str | None:
        """Get the child call SID for an outbound call.

        Args:
            call_sid: The agent's parent call SID

        Returns:
            Child call SID or None
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT child_call_sid FROM call_log WHERE call_sid = ?",
                (call_sid,)
            ).fetchone()
            return row['child_call_sid'] if row and row['child_call_sid'] else None

    # ── Ring attempt tracking ──────────────────────────────────────────
    # Stored in DB so all gunicorn workers share the same state.

    def store_ring_attempts(self, group_key: str, call_sids: list[str],
                            group_type: str, metadata_by_sid: dict | None = None) -> None:
        """Store a batch of outbound ring call SIDs for later cancellation.

        Args:
            group_key: Grouping key (conference_name or customer_call_sid)
            call_sids: List of outbound call SIDs
            group_type: 'conference' or 'queue'
            metadata_by_sid: Optional dict of {call_sid: json_string} for extra data
        """
        with self._get_conn() as conn:
            for sid in call_sids:
                meta = (metadata_by_sid or {}).get(sid)
                conn.execute(
                    "INSERT OR IGNORE INTO ring_attempts (group_key, call_sid, group_type, metadata) VALUES (?, ?, ?, ?)",
                    (group_key, sid, group_type, meta)
                )
            conn.commit()

    def get_ring_attempts(self, group_key: str) -> list[str]:
        """Get all call SIDs for a ring group.

        Returns:
            List of call SIDs
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT call_sid FROM ring_attempts WHERE group_key = ?",
                (group_key,)
            ).fetchall()
            return [r['call_sid'] for r in rows]

    def get_ring_attempt_metadata(self, call_sid: str) -> dict | None:
        """Get metadata for a specific ring attempt by its call SID.

        Returns:
            Dict with group_key, group_type, metadata (parsed JSON) or None
        """
        import json
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT group_key, group_type, metadata FROM ring_attempts WHERE call_sid = ?",
                (call_sid,)
            ).fetchone()
            if not row:
                return None
            result = {'group_key': row['group_key'], 'group_type': row['group_type']}
            if row['metadata']:
                try:
                    result.update(json.loads(row['metadata']))
                except (json.JSONDecodeError, TypeError):
                    pass
            return result

    def remove_ring_attempt(self, group_key: str, call_sid: str) -> int:
        """Remove a single ring attempt. Returns rows deleted."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM ring_attempts WHERE group_key = ? AND call_sid = ?",
                (group_key, call_sid)
            )
            conn.commit()
            return cursor.rowcount

    def remove_ring_attempt_by_sid(self, call_sid: str) -> int:
        """Remove a ring attempt by call SID (regardless of group). Returns rows deleted."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM ring_attempts WHERE call_sid = ?",
                (call_sid,)
            )
            conn.commit()
            return cursor.rowcount

    def pop_ring_attempts(self, group_key: str) -> list[str]:
        """Get and remove all call SIDs for a ring group (atomic pop).

        Returns:
            List of call SIDs that were removed
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT call_sid FROM ring_attempts WHERE group_key = ?",
                (group_key,)
            ).fetchall()
            sids = [r['call_sid'] for r in rows]
            if sids:
                conn.execute(
                    "DELETE FROM ring_attempts WHERE group_key = ?",
                    (group_key,)
                )
                conn.commit()
            return sids

    def cleanup_old_ring_attempts(self, max_age_minutes: int = 10) -> int:
        """Remove stale ring attempts older than max_age_minutes.

        Safety net for attempts that were never cleaned up (e.g. missed callbacks).
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM ring_attempts WHERE created_at < datetime('now', ?)",
                (f'-{max_age_minutes} minutes',)
            )
            conn.commit()
            return cursor.rowcount

    # ── Call participant tracking ──────────────────────────────────────
    # Source of truth for who is in each active call.

    def add_participant(self, conference_name: str, call_sid: str, role: str,
                        name: str = None, phone_number: str = None,
                        email: str = None) -> None:
        """Record a participant joining a call.

        Args:
            conference_name: The conference room name
            call_sid: Twilio call SID for this participant
            role: 'agent', 'customer', or 'transfer_target'
            name: Display name
            phone_number: E.164 phone number (if applicable)
            email: Staff email (if agent)
        """
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO call_participants
                    (conference_name, call_sid, role, name, phone_number, email, joined_at, left_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), NULL)
            """, (conference_name, call_sid, role, name, phone_number, email))
            conn.commit()

    def remove_participant(self, call_sid: str) -> None:
        """Mark a participant as having left the call."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE call_participants SET left_at = datetime('now') WHERE call_sid = ? AND left_at IS NULL",
                (call_sid,)
            )
            conn.commit()

    def get_participants(self, conference_name: str, active_only: bool = True) -> list[dict]:
        """Get participants for a conference.

        Args:
            conference_name: The conference room name
            active_only: If True, only return participants who haven't left

        Returns:
            List of participant dicts
        """
        with self._get_conn() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM call_participants WHERE conference_name = ? AND left_at IS NULL ORDER BY joined_at",
                    (conference_name,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM call_participants WHERE conference_name = ? ORDER BY joined_at",
                    (conference_name,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_participant_by_sid(self, call_sid: str) -> dict | None:
        """Get a participant record by call SID."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM call_participants WHERE call_sid = ? AND left_at IS NULL",
                (call_sid,)
            ).fetchone()
            return dict(row) if row else None

    def update_participant(self, call_sid: str, **fields) -> None:
        """Update fields on a participant record.

        Args:
            call_sid: The participant's call SID
            **fields: Fields to update (role, name, phone_number, email)
        """
        allowed = {'role', 'name', 'phone_number', 'email'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [call_sid]
        with self._get_conn() as conn:
            conn.execute(
                f"UPDATE call_participants SET {set_clause} WHERE call_sid = ? AND left_at IS NULL",
                values
            )
            conn.commit()

    def mark_conference_left(self, conference_name: str) -> None:
        """Mark all participants in a conference as having left."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE call_participants SET left_at = datetime('now') WHERE conference_name = ? AND left_at IS NULL",
                (conference_name,)
            )
            conn.commit()

    def get_active_conferences(self) -> list[str]:
        """Get conference names that have active (not left) participants."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT conference_name FROM call_participants WHERE left_at IS NULL"
            ).fetchall()
            return [r['conference_name'] for r in rows]

    def cleanup_old_participants(self, hours: int = 24) -> int:
        """Remove participant records older than the given hours."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM call_participants WHERE joined_at < datetime('now', ?)",
                (f'-{hours} hours',)
            )
            conn.commit()
            return cursor.rowcount

    # ── Local permissions ─────────────────────────────────────────────

    def get_permissions(self, role_filter: list[str] = None) -> list[dict]:
        """Get all permission records, optionally filtered by role."""
        with self._get_conn() as conn:
            if role_filter:
                placeholders = ','.join('?' * len(role_filter))
                rows = conn.execute(
                    f"SELECT * FROM permissions WHERE role IN ({placeholders}) ORDER BY email",
                    role_filter
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM permissions ORDER BY email").fetchall()
            return [dict(r) for r in rows]

    def get_permission(self, email: str) -> dict | None:
        """Get permission record for a specific email."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM permissions WHERE email = ?",
                (email.lower(),)
            ).fetchone()
            return dict(row) if row else None

    def set_permission(self, email: str, role: str, granted_by: str = None) -> bool:
        """Set or update a user's role. Returns True on success."""
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO permissions (email, role, granted_by) VALUES (?, ?, ?)",
                (email.lower(), role, granted_by)
            )
            conn.commit()
            return True

    def remove_permission(self, email: str) -> bool:
        """Remove a user's elevated permission. Returns True if removed."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM permissions WHERE email = ?",
                (email.lower(),)
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_queue_stats(self, queue_id: int = None) -> dict:
        """Get statistics for queued calls.

        Returns:
            Dict with counts by status, average wait time, etc.
        """
        with self._get_conn() as conn:
            if queue_id:
                waiting = conn.execute("""
                    SELECT COUNT(*) FROM queued_calls
                    WHERE queue_id = ? AND status = 'waiting'
                """, (queue_id,)).fetchone()[0]

                avg_wait = conn.execute("""
                    SELECT AVG(wait_seconds) FROM queued_calls
                    WHERE queue_id = ? AND status = 'answered'
                    AND enqueued_at > datetime('now', '-1 hour')
                """, (queue_id,)).fetchone()[0]
            else:
                waiting = conn.execute("""
                    SELECT COUNT(*) FROM queued_calls WHERE status = 'waiting'
                """).fetchone()[0]

                avg_wait = conn.execute("""
                    SELECT AVG(wait_seconds) FROM queued_calls
                    WHERE status = 'answered'
                    AND enqueued_at > datetime('now', '-1 hour')
                """).fetchone()[0]

            return {
                'waiting': waiting,
                'avg_wait_seconds': round(avg_wait) if avg_wait else 0
            }

    def cleanup_old_queued_calls(self, hours: int = 24) -> int:
        """Remove queued calls older than specified hours.

        Returns:
            Number of records deleted
        """
        with self._get_conn() as conn:
            cursor = conn.execute("""
                DELETE FROM queued_calls
                WHERE enqueued_at < datetime('now', '-' || ? || ' hours')
            """, (hours,))
            conn.commit()
            return cursor.rowcount

    # =========================================================================
    # Call Transfer - Track transfer state for warm/blind transfers
    # =========================================================================

    _TRANSFER_TABLES = {'queued_calls', 'call_log'}

    def _transfer_tables(self, source: str) -> list[str]:
        """Return which tables to update based on source.

        source='queued_calls' → both tables; source='call_log' → call_log only.
        """
        if source == 'call_log':
            return ['call_log']
        return ['queued_calls', 'call_log']

    def start_transfer(self, call_sid: str, transfer_type: str, target: str,
                       target_name: str, transferred_by: str,
                       source: str = 'queued_calls') -> None:
        """Start a transfer for a call."""
        now = datetime.now(timezone.utc).isoformat()
        params = (transfer_type, target, target_name, transferred_by, now, call_sid)
        with self._get_conn() as conn:
            for table in self._transfer_tables(source):
                conn.execute(f"""
                    UPDATE {table}
                    SET transfer_status = 'pending', transfer_type = ?,
                        transfer_target = ?, transfer_target_name = ?,
                        transferred_by = ?, transferred_at = ?,
                        transfer_consult_call_sid = NULL,
                        transfer_consult_conference = NULL,
                        transfer_failure_reason = NULL
                    WHERE call_sid = ?
                """, params)
            conn.commit()

    def update_transfer_consultation(self, call_sid: str, consult_call_sid: str,
                                      consult_conference: str,
                                      source: str = 'queued_calls') -> None:
        """Update transfer with consultation call details (for warm transfers)."""
        params = (consult_call_sid, consult_conference, call_sid)
        with self._get_conn() as conn:
            for table in self._transfer_tables(source):
                conn.execute(f"""
                    UPDATE {table}
                    SET transfer_status = 'consulting',
                        transfer_consult_call_sid = ?, transfer_consult_conference = ?
                    WHERE call_sid = ?
                """, params)
            conn.commit()

    def complete_transfer(self, call_sid: str, source: str = 'queued_calls') -> None:
        """Mark a transfer as completed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            for table in self._transfer_tables(source):
                conn.execute(f"""
                    UPDATE {table}
                    SET transfer_status = 'completed', status = 'transferred', ended_at = ?
                    WHERE call_sid = ?
                """, (now, call_sid))
            conn.commit()

    def cancel_transfer(self, call_sid: str, source: str = 'queued_calls') -> None:
        """Cancel a pending or consulting transfer."""
        with self._get_conn() as conn:
            for table in self._transfer_tables(source):
                conn.execute(f"""
                    UPDATE {table}
                    SET transfer_status = 'cancelled',
                        transfer_consult_call_sid = NULL,
                        transfer_consult_conference = NULL
                    WHERE call_sid = ?
                """, (call_sid,))
            conn.commit()

    def fail_transfer(self, call_sid: str, reason: str = None,
                      source: str = 'queued_calls') -> None:
        """Mark a transfer as failed and record the reason.

        `reason` is typically the Twilio CallStatus value (busy, no-answer,
        failed, canceled) so the UI can show an agent-friendly explanation.
        """
        with self._get_conn() as conn:
            for table in self._transfer_tables(source):
                conn.execute(
                    f"UPDATE {table} SET transfer_status = 'failed', "
                    f"transfer_failure_reason = ? WHERE call_sid = ?",
                    (reason, call_sid)
                )
            conn.commit()

    def update_transfer_status(self, call_sid: str, status: str,
                               source: str = 'queued_calls') -> None:
        """Update transfer_status on a call."""
        with self._get_conn() as conn:
            for table in self._transfer_tables(source):
                conn.execute(
                    f"UPDATE {table} SET transfer_status = ? WHERE call_sid = ?",
                    (status, call_sid)
                )
            conn.commit()

    def get_transfer_state(self, call_sid: str, source: str = 'queued_calls') -> dict | None:
        """Get the current transfer state for a call."""
        _TRANSFER_COLS = """transfer_status, transfer_type, transfer_target,
                       transfer_target_name, transfer_consult_call_sid,
                       transfer_consult_conference, transferred_by, transferred_at,
                       transfer_failure_reason, conference_name"""
        with self._get_conn() as conn:
            for table in self._transfer_tables(source):
                row = conn.execute(f"""
                    SELECT {_TRANSFER_COLS}
                    FROM {table} WHERE call_sid = ?
                """, (call_sid,)).fetchone()
                if row and row['transfer_status']:
                    return dict(row)
            return None

    # Backward-compatible aliases for callers using the old _log suffix
    def start_transfer_log(self, *args, **kwargs):
        return self.start_transfer(*args, source='call_log', **kwargs)

    def update_transfer_consultation_log(self, *args, **kwargs):
        return self.update_transfer_consultation(*args, source='call_log', **kwargs)

    def complete_transfer_log(self, *args, **kwargs):
        return self.complete_transfer(*args, source='call_log', **kwargs)

    def cancel_transfer_log(self, *args, **kwargs):
        return self.cancel_transfer(*args, source='call_log', **kwargs)

    def fail_transfer_log(self, *args, **kwargs):
        return self.fail_transfer(*args, source='call_log', **kwargs)

    def get_transfer_state_log(self, *args, **kwargs):
        return self.get_transfer_state(*args, source='call_log', **kwargs)

    # Legacy aliases
    def update_queued_call_transfer_status(self, call_sid, status):
        return self.update_transfer_status(call_sid, status, source='queued_calls')

    def update_call_log_transfer_status(self, call_sid, status):
        return self.update_transfer_status(call_sid, status, source='call_log')

    def get_transfer_by_consult_sid(self, consult_call_sid: str) -> dict | None:
        """Look up a transfer by the consultation call SID (the target agent's call)."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT transferred_by, transfer_target_name, transfer_type,
                       transfer_status, direction, from_number, to_number, customer_name
                FROM call_log WHERE transfer_consult_call_sid = ?
                AND transfer_status IN ('pending', 'consulting', 'callback')
            """, (consult_call_sid,)).fetchone()
            if not row:
                # Also check queued_calls for queue-originated transfers.
                # Queue calls are always inbound, so caller_number IS the customer.
                row = conn.execute("""
                    SELECT transferred_by, transfer_target_name, transfer_type,
                           transfer_status, 'inbound' as direction,
                           caller_number as from_number, NULL as to_number,
                           customer_name
                    FROM queued_calls WHERE transfer_consult_call_sid = ?
                    AND transfer_status IN ('pending', 'consulting', 'callback')
                """, (consult_call_sid,)).fetchone()
            return dict(row) if row else None

    # =========================================================================
    # Bot Settings (general configuration)
    # =========================================================================

    def get_bot_settings(self) -> dict:
        """Get all bot settings as a dict."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT setting_key, setting_value, description
                FROM bot_settings
            """).fetchall()
            return {row['setting_key']: {
                'value': row['setting_value'],
                'description': row['description']
            } for row in rows}

    def get_bot_setting(self, key: str, default: str = None) -> str | None:
        """Get a single bot setting value."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT setting_value FROM bot_settings WHERE setting_key = ?
            """, (key,)).fetchone()
            return row['setting_value'] if row else default

    def set_bot_setting(self, key: str, value: str, updated_by: str) -> None:
        """Set a bot setting value."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO bot_settings (setting_key, setting_value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
            """, (key, value, now, updated_by))
            conn.commit()

    # =========================================================================
    # Address Book
    # =========================================================================

    def get_address_book(self, search: str = None) -> list[dict]:
        """Return all address book entries, optionally filtered by name/section."""
        with self._get_conn() as conn:
            if search:
                pattern = f"%{search}%"
                rows = conn.execute("""
                    SELECT * FROM address_book
                    WHERE name LIKE ? OR section LIKE ? OR position LIKE ?
                    ORDER BY name COLLATE NOCASE
                """, (pattern, pattern, pattern)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM address_book
                    ORDER BY name COLLATE NOCASE
                """).fetchall()
            return [dict(r) for r in rows]

    def get_address_book_by_mobile(self, mobile_e164: str) -> dict | None:
        """Look up an address book entry by normalised E.164 mobile number."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM address_book WHERE mobile_e164 = ?
            """, (mobile_e164,)).fetchone()
            return dict(row) if row else None

    def get_address_book_by_source_id(self, source: str, external_id: str) -> dict | None:
        """Look up an address book entry by source + external_id."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM address_book WHERE source = ? AND external_id = ?
            """, (source, external_id)).fetchone()
            return dict(row) if row else None

    def upsert_address_book_entry(self, name: str, display_mobile: str,
                                  mobile_e164: str, section: str = None,
                                  position: str = None, source: str = 'manual',
                                  external_id: str = None,
                                  email: str = None) -> int:
        """Insert or update an address book entry. Returns the row id."""
        now = datetime.now(timezone.utc).isoformat()
        email_lc = email.lower() if email else None
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO address_book
                    (name, display_mobile, mobile_e164, section, position,
                     source, external_id, email, synced_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    name         = excluded.name,
                    display_mobile = excluded.display_mobile,
                    mobile_e164  = excluded.mobile_e164,
                    section      = excluded.section,
                    position     = excluded.position,
                    email        = excluded.email,
                    synced_at    = excluded.synced_at,
                    updated_at   = excluded.updated_at
            """, (name, display_mobile, mobile_e164, section, position,
                  source, external_id, email_lc, now, now))
            conn.commit()
            return cursor.lastrowid

    def delete_address_book_entry(self, entry_id: int) -> bool:
        """Delete an address book entry by id. Returns True if a row was deleted."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM address_book WHERE id = ?", (entry_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_address_book_by_source(self, source: str,
                                      keep_external_ids: list[str] = None) -> int:
        """Remove synced entries that are no longer in the source.

        Deletes all rows with the given source whose external_id is NOT in
        keep_external_ids. Used by the sync service to soft-prune removed
        contacts. Returns number of rows deleted.
        """
        with self._get_conn() as conn:
            if keep_external_ids:
                placeholders = ','.join('?' * len(keep_external_ids))
                cursor = conn.execute(f"""
                    DELETE FROM address_book
                    WHERE source = ? AND external_id NOT IN ({placeholders})
                """, [source] + keep_external_ids)
            else:
                cursor = conn.execute(
                    "DELETE FROM address_book WHERE source = ?", (source,)
                )
            conn.commit()
            return cursor.rowcount


def get_db() -> Database:
    """Get the tenant-scoped database for the current request."""
    from rinq.tenant.context import get_tenant_db
    return get_tenant_db()
