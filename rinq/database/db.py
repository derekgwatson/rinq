"""
Database operations for Tina.

Manages:
- Phone numbers and their forwarding rules
- User/extension mappings
- Call recording logs
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    from shared.migrations import MigrationRunner
except ImportError:
    from rinq.vendor.migrations import MigrationRunner


class Database:
    """SQLite database for Tina."""

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
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE phone_numbers
                SET forward_to = ?, updated_at = ?, updated_by = ?
                WHERE sid = ?
            """, (forward_to, now, updated_by, sid))
            conn.commit()

    def update_browser_ring(self, sid: str, ring_browser: bool, browser_identity: str, updated_by: str) -> None:
        """Update browser ring settings for a phone number."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE phone_numbers
                SET ring_browser = ?, browser_identity = ?, updated_at = ?, updated_by = ?
                WHERE sid = ?
            """, (1 if ring_browser else 0, browser_identity or None, now, updated_by, sid))
            conn.commit()

    def update_phone_number_section(self, sid: str, section: str, updated_by: str) -> None:
        """Update the section for a phone number."""
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
                INSERT INTO recording_log (
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
                          exclude_voicemail: bool = False) -> list[dict]:
        """Get recent recording log entries.

        Args:
            limit: Max number of records to return
            call_type: Filter by call type ('inbound', 'outbound', etc.)
            exclude_voicemail: If True, exclude voicemail recordings
        """
        with self._get_conn() as conn:
            query = "SELECT * FROM recording_log WHERE 1=1"
            params = []

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
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE recording_log
                SET local_file_path = ?, last_accessed_at = ?
                WHERE recording_sid = ?
            """, (local_file_path, now, recording_sid))
            conn.commit()

    def update_recording_last_accessed(self, recording_sid: str) -> None:
        """Update the last_accessed_at timestamp when a recording is played."""
        now = datetime.utcnow().isoformat()
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
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

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
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

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
        now = datetime.utcnow().isoformat()
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
            """, (action, target, details, performed_by, datetime.utcnow().isoformat()))
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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

        now = datetime.utcnow().isoformat()
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

        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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

        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE call_flows
                SET name = ?, description = ?, greeting_audio_id = ?, schedule_id = ?,
                    open_action = ?, open_queue_id = ?, open_forward_number = ?, open_audio_id = ?,
                    open_no_answer_action = ?,
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
        now = datetime.utcnow().isoformat()
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
                       cf.closed_action, cf.closed_audio_id, cf.closed_forward_number,
                       cf.voicemail_email,
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
                'closed_action': phone_row['closed_action'],
                'closed_audio_id': phone_row['closed_audio_id'],
                'closed_forward_number': phone_row['closed_forward_number'],
                'voicemail_email': phone_row['voicemail_email'],
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE callback_requests
                SET status = 'completed', completed_at = ?, callback_call_sid = ?, updated_at = ?
                WHERE id = ?
            """, (now, call_sid, now, callback_id))
            conn.commit()

    def fail_callback(self, callback_id: int, notes: str = None) -> None:
        """Mark a callback as failed."""
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        """Get staff extensions that are actively using Tina."""
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
        """Toggle whether a staff member is actively using Tina."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET is_active = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (1 if is_active else 0, now, updated_by, email.lower()))
            conn.commit()

    def set_dnd(self, email: str, enabled: bool, updated_by: str) -> None:
        """Toggle do-not-disturb for a staff member."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET dnd_enabled = ?, updated_at = ?, updated_by = ?
                WHERE email = ?
            """, (1 if enabled else 0, now, updated_by, email.lower()))
            conn.commit()

    def update_heartbeat(self, email: str) -> None:
        """Update the last_heartbeat timestamp for a staff member."""
        now = datetime.utcnow().isoformat()
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

        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE staff_extensions
                SET show_in_pam = ?,
                    forward_to = ?,
                    forward_mode = ?,
                    updated_at = ?,
                    updated_by = ?
                WHERE email = ?
            """, (
                1 if data.get('show_in_pam') else 0,
                data.get('forward_to'),
                data.get('forward_mode', 'always'),
                now,
                updated_by,
                email.lower()
            ))
            conn.commit()

    def update_staff_name_audio(self, email: str, name_audio_path: str,
                                name_audio_text: str, updated_by: str) -> None:
        """Update the name audio path and text for a staff extension."""
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat() + 'Z'  # Add Z suffix for JavaScript parsing
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
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            # Get enqueued_at to calculate wait time
            row = conn.execute("""
                SELECT enqueued_at FROM queued_calls WHERE call_sid = ?
            """, (call_sid,)).fetchone()

            wait_seconds = None
            if row and row['enqueued_at']:
                try:
                    enqueued = datetime.fromisoformat(row['enqueued_at'])
                    wait_seconds = int((datetime.utcnow() - enqueued).total_seconds())
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
        now = datetime.utcnow().isoformat()
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
                    enqueued = datetime.fromisoformat(row['enqueued_at'])
                    wait_seconds = int((datetime.utcnow() - enqueued).total_seconds())
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
                now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
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
                now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
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

    def start_transfer(self, call_sid: str, transfer_type: str, target: str,
                       target_name: str, transferred_by: str) -> None:
        """Start a transfer for a call. Writes to both queued_calls and call_log."""
        now = datetime.utcnow().isoformat()
        params = (transfer_type, target, target_name, transferred_by, now, call_sid)
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE queued_calls
                SET transfer_status = 'pending', transfer_type = ?,
                    transfer_target = ?, transfer_target_name = ?,
                    transferred_by = ?, transferred_at = ?
                WHERE call_sid = ?
            """, params)
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'pending', transfer_type = ?,
                    transfer_target = ?, transfer_target_name = ?,
                    transferred_by = ?, transferred_at = ?
                WHERE call_sid = ?
            """, params)
            conn.commit()

    def update_transfer_consultation(self, call_sid: str, consult_call_sid: str,
                                      consult_conference: str) -> None:
        """Update transfer with consultation call details (for warm transfers).

        Args:
            call_sid: The original caller's call SID
            consult_call_sid: The SID of the consultation call to the target
            consult_conference: The conference room name for the consultation
        """
        with self._get_conn() as conn:
            params = (consult_call_sid, consult_conference, call_sid)
            conn.execute("""
                UPDATE queued_calls
                SET transfer_status = 'consulting',
                    transfer_consult_call_sid = ?, transfer_consult_conference = ?
                WHERE call_sid = ?
            """, params)
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'consulting',
                    transfer_consult_call_sid = ?, transfer_consult_conference = ?
                WHERE call_sid = ?
            """, params)
            conn.commit()

    def complete_transfer(self, call_sid: str) -> None:
        """Mark a transfer as completed."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE queued_calls
                SET transfer_status = 'completed', status = 'transferred', ended_at = ?
                WHERE call_sid = ?
            """, (now, call_sid))
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'completed', status = 'transferred', ended_at = ?
                WHERE call_sid = ?
            """, (now, call_sid))
            conn.commit()

    def cancel_transfer(self, call_sid: str) -> None:
        """Cancel a pending or consulting transfer."""
        with self._get_conn() as conn:
            for table in ('queued_calls', 'call_log'):
                conn.execute(f"""
                    UPDATE {table}
                    SET transfer_status = 'cancelled',
                        transfer_consult_call_sid = NULL,
                        transfer_consult_conference = NULL
                    WHERE call_sid = ?
                """, (call_sid,))
            conn.commit()

    def fail_transfer(self, call_sid: str, reason: str = None) -> None:
        """Mark a transfer as failed.

        Args:
            call_sid: The original caller's call SID
            reason: Optional failure reason
        """
        with self._get_conn() as conn:
            for table in ('queued_calls', 'call_log'):
                conn.execute(f"UPDATE {table} SET transfer_status = 'failed' WHERE call_sid = ?", (call_sid,))
            conn.commit()

    def update_queued_call_transfer_status(self, call_sid: str, status: str) -> None:
        """Update transfer_status on a queued call."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE queued_calls SET transfer_status = ? WHERE call_sid = ?",
                (status, call_sid)
            )
            conn.execute(
                "UPDATE call_log SET transfer_status = ? WHERE call_sid = ?",
                (status, call_sid)
            )
            conn.commit()

    def update_call_log_transfer_status(self, call_sid: str, status: str) -> None:
        """Update transfer_status on a call_log entry."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE call_log SET transfer_status = ? WHERE call_sid = ?",
                (status, call_sid)
            )
            conn.commit()

    def get_transfer_state(self, call_sid: str) -> dict | None:
        """Get the current transfer state for a call. Checks queued_calls then call_log."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT transfer_status, transfer_type, transfer_target,
                       transfer_target_name, transfer_consult_call_sid,
                       transfer_consult_conference, transferred_by, transferred_at,
                       conference_name
                FROM queued_calls WHERE call_sid = ?
            """, (call_sid,)).fetchone()
            if row and row['transfer_status']:
                return dict(row)
            # Fallback to call_log for conference-first calls
            row = conn.execute("""
                SELECT transfer_status, transfer_type, transfer_target,
                       transfer_target_name, transfer_consult_call_sid,
                       transfer_consult_conference, transferred_by, transferred_at,
                       conference_name
                FROM call_log WHERE call_sid = ?
            """, (call_sid,)).fetchone()
            if row and row['transfer_status']:
                return dict(row)
            return None

    # =========================================================================
    # Transfer State (call_log-based — for non-queue calls)
    # =========================================================================

    def start_transfer_log(self, call_sid: str, transfer_type: str, target: str,
                           target_name: str, transferred_by: str) -> None:
        """Start a transfer for any call type (uses call_log)."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'pending',
                    transfer_type = ?,
                    transfer_target = ?,
                    transfer_target_name = ?,
                    transferred_by = ?,
                    transferred_at = ?
                WHERE call_sid = ?
            """, (transfer_type, target, target_name, transferred_by, now, call_sid))
            conn.commit()

    def update_transfer_consultation_log(self, call_sid: str, consult_call_sid: str,
                                          consult_conference: str) -> None:
        """Update transfer with consultation details (uses call_log)."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'consulting',
                    transfer_consult_call_sid = ?,
                    transfer_consult_conference = ?
                WHERE call_sid = ?
            """, (consult_call_sid, consult_conference, call_sid))
            conn.commit()

    def complete_transfer_log(self, call_sid: str) -> None:
        """Mark a transfer as completed (uses call_log)."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'completed',
                    status = 'transferred',
                    ended_at = ?
                WHERE call_sid = ?
            """, (now, call_sid))
            conn.commit()

    def cancel_transfer_log(self, call_sid: str) -> None:
        """Cancel a transfer (uses call_log)."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'cancelled',
                    transfer_consult_call_sid = NULL,
                    transfer_consult_conference = NULL
                WHERE call_sid = ?
            """, (call_sid,))
            conn.commit()

    def fail_transfer_log(self, call_sid: str, reason: str = None) -> None:
        """Mark a transfer as failed (uses call_log)."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE call_log
                SET transfer_status = 'failed'
                WHERE call_sid = ?
            """, (call_sid,))
            conn.commit()

    def get_transfer_state_log(self, call_sid: str) -> dict | None:
        """Get transfer state from call_log."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT transfer_status, transfer_type, transfer_target,
                       transfer_target_name, transfer_consult_call_sid,
                       transfer_consult_conference, transferred_by, transferred_at,
                       conference_name
                FROM call_log WHERE call_sid = ?
            """, (call_sid,)).fetchone()
            if row and row['transfer_status']:
                return dict(row)
            return None

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
        now = datetime.utcnow().isoformat()
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
    # Call Statistics - Aggregation and Reporting
    # =========================================================================

    def aggregate_daily_stats(self, target_date: str = None) -> dict:
        """Aggregate call statistics for a given date.

        This should be called before cleanup_old_queued_calls to preserve
        statistics that would otherwise be lost.

        Args:
            target_date: Date to aggregate (YYYY-MM-DD). Defaults to yesterday.

        Returns:
            Dict with counts of records aggregated
        """
        if not target_date:
            target_date = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            # Aggregate queue stats by queue and agent
            queue_data = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    answered_by as agent_email,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(CASE WHEN status = 'transferred' THEN 1 ELSE 0 END) as transferred_calls,
                    SUM(COALESCE(wait_seconds, 0)) as total_wait_seconds,
                    MAX(wait_seconds) as max_wait_seconds,
                    -- Answer speed buckets (for answered calls only)
                    SUM(CASE WHEN status = 'answered' AND wait_seconds <= 15 THEN 1 ELSE 0 END) as within_15s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 15 AND wait_seconds <= 30 THEN 1 ELSE 0 END) as within_30s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 30 AND wait_seconds <= 60 THEN 1 ELSE 0 END) as within_60s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 60 AND wait_seconds <= 90 THEN 1 ELSE 0 END) as within_90s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 90 THEN 1 ELSE 0 END) as over_90s,
                    -- Max wait for answered vs abandoned
                    MAX(CASE WHEN status = 'answered' THEN wait_seconds ELSE 0 END) as max_answered_wait,
                    MAX(CASE WHEN status = 'abandoned' THEN wait_seconds ELSE 0 END) as max_abandoned_wait,
                    -- Total wait for abandoned (for average calculation)
                    SUM(CASE WHEN status = 'abandoned' THEN COALESCE(wait_seconds, 0) ELSE 0 END) as abandoned_wait
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                GROUP BY queue_id, queue_name, answered_by
            """, (target_date,)).fetchall()

            # Get duration stats from recording_log for the same date
            recording_data = conn.execute("""
                SELECT
                    staff_email as agent_email,
                    SUM(COALESCE(duration_seconds, 0)) as total_duration,
                    SUM(CASE WHEN call_type = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN call_type = 'outbound' THEN 1 ELSE 0 END) as outbound_calls
                FROM recording_log
                WHERE DATE(created_at) = ?
                  AND call_type IN ('inbound', 'outbound')
                GROUP BY staff_email
            """, (target_date,)).fetchall()

            # Create lookup for recording duration by agent
            duration_by_agent = {
                row['agent_email']: {
                    'duration': row['total_duration'],
                    'inbound': row['inbound_calls'],
                    'outbound': row['outbound_calls']
                }
                for row in recording_data if row['agent_email']
            }

            now = datetime.utcnow().isoformat()
            records_created = 0

            for row in queue_data:
                agent = row['agent_email'] or '__all__'
                agent_durations = duration_by_agent.get(agent, {'duration': 0, 'inbound': 0, 'outbound': 0})

                conn.execute("""
                    INSERT INTO daily_call_stats (
                        stat_date, queue_id, queue_name, agent_email,
                        total_calls, answered_calls, abandoned_calls, timeout_calls, transferred_calls,
                        total_duration_seconds, total_wait_seconds,
                        answered_within_15s, answered_within_30s, answered_within_60s,
                        answered_within_90s, answered_over_90s,
                        abandoned_total_wait_seconds,
                        max_wait_seconds, max_answered_wait_seconds, max_abandoned_wait_seconds,
                        inbound_calls, outbound_calls,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stat_date, queue_id, agent_email) DO UPDATE SET
                        total_calls = excluded.total_calls,
                        answered_calls = excluded.answered_calls,
                        abandoned_calls = excluded.abandoned_calls,
                        timeout_calls = excluded.timeout_calls,
                        transferred_calls = excluded.transferred_calls,
                        total_duration_seconds = excluded.total_duration_seconds,
                        total_wait_seconds = excluded.total_wait_seconds,
                        answered_within_15s = excluded.answered_within_15s,
                        answered_within_30s = excluded.answered_within_30s,
                        answered_within_60s = excluded.answered_within_60s,
                        answered_within_90s = excluded.answered_within_90s,
                        answered_over_90s = excluded.answered_over_90s,
                        abandoned_total_wait_seconds = excluded.abandoned_total_wait_seconds,
                        max_wait_seconds = excluded.max_wait_seconds,
                        max_answered_wait_seconds = excluded.max_answered_wait_seconds,
                        max_abandoned_wait_seconds = excluded.max_abandoned_wait_seconds,
                        inbound_calls = excluded.inbound_calls,
                        outbound_calls = excluded.outbound_calls,
                        updated_at = excluded.updated_at
                """, (
                    target_date, row['queue_id'], row['queue_name'],
                    row['agent_email'],
                    row['total_calls'], row['answered_calls'], row['abandoned_calls'],
                    row['timeout_calls'], row['transferred_calls'],
                    agent_durations['duration'], row['total_wait_seconds'],
                    row['within_15s'], row['within_30s'], row['within_60s'],
                    row['within_90s'], row['over_90s'],
                    row['abandoned_wait'],
                    row['max_wait_seconds'], row['max_answered_wait'], row['max_abandoned_wait'],
                    agent_durations['inbound'], agent_durations['outbound'],
                    now, now
                ))
                records_created += 1

            conn.commit()
            return {'date': target_date, 'records_created': records_created}

    def aggregate_hourly_stats(self, target_date: str = None) -> dict:
        """Aggregate hourly call distribution for a given date.

        Args:
            target_date: Date to aggregate (YYYY-MM-DD). Defaults to yesterday.

        Returns:
            Dict with counts of records aggregated
        """
        if not target_date:
            target_date = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            hourly_data = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    CAST(strftime('%H', enqueued_at) AS INTEGER) as hour,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                GROUP BY queue_id, queue_name, hour
            """, (target_date,)).fetchall()

            now = datetime.utcnow().isoformat()
            records_created = 0

            for row in hourly_data:
                conn.execute("""
                    INSERT INTO hourly_call_stats (
                        stat_date, stat_hour, queue_id, queue_name,
                        total_calls, answered_calls, abandoned_calls, timeout_calls,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stat_date, stat_hour, queue_id) DO UPDATE SET
                        total_calls = excluded.total_calls,
                        answered_calls = excluded.answered_calls,
                        abandoned_calls = excluded.abandoned_calls,
                        timeout_calls = excluded.timeout_calls
                """, (
                    target_date, row['hour'], row['queue_id'], row['queue_name'],
                    row['total_calls'], row['answered_calls'],
                    row['abandoned_calls'], row['timeout_calls'],
                    now
                ))
                records_created += 1

            conn.commit()
            return {'date': target_date, 'records_created': records_created}

    def get_daily_stats_summary(self, start_date: str, end_date: str) -> dict:
        """Get aggregated daily statistics summary for a date range.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            Dict with summary statistics
        """
        with self._get_conn() as conn:
            # Get aggregated totals
            row = conn.execute("""
                SELECT
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls,
                    SUM(transferred_calls) as transferred_calls,
                    SUM(total_duration_seconds) as total_duration,
                    SUM(total_wait_seconds) as total_wait,
                    SUM(answered_within_15s) as within_15s,
                    SUM(answered_within_30s) as within_30s,
                    SUM(answered_within_60s) as within_60s,
                    SUM(answered_within_90s) as within_90s,
                    SUM(answered_over_90s) as over_90s,
                    SUM(abandoned_total_wait_seconds) as abandoned_wait,
                    MAX(max_wait_seconds) as max_wait,
                    MAX(max_answered_wait_seconds) as max_answered_wait,
                    MAX(max_abandoned_wait_seconds) as max_abandoned_wait,
                    SUM(inbound_calls) as inbound_calls,
                    SUM(outbound_calls) as outbound_calls
                FROM daily_call_stats
                WHERE stat_date BETWEEN ? AND ?
            """, (start_date, end_date)).fetchone()

            if not row or not row['total_calls']:
                return self._empty_stats_summary()

            total = row['total_calls'] or 0
            answered = row['answered_calls'] or 0
            abandoned = row['abandoned_calls'] or 0
            timeout = row['timeout_calls'] or 0

            return {
                'total_calls': total,
                'answered_calls': answered,
                'abandoned_calls': abandoned,
                'timeout_calls': timeout,
                'transferred_calls': row['transferred_calls'] or 0,
                'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                'abandoned_rate': round(abandoned / total * 100, 1) if total > 0 else 0,
                'timeout_rate': round(timeout / total * 100, 1) if total > 0 else 0,
                'total_duration_seconds': row['total_duration'] or 0,
                'avg_duration_seconds': round((row['total_duration'] or 0) / answered) if answered > 0 else 0,
                'total_wait_seconds': row['total_wait'] or 0,
                'avg_wait_seconds': round((row['total_wait'] or 0) / total) if total > 0 else 0,
                'avg_answered_wait_seconds': round((row['total_wait'] or 0) / answered) if answered > 0 else 0,
                'avg_abandoned_wait_seconds': round((row['abandoned_wait'] or 0) / abandoned) if abandoned > 0 else 0,
                'max_wait_seconds': row['max_wait'] or 0,
                'max_answered_wait_seconds': row['max_answered_wait'] or 0,
                'max_abandoned_wait_seconds': row['max_abandoned_wait'] or 0,
                'answer_speed': {
                    'within_15s': row['within_15s'] or 0,
                    'within_30s': row['within_30s'] or 0,
                    'within_60s': row['within_60s'] or 0,
                    'within_90s': row['within_90s'] or 0,
                    'over_90s': row['over_90s'] or 0,
                },
                'inbound_calls': row['inbound_calls'] or 0,
                'outbound_calls': row['outbound_calls'] or 0,
            }

    def _empty_stats_summary(self) -> dict:
        """Return an empty stats summary structure."""
        return {
            'total_calls': 0,
            'answered_calls': 0,
            'abandoned_calls': 0,
            'timeout_calls': 0,
            'transferred_calls': 0,
            'answer_rate': 0,
            'abandoned_rate': 0,
            'timeout_rate': 0,
            'total_duration_seconds': 0,
            'avg_duration_seconds': 0,
            'total_wait_seconds': 0,
            'avg_wait_seconds': 0,
            'avg_answered_wait_seconds': 0,
            'avg_abandoned_wait_seconds': 0,
            'max_wait_seconds': 0,
            'max_answered_wait_seconds': 0,
            'max_abandoned_wait_seconds': 0,
            'answer_speed': {
                'within_15s': 0,
                'within_30s': 0,
                'within_60s': 0,
                'within_90s': 0,
                'over_90s': 0,
            },
            'inbound_calls': 0,
            'outbound_calls': 0,
        }

    def get_agent_stats(self, start_date: str, end_date: str) -> list[dict]:
        """Get per-agent statistics for a date range.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            List of dicts with per-agent stats, sorted by total calls desc
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    agent_email,
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls,
                    SUM(total_duration_seconds) as total_duration,
                    SUM(total_wait_seconds) as total_wait
                FROM daily_call_stats
                WHERE stat_date BETWEEN ? AND ?
                  AND agent_email IS NOT NULL
                  AND agent_email != '__all__'
                GROUP BY agent_email
                ORDER BY SUM(answered_calls) DESC
            """, (start_date, end_date)).fetchall()

            result = []
            for row in rows:
                answered = row['answered_calls'] or 0
                total = row['total_calls'] or 0
                missed = (row['abandoned_calls'] or 0) + (row['timeout_calls'] or 0)

                result.append({
                    'agent_email': row['agent_email'],
                    'total_calls': total,
                    'answered_calls': answered,
                    'missed_calls': missed,
                    'total_duration_seconds': row['total_duration'] or 0,
                    'avg_duration_seconds': round((row['total_duration'] or 0) / answered) if answered > 0 else 0,
                })

            return result

    def get_queue_stats_report(self, start_date: str, end_date: str) -> list[dict]:
        """Get per-queue statistics for a date range.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            List of dicts with per-queue stats, sorted by total calls desc
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls,
                    SUM(total_wait_seconds) as total_wait,
                    MAX(max_wait_seconds) as max_wait,
                    SUM(answered_within_15s) as within_15s,
                    SUM(answered_within_30s) as within_30s,
                    SUM(answered_within_60s) as within_60s,
                    SUM(answered_within_90s) as within_90s,
                    SUM(answered_over_90s) as over_90s
                FROM daily_call_stats
                WHERE stat_date BETWEEN ? AND ?
                  AND queue_id IS NOT NULL
                GROUP BY queue_id, queue_name
                ORDER BY SUM(total_calls) DESC
            """, (start_date, end_date)).fetchall()

            result = []
            for row in rows:
                total = row['total_calls'] or 0
                answered = row['answered_calls'] or 0

                result.append({
                    'queue_id': row['queue_id'],
                    'queue_name': row['queue_name'] or f"Queue {row['queue_id']}",
                    'total_calls': total,
                    'answered_calls': answered,
                    'abandoned_calls': row['abandoned_calls'] or 0,
                    'timeout_calls': row['timeout_calls'] or 0,
                    'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                    'avg_wait_seconds': round((row['total_wait'] or 0) / total) if total > 0 else 0,
                    'max_wait_seconds': row['max_wait'] or 0,
                })

            return result

    def get_hourly_distribution(self, start_date: str, end_date: str,
                                queue_id: int = None) -> list[dict]:
        """Get hourly call distribution for charting.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            queue_id: Optional queue filter

        Returns:
            List of 24 dicts (one per hour) with call counts
        """
        with self._get_conn() as conn:
            query = """
                SELECT
                    stat_hour,
                    SUM(total_calls) as total_calls,
                    SUM(answered_calls) as answered_calls,
                    SUM(abandoned_calls) as abandoned_calls,
                    SUM(timeout_calls) as timeout_calls
                FROM hourly_call_stats
                WHERE stat_date BETWEEN ? AND ?
            """
            params = [start_date, end_date]

            if queue_id:
                query += " AND queue_id = ?"
                params.append(queue_id)

            query += " GROUP BY stat_hour ORDER BY stat_hour"

            rows = conn.execute(query, params).fetchall()

            # Create a dict for quick lookup
            hour_data = {row['stat_hour']: row for row in rows}

            # Return all 24 hours, filling in zeros for missing hours
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

    def get_realtime_stats_today(self) -> dict:
        """Get real-time statistics for today from live data.

        This pulls directly from queued_calls and recording_log for
        today's data that hasn't been aggregated yet.

        Returns:
            Dict with today's statistics
        """
        today = datetime.utcnow().strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            # Queue stats from queued_calls
            queue_row = conn.execute("""
                SELECT
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(COALESCE(wait_seconds, 0)) as total_wait,
                    MAX(wait_seconds) as max_wait,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds <= 15 THEN 1 ELSE 0 END) as within_15s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 15 AND wait_seconds <= 30 THEN 1 ELSE 0 END) as within_30s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 30 AND wait_seconds <= 60 THEN 1 ELSE 0 END) as within_60s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 60 AND wait_seconds <= 90 THEN 1 ELSE 0 END) as within_90s,
                    SUM(CASE WHEN status = 'answered' AND wait_seconds > 90 THEN 1 ELSE 0 END) as over_90s,
                    SUM(CASE WHEN status = 'abandoned' THEN COALESCE(wait_seconds, 0) ELSE 0 END) as abandoned_wait,
                    MAX(CASE WHEN status = 'answered' THEN wait_seconds ELSE 0 END) as max_answered_wait,
                    MAX(CASE WHEN status = 'abandoned' THEN wait_seconds ELSE 0 END) as max_abandoned_wait
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
            """, (today,)).fetchone()

            # Duration from recording_log
            recording_row = conn.execute("""
                SELECT
                    SUM(COALESCE(duration_seconds, 0)) as total_duration,
                    SUM(CASE WHEN call_type = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN call_type = 'outbound' THEN 1 ELSE 0 END) as outbound_calls
                FROM recording_log
                WHERE DATE(created_at) = ?
                  AND call_type IN ('inbound', 'outbound')
            """, (today,)).fetchone()

            total = queue_row['total_calls'] or 0
            answered = queue_row['answered_calls'] or 0
            abandoned = queue_row['abandoned_calls'] or 0
            timeout = queue_row['timeout_calls'] or 0

            return {
                'date': today,
                'is_realtime': True,
                'total_calls': total,
                'answered_calls': answered,
                'abandoned_calls': abandoned,
                'timeout_calls': timeout,
                'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                'abandoned_rate': round(abandoned / total * 100, 1) if total > 0 else 0,
                'timeout_rate': round(timeout / total * 100, 1) if total > 0 else 0,
                'total_duration_seconds': recording_row['total_duration'] or 0,
                'avg_duration_seconds': round((recording_row['total_duration'] or 0) / answered) if answered > 0 else 0,
                'total_wait_seconds': queue_row['total_wait'] or 0,
                'avg_wait_seconds': round((queue_row['total_wait'] or 0) / total) if total > 0 else 0,
                'avg_answered_wait_seconds': round((queue_row['total_wait'] or 0) / answered) if answered > 0 else 0,
                'avg_abandoned_wait_seconds': round((queue_row['abandoned_wait'] or 0) / abandoned) if abandoned > 0 else 0,
                'max_wait_seconds': queue_row['max_wait'] or 0,
                'max_answered_wait_seconds': queue_row['max_answered_wait'] or 0,
                'max_abandoned_wait_seconds': queue_row['max_abandoned_wait'] or 0,
                'answer_speed': {
                    'within_15s': queue_row['within_15s'] or 0,
                    'within_30s': queue_row['within_30s'] or 0,
                    'within_60s': queue_row['within_60s'] or 0,
                    'within_90s': queue_row['within_90s'] or 0,
                    'over_90s': queue_row['over_90s'] or 0,
                },
                'inbound_calls': recording_row['inbound_calls'] or 0,
                'outbound_calls': recording_row['outbound_calls'] or 0,
            }

    def get_realtime_agent_stats_today(self) -> list[dict]:
        """Get real-time per-agent statistics for today.

        Returns:
            List of dicts with per-agent stats for today
        """
        today = datetime.utcnow().strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            # Get agent stats from queued_calls
            queue_rows = conn.execute("""
                SELECT
                    answered_by as agent_email,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status IN ('abandoned', 'timeout') THEN 1 ELSE 0 END) as missed_calls
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                  AND answered_by IS NOT NULL
                GROUP BY answered_by
            """, (today,)).fetchall()

            # Get duration from recording_log
            recording_rows = conn.execute("""
                SELECT
                    staff_email,
                    SUM(COALESCE(duration_seconds, 0)) as total_duration
                FROM recording_log
                WHERE DATE(created_at) = ?
                  AND staff_email IS NOT NULL
                  AND call_type IN ('inbound', 'outbound')
                GROUP BY staff_email
            """, (today,)).fetchall()

            duration_by_agent = {row['staff_email']: row['total_duration'] for row in recording_rows}

            result = []
            for row in queue_rows:
                agent = row['agent_email']
                answered = row['answered_calls'] or 0
                duration = duration_by_agent.get(agent, 0)

                result.append({
                    'agent_email': agent,
                    'total_calls': row['total_calls'] or 0,
                    'answered_calls': answered,
                    'missed_calls': row['missed_calls'] or 0,
                    'total_duration_seconds': duration,
                    'avg_duration_seconds': round(duration / answered) if answered > 0 else 0,
                })

            # Sort by answered calls descending
            result.sort(key=lambda x: x['answered_calls'], reverse=True)
            return result

    def get_realtime_queue_stats_today(self) -> list[dict]:
        """Get real-time per-queue statistics for today.

        Returns:
            List of dicts with per-queue stats for today
        """
        today = datetime.utcnow().strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    queue_id,
                    queue_name,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(COALESCE(wait_seconds, 0)) as total_wait,
                    MAX(wait_seconds) as max_wait
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                  AND queue_id IS NOT NULL
                GROUP BY queue_id, queue_name
                ORDER BY COUNT(*) DESC
            """, (today,)).fetchall()

            result = []
            for row in rows:
                total = row['total_calls'] or 0
                answered = row['answered_calls'] or 0

                result.append({
                    'queue_id': row['queue_id'],
                    'queue_name': row['queue_name'] or f"Queue {row['queue_id']}",
                    'total_calls': total,
                    'answered_calls': answered,
                    'abandoned_calls': row['abandoned_calls'] or 0,
                    'timeout_calls': row['timeout_calls'] or 0,
                    'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                    'avg_wait_seconds': round((row['total_wait'] or 0) / total) if total > 0 else 0,
                    'max_wait_seconds': row['max_wait'] or 0,
                })

            return result

    def get_realtime_hourly_today(self) -> list[dict]:
        """Get real-time hourly distribution for today.

        Returns:
            List of 24 dicts with hourly call counts for today
        """
        today = datetime.utcnow().strftime('%Y-%m-%d')

        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    CAST(strftime('%H', enqueued_at) AS INTEGER) as hour,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status = 'abandoned' THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout_calls
                FROM queued_calls
                WHERE DATE(enqueued_at) = ?
                GROUP BY hour
                ORDER BY hour
            """, (today,)).fetchall()

            hour_data = {row['hour']: row for row in rows}

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
    # Call Log - Comprehensive call tracking
    # =========================================================================

    def log_call(self, data: dict) -> int:
        """Log a call to the comprehensive call_log table."""
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO call_log (
                    call_sid, parent_call_sid, direction, call_type,
                    from_number, to_number, phone_number_id,
                    queue_id, queue_name, call_flow_id,
                    status, agent_email,
                    customer_id, customer_name, customer_email,
                    conference_name, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('call_sid'),
                data.get('parent_call_sid'),
                data.get('direction'),
                data.get('call_type'),
                data.get('from_number'),
                data.get('to_number'),
                data.get('phone_number_id'),
                data.get('queue_id'),
                data.get('queue_name'),
                data.get('call_flow_id'),
                data.get('status', 'ringing'),
                data.get('agent_email'),
                data.get('customer_id'),
                data.get('customer_name'),
                data.get('customer_email'),
                data.get('conference_name'),
                data.get('started_at', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')),
            ))
            conn.commit()
            return cursor.lastrowid

    def get_call_log_field(self, call_sid: str, field: str):
        """Get a single field from a call_log entry by call SID."""
        with self._get_conn() as conn:
            row = conn.execute(
                f"SELECT {field} FROM call_log WHERE call_sid = ?", (call_sid,)
            ).fetchone()
            return row[field] if row else None

    def update_call_log(self, call_sid: str, updates: dict) -> None:
        """Update a call_log entry."""
        if not updates:
            return
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        set_clauses = []
        values = []
        for key, value in updates.items():
            if key not in ('call_sid', 'id', 'created_at'):
                set_clauses.append(f"{key} = ?")
                # Convert 'CURRENT_TIMESTAMP' string to actual datetime
                if value == 'CURRENT_TIMESTAMP':
                    value = now
                values.append(value)
        if not set_clauses:
            return
        set_clauses.append("updated_at = ?")
        values.append(datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
        values.append(call_sid)
        with self._get_conn() as conn:
            conn.execute(f"UPDATE call_log SET {', '.join(set_clauses)} WHERE call_sid = ?", values)
            conn.commit()

    def complete_call(self, call_sid: str, status: str, agent_email: str = None,
                      talk_seconds: int = None) -> None:
        """Mark a call as completed with final status and duration."""
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        with self._get_conn() as conn:
            row = conn.execute("SELECT started_at, answered_at FROM call_log WHERE call_sid = ?", (call_sid,)).fetchone()
            updates = {'status': status, 'ended_at': now, 'updated_at': now}
            if row and row['started_at']:
                try:
                    start_dt = datetime.fromisoformat(row['started_at'])
                    if row['answered_at']:
                        answer_dt = datetime.fromisoformat(row['answered_at'])
                        updates['ring_seconds'] = int((answer_dt - start_dt).total_seconds())
                    else:
                        updates['ring_seconds'] = int((datetime.fromisoformat(now) - start_dt).total_seconds())
                except (ValueError, TypeError):
                    pass
            if status == 'answered':
                if not row or not row['answered_at']:
                    updates['answered_at'] = now
                if agent_email:
                    updates['agent_email'] = agent_email
            if talk_seconds is not None:
                updates['talk_seconds'] = talk_seconds
                updates['total_seconds'] = (updates.get('ring_seconds', 0) or 0) + talk_seconds
            else:
                # Calculate talk_seconds as total_duration - ring_seconds
                # (answered_at is unreliable for conference-based calls — can arrive after ended_at)
                if row and row['started_at']:
                    try:
                        start_dt = datetime.fromisoformat(row['started_at'])
                        ended_dt = datetime.fromisoformat(now)
                        total_secs = max(0, int((ended_dt - start_dt).total_seconds()))
                        ring_secs = updates.get('ring_seconds', 0) or 0
                        calculated_talk = max(0, total_secs - ring_secs)
                        updates['talk_seconds'] = calculated_talk
                        updates['total_seconds'] = total_secs
                    except (ValueError, TypeError):
                        if 'ring_seconds' in updates:
                            updates['total_seconds'] = updates['ring_seconds']
                elif 'ring_seconds' in updates:
                    updates['total_seconds'] = updates['ring_seconds']
            set_clauses = [f"{k} = ?" for k in updates.keys()]
            values = list(updates.values()) + [call_sid]
            conn.execute(f"UPDATE call_log SET {', '.join(set_clauses)} WHERE call_sid = ?", values)
            conn.commit()

    def get_active_calls(self) -> list[dict]:
        """Get calls currently in progress (not yet ended).

        Filters:
        - ended_at IS NULL (not completed)
        - Must have an agent (otherwise we can't show who's busy)
        - Auto-cleans stale ringing calls (>2 min) on each query
        - Deduplicates per agent, keeping only the most recent call
        """
        with self._get_conn() as conn:
            # Clean up stale calls:
            # - Ringing > 2 min is impossible (Twilio gives up sooner)
            # - Answered calls > 2 hours old without ended_at are stale
            #   (status callback likely never fired for transferred/SIP calls)
            conn.execute("""
                UPDATE call_log
                SET status = 'missed', ended_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE ended_at IS NULL
                  AND status = 'ringing'
                  AND started_at < datetime('now', '-2 minutes')
            """)
            conn.execute("""
                UPDATE call_log
                SET status = 'completed', ended_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE ended_at IS NULL
                  AND status = 'answered'
                  AND started_at < datetime('now', '-2 hours')
            """)
            conn.commit()

            rows = conn.execute("""
                SELECT cl.call_sid, cl.direction, cl.from_number, cl.to_number,
                       cl.agent_email, cl.status, cl.started_at, cl.answered_at,
                       cl.customer_name, cl.queue_name,
                       u.staff_email as resolved_email
                FROM call_log cl
                LEFT JOIN users u
                    ON cl.agent_email = 'sip:' || u.username
                WHERE cl.ended_at IS NULL
                  AND cl.agent_email IS NOT NULL
                  AND cl.status = 'answered'
                ORDER BY cl.started_at DESC
            """).fetchall()

            # Deduplicate: keep only the most recent call per agent
            seen_agents = set()
            result = []
            for row in rows:
                row_dict = dict(row)
                # Resolve SIP identities (sip:username) to staff email
                if row_dict.get('resolved_email'):
                    row_dict['agent_email'] = row_dict['resolved_email']
                del row_dict['resolved_email']

                agent = row_dict['agent_email']
                if agent in seen_agents:
                    continue
                seen_agents.add(agent)
                result.append(row_dict)
            return result

    def close_stale_calls(self, active_sids: set[str]) -> int:
        """Close call_log entries for calls no longer active in Twilio.

        Any call_log entry with ended_at IS NULL whose call_sid is NOT in
        active_sids is marked as completed. Returns the number of rows updated.
        """
        with self._get_conn() as conn:
            # Always clean up stale ringing calls regardless
            conn.execute("""
                UPDATE call_log
                SET status = 'missed', ended_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE ended_at IS NULL
                  AND status = 'ringing'
                  AND started_at < datetime('now', '-2 minutes')
            """)

            # Close calls that Twilio says are no longer active
            rows = conn.execute("""
                SELECT call_sid FROM call_log
                WHERE ended_at IS NULL AND status = 'answered'
            """).fetchall()

            stale_sids = [r['call_sid'] for r in rows if r['call_sid'] not in active_sids]
            if stale_sids:
                placeholders = ','.join('?' * len(stale_sids))
                conn.execute(f"""
                    UPDATE call_log
                    SET status = 'completed', ended_at = datetime('now'),
                        updated_at = datetime('now')
                    WHERE call_sid IN ({placeholders})
                """, stale_sids)

            conn.commit()
            return len(stale_sids)

    def get_call_log_by_sids(self, sids: set[str]) -> dict:
        """Get call_log entries for a set of call SIDs.

        Returns a dict mapping call_sid -> row dict, with SIP agent
        identities resolved to staff emails.
        """
        if not sids:
            return {}

        with self._get_conn() as conn:
            placeholders = ','.join('?' * len(sids))
            rows = conn.execute(f"""
                SELECT cl.call_sid, cl.direction, cl.from_number, cl.to_number,
                       cl.agent_email, cl.status, cl.started_at, cl.answered_at,
                       cl.customer_name, cl.queue_name,
                       u.staff_email as resolved_email
                FROM call_log cl
                LEFT JOIN users u
                    ON cl.agent_email = 'sip:' || u.username
                WHERE cl.call_sid IN ({placeholders})
            """, list(sids)).fetchall()

            result = {}
            for row in rows:
                d = dict(row)
                if d.get('resolved_email'):
                    d['agent_email'] = d['resolved_email']
                del d['resolved_email']
                result[d['call_sid']] = d
            return result

    def _build_queue_filter(self, queue_name=None, queue_names=None, agent_emails=None):
        """Build WHERE clause fragments for queue + outbound agent filtering.

        Returns (clause_str, params_list). The clause includes queue calls
        and outbound calls by the specified agents.
        """
        conditions = []
        params = []

        if queue_name:
            conditions.append("queue_name = ?")
            params.append(queue_name)
        elif queue_names:
            placeholders = ','.join('?' * len(queue_names))
            conditions.append(f"queue_name IN ({placeholders})")
            params.extend(queue_names)

        if agent_emails:
            placeholders = ','.join('?' * len(agent_emails))
            conditions.append(f"(direction = 'outbound' AND agent_email IN ({placeholders}))")
            params.extend(agent_emails)

        if conditions:
            return " AND (" + " OR ".join(conditions) + ")", params
        return "", []

    def get_call_log_stats(self, start_utc: str, end_utc: str, queue_name: str = None, queue_names: list = None, agent_emails: list = None) -> dict:
        """Get call statistics from call_log for a UTC timestamp range."""
        with self._get_conn() as conn:
            where = "started_at >= ? AND started_at <= ?"
            params = [start_utc, end_utc]
            filter_clause, filter_params = self._build_queue_filter(queue_name, queue_names, agent_emails)
            where += filter_clause
            params.extend(filter_params)

            row = conn.execute(f"""
                SELECT
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) as outbound_calls,
                    SUM(CASE WHEN status IN ('answered', 'completed') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'voicemail' THEN 1 ELSE 0 END) as timeout_calls,
                    SUM(COALESCE(talk_seconds, 0)) as total_duration_seconds,
                    SUM(COALESCE(ring_seconds, 0)) as total_wait_seconds,
                    AVG(CASE WHEN status IN ('answered', 'completed') THEN talk_seconds END) as avg_duration_seconds,
                    AVG(CASE WHEN status IN ('answered', 'completed') THEN ring_seconds END) as avg_answered_wait_seconds,
                    MAX(CASE WHEN status IN ('answered', 'completed') THEN ring_seconds END) as max_answered_wait_seconds,
                    AVG(CASE WHEN status IN ('abandoned', 'missed') THEN ring_seconds END) as avg_abandoned_wait_seconds,
                    MAX(CASE WHEN status IN ('abandoned', 'missed') THEN ring_seconds END) as max_abandoned_wait_seconds,
                    AVG(ring_seconds) as avg_wait_seconds,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds <= 15 THEN 1 ELSE 0 END) as within_15s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 15 AND ring_seconds <= 30 THEN 1 ELSE 0 END) as within_30s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 30 AND ring_seconds <= 60 THEN 1 ELSE 0 END) as within_60s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 60 AND ring_seconds <= 90 THEN 1 ELSE 0 END) as within_90s,
                    SUM(CASE WHEN status IN ('answered', 'completed') AND direction = 'inbound' AND ring_seconds > 90 THEN 1 ELSE 0 END) as over_90s
                FROM call_log WHERE {where}
            """, params).fetchone()
            total = row['total_calls'] or 0
            answered = row['answered_calls'] or 0
            abandoned = row['abandoned_calls'] or 0
            timeout = row['timeout_calls'] or 0
            return {
                'total_calls': total,
                'inbound_calls': row['inbound_calls'] or 0,
                'outbound_calls': row['outbound_calls'] or 0,
                'answered_calls': answered,
                'abandoned_calls': abandoned,
                'timeout_calls': timeout,
                'answer_rate': round(answered / total * 100, 1) if total > 0 else 0,
                'abandoned_rate': round(abandoned / total * 100, 1) if total > 0 else 0,
                'timeout_rate': round(timeout / total * 100, 1) if total > 0 else 0,
                'total_duration_seconds': row['total_duration_seconds'] or 0,
                'total_wait_seconds': row['total_wait_seconds'] or 0,
                'avg_duration_seconds': int(row['avg_duration_seconds'] or 0),
                'avg_wait_seconds': int(row['avg_wait_seconds'] or 0),
                'avg_answered_wait_seconds': int(row['avg_answered_wait_seconds'] or 0),
                'max_answered_wait_seconds': int(row['max_answered_wait_seconds'] or 0),
                'avg_abandoned_wait_seconds': int(row['avg_abandoned_wait_seconds'] or 0),
                'max_abandoned_wait_seconds': int(row['max_abandoned_wait_seconds'] or 0),
                'answer_speed': {
                    'within_15s': row['within_15s'] or 0,
                    'within_30s': row['within_30s'] or 0,
                    'within_60s': row['within_60s'] or 0,
                    'within_90s': row['within_90s'] or 0,
                    'over_90s': row['over_90s'] or 0,
                },
            }

    def get_call_log_by_agent(self, start_utc: str, end_utc: str, queue_name: str = None,
                              queue_names: list = None, agent_emails: list = None,
                              team_emails: list = None) -> list[dict]:
        """Get call statistics grouped by agent.

        Resolves SIP identities (sip:username@...) to staff emails so
        desk phone and browser calls are merged into one agent entry.

        Args:
            team_emails: If provided, filters agents to this list (from Peter's
                        manager reportees). Overrides queue-based agent filtering
                        for the agent stats only.
        """
        with self._get_conn() as conn:
            # Build a SIP username -> email mapping for resolution
            users = conn.execute(
                "SELECT username, staff_email FROM users WHERE username IS NOT NULL"
            ).fetchall()
            sip_map = {}
            for u in users:
                sip_map[f"sip:{u['username']}"] = u['staff_email']

            where = "started_at >= ? AND started_at <= ? AND agent_email IS NOT NULL"
            params = [start_utc, end_utc]

            if team_emails:
                # Team-based filtering: show all calls (inbound + outbound) for team members
                # Build SIP variants so desk phone calls are included
                all_identities = list(team_emails)
                for email in team_emails:
                    # Find SIP identities that map to this email
                    for sip_key, mapped_email in sip_map.items():
                        if mapped_email == email:
                            all_identities.append(sip_key)
                            # Also add with domain suffix variants
                            all_identities.append(sip_key + '@' + email.split('@')[1] if '@' in email else sip_key)

                placeholders = ','.join('?' * len(all_identities))
                where += f" AND agent_email IN ({placeholders})"
                params.extend(all_identities)
            else:
                # Queue-based filtering (legacy behavior)
                filter_clause, filter_params = self._build_queue_filter(queue_name, queue_names, agent_emails)
                where += filter_clause
                params.extend(filter_params)

            # Exclude auto-ring attempts (outbound calls with no talk time
            # that ended as missed/no-answer — these are queue ring attempts, not real calls)
            real_call_where = where + " AND NOT (direction = 'outbound' AND status IN ('missed', 'abandoned') AND (talk_seconds IS NULL OR talk_seconds = 0))"

            rows = conn.execute(f"""
                SELECT agent_email, COUNT(*) as total_calls,
                    SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) as inbound_calls,
                    SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) as outbound_calls,
                    SUM(CASE WHEN status IN ('answered', 'completed') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN direction = 'inbound' AND status IN ('answered', 'completed') THEN 1 ELSE 0 END) as inbound_answered,
                    SUM(CASE WHEN direction = 'inbound' AND status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as missed_calls,
                    SUM(COALESCE(talk_seconds, 0)) as total_duration_seconds,
                    SUM(CASE WHEN direction = 'inbound' THEN COALESCE(talk_seconds, 0) ELSE 0 END) as inbound_duration_seconds,
                    SUM(CASE WHEN direction = 'outbound' THEN COALESCE(talk_seconds, 0) ELSE 0 END) as outbound_duration_seconds,
                    AVG(CASE WHEN talk_seconds > 0 THEN talk_seconds END) as avg_duration_seconds,
                    AVG(CASE WHEN direction = 'outbound' AND talk_seconds > 0 THEN talk_seconds END) as outbound_avg_duration_seconds
                FROM call_log WHERE {real_call_where}
                GROUP BY agent_email ORDER BY total_calls DESC
            """, params).fetchall()

            # Merge SIP entries with their email counterparts
            merged = {}
            for row in rows:
                d = dict(row)
                email = d['agent_email']

                # Resolve SIP URI: "sip:chris_savage@domain" -> check sip_map for "sip:chris_savage"
                if email.startswith('sip:'):
                    sip_key = email.split('@')[0] if '@' in email else email
                    email = sip_map.get(sip_key, email)
                    d['agent_email'] = email

                if email in merged:
                    # Merge with existing entry
                    m = merged[email]
                    m['total_calls'] += d['total_calls']
                    m['inbound_calls'] += d['inbound_calls']
                    m['outbound_calls'] += d['outbound_calls']
                    m['answered_calls'] += d['answered_calls']
                    m['inbound_answered'] = (m.get('inbound_answered') or 0) + (d.get('inbound_answered') or 0)
                    m['missed_calls'] += d['missed_calls']
                    m['total_duration_seconds'] += d['total_duration_seconds']
                    m['inbound_duration_seconds'] = (m.get('inbound_duration_seconds') or 0) + (d.get('inbound_duration_seconds') or 0)
                    m['outbound_duration_seconds'] = (m.get('outbound_duration_seconds') or 0) + (d.get('outbound_duration_seconds') or 0)
                    # Recalculate average
                    if m['answered_calls'] > 0:
                        m['avg_duration_seconds'] = m['total_duration_seconds'] // m['answered_calls']
                    if m['outbound_calls'] > 0 and m['outbound_duration_seconds'] > 0:
                        m['outbound_avg_duration_seconds'] = m['outbound_duration_seconds'] // m['outbound_calls']
                else:
                    d['avg_duration_seconds'] = int(d['avg_duration_seconds'] or 0)
                    d['outbound_avg_duration_seconds'] = int(d['outbound_avg_duration_seconds'] or 0)
                    d['total_duration_seconds'] = int(d['total_duration_seconds'] or 0)
                    d['inbound_duration_seconds'] = int(d.get('inbound_duration_seconds') or 0)
                    d['outbound_duration_seconds'] = int(d.get('outbound_duration_seconds') or 0)
                    d['inbound_answered'] = int(d.get('inbound_answered') or 0)
                    merged[email] = d

            result = sorted(merged.values(), key=lambda x: x['total_calls'], reverse=True)
            return result

    def get_call_log_hourly(self, start_utc: str, end_utc: str, tz_offset_hours: int = 11, queue_name: str = None, queue_names: list = None, agent_emails: list = None) -> list[dict]:
        """Get hourly call distribution in local time."""
        with self._get_conn() as conn:
            where = "started_at >= ? AND started_at <= ?"
            params = [start_utc, end_utc]
            filter_clause, filter_params = self._build_queue_filter(queue_name, queue_names, agent_emails)
            where += filter_clause
            params.extend(filter_params)

            # Convert UTC hour to local hour by adding timezone offset
            rows = conn.execute(f"""
                SELECT CAST((strftime('%H', started_at) + {tz_offset_hours}) % 24 AS INTEGER) as hour,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN status IN ('answered', 'completed') THEN 1 ELSE 0 END) as answered_calls,
                    SUM(CASE WHEN status IN ('abandoned', 'missed') THEN 1 ELSE 0 END) as abandoned_calls,
                    SUM(CASE WHEN status = 'voicemail' THEN 1 ELSE 0 END) as timeout_calls
                FROM call_log WHERE {where} GROUP BY hour ORDER BY hour
            """, params).fetchall()
            hour_data = {row['hour']: row for row in rows}
            result = []
            for hour in range(24):
                if hour in hour_data:
                    row = hour_data[hour]
                    result.append({'hour': hour, 'label': f"{hour:02d}:00", 'total_calls': row['total_calls'] or 0,
                        'answered_calls': row['answered_calls'] or 0, 'abandoned_calls': row['abandoned_calls'] or 0,
                        'timeout_calls': row['timeout_calls'] or 0})
                else:
                    result.append({'hour': hour, 'label': f"{hour:02d}:00", 'total_calls': 0,
                        'answered_calls': 0, 'abandoned_calls': 0, 'timeout_calls': 0})
            return result

    def get_my_call_history(self, agent_email: str, limit: int = 50) -> list[dict]:
        """Get recent call history for a specific agent.

        Returns calls where the agent was involved — either as the agent_email
        on inbound calls, or as the caller on outbound calls.

        Args:
            agent_email: The agent's email address
            limit: Maximum number of calls to return

        Returns:
            List of call dicts with direction, status, numbers, timing, etc.
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    call_sid, direction, call_type, status,
                    from_number, to_number,
                    started_at, answered_at, ended_at,
                    ring_seconds, talk_seconds, total_seconds,
                    customer_name, queue_name, is_recorded
                FROM call_log
                WHERE agent_email = ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (agent_email, limit)).fetchall()
            return [dict(row) for row in rows]

    def get_call_history_by_phone(self, phone_number: str, limit: int = 10) -> dict:
        """Get call history for a phone number.

        Searches both from_number and to_number to capture
        inbound and outbound calls with this customer.

        Args:
            phone_number: Phone number to search for (any format)
            limit: Maximum number of recent calls to return

        Returns:
            dict with:
                - total_calls: Total call count with this number
                - calls: List of recent calls (up to limit)
                - last_call_date: Date of most recent call
        """
        # Extract the core number (last 9 digits) to match any format
        # +61412345678, 0412345678, 412345678 all have the same last 9 digits
        cleaned = phone_number.replace(' ', '').replace('-', '').replace('+', '')
        # Get last 9 digits (Australian mobile numbers)
        core_number = cleaned[-9:] if len(cleaned) >= 9 else cleaned

        with self._get_conn() as conn:
            # Get recent calls (both directions)
            # Use LIKE with % to match partial numbers (handles format variations)
            cursor = conn.execute("""
                SELECT
                    call_sid,
                    direction,
                    status,
                    started_at,
                    talk_seconds,
                    agent_email,
                    customer_name,
                    queue_name
                FROM call_log
                WHERE from_number LIKE ? OR to_number LIKE ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (f'%{core_number}%', f'%{core_number}%', limit))

            calls = [dict(row) for row in cursor.fetchall()]

            # Get total count
            cursor = conn.execute("""
                SELECT COUNT(*) FROM call_log
                WHERE from_number LIKE ? OR to_number LIKE ?
            """, (f'%{core_number}%', f'%{core_number}%'))
            total = cursor.fetchone()[0]

            return {
                'total_calls': total,
                'calls': calls,
                'last_call_date': calls[0]['started_at'] if calls else None
            }


# Singleton instance (single-tenant mode)
_db = None


def get_db() -> Database:
    """Get the database for the current context.

    In multi-tenant mode: returns the tenant-scoped database from g.tenant.
    In single-tenant mode: returns the singleton database.
    """
    from rinq.config import config
    if config.multi_tenant:
        from rinq.tenant.context import get_current_tenant, get_tenant_db
        tenant = get_current_tenant()
        if tenant:
            return get_tenant_db()

    # Single-tenant fallback
    global _db
    if _db is None:
        _db = Database(db_path=config.database_path)
    return _db
