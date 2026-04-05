"""
Call recording service for managing recordings.

Handles:
- Processing completed recordings from Twilio
- Sending recordings to Google Group storage via Mabel
- Tracking recordings in the database
- Starting/stopping recording on active calls
"""

import base64
import logging
import os
import requests
from datetime import datetime

from rinq.config import config
from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service, twilio_list

logger = logging.getLogger(__name__)


class RecordingService:
    """Service for managing call recordings."""

    # Directory to store local copies of recordings
    RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'recordings')

    def __init__(self):
        self._drive_service = None

    @property
    def db(self):
        return get_db()
        # Ensure recordings directory exists
        os.makedirs(self.RECORDINGS_DIR, exist_ok=True)

    @property
    def drive_service(self):
        """Get Drive service for cloud storage."""
        if self._drive_service is None:
            from rinq.services.drive_service import drive_service
            self._drive_service = drive_service
        return self._drive_service

    def _save_recording_locally(self, recording_sid: str, audio_content: bytes) -> str:
        """Save recording audio to local storage.

        Args:
            recording_sid: Twilio recording SID (used as filename)
            audio_content: Raw MP3 bytes

        Returns:
            Relative path to saved file (relative to RECORDINGS_DIR)
        """
        filename = f"{recording_sid}.mp3"
        filepath = os.path.join(self.RECORDINGS_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(audio_content)
        logger.info(f"Saved recording locally: {filepath} ({len(audio_content)} bytes)")
        return filename  # Return just filename, not full path

    def get_recording_file_path(self, recording_sid: str) -> str | None:
        """Get the full path to a recording file if it exists."""
        filename = f"{recording_sid}.mp3"
        filepath = os.path.join(self.RECORDINGS_DIR, filename)
        if os.path.exists(filepath):
            return filepath
        return None

    def _upload_to_drive(self, recording_sid: str, audio_content: bytes,
                         metadata: dict) -> str | None:
        """Upload recording to Google Drive.

        Args:
            recording_sid: Twilio recording SID
            audio_content: MP3 audio bytes
            metadata: Call metadata for Drive file description

        Returns:
            Drive file ID if successful, None otherwise
        """
        try:
            result = self.drive_service.upload_recording(recording_sid, audio_content, metadata)

            if 'error' in result:
                logger.error(f"Failed to upload recording to Drive: {result['error']}")
                return None

            drive_file_id = result.get('id')
            logger.info(f"Uploaded recording to Drive: {drive_file_id}")
            return drive_file_id

        except Exception as e:
            logger.error(f"Failed to upload recording to Drive: {e}")
            return None

    def fetch_from_drive(self, drive_file_id: str) -> bytes | None:
        """Fetch a recording from Google Drive.

        Args:
            drive_file_id: Google Drive file ID

        Returns:
            Audio content bytes if successful, None otherwise
        """
        try:
            result = self.drive_service.download_recording(drive_file_id)

            if 'error' in result:
                logger.error(f"Failed to fetch recording from Drive: {result['error']}")
                return None

            logger.info(f"Fetched recording from Drive: {drive_file_id}")
            return result['content']

        except Exception as e:
            logger.error(f"Failed to fetch recording from Drive: {e}")
            return None

    def process_completed_recording(self, recording_sid: str, call_sid: str,
                                     recording_url: str, duration: int,
                                     from_number: str, to_number: str,
                                     call_type: str, staff_email: str = None,
                                     staff_name: str = None,
                                     caller_name: str = None) -> dict:
        """Process a completed recording from Twilio.

        This is called by the recording-status webhook when Twilio
        finishes processing a recording.

        Storage tiers:
        1. Local (3 weeks) - hot cache for instant playback
        2. Google Drive (12 months) - warm storage with API access
        3. Google Groups (forever) - cold archive via email

        Steps:
        1. Download recording from Twilio
        2. Save locally for instant playback
        3. Upload to Google Drive for 12-month warm storage
        4. Email to Google Group for permanent archive
        5. Log in database
        6. Delete from Twilio to save storage

        Args:
            recording_sid: Twilio recording SID
            call_sid: Twilio call SID
            recording_url: URL to download the recording
            duration: Recording duration in seconds
            from_number: Caller phone number
            to_number: Called phone number
            call_type: 'inbound', 'outbound', or 'internal'
            staff_email: Staff member on the call
            staff_name: Staff member's display name
            caller_name: Customer/caller name from CRM lookup

        Returns:
            Dict with 'success' and details or 'error'
        """
        try:
            # 1. Download recording from Twilio
            logger.info(f"Downloading recording {recording_sid} from Twilio")
            audio_url = recording_url if recording_url.endswith('.mp3') else f"{recording_url}.mp3"

            # Twilio requires authentication for recording downloads
            from rinq.tenant.context import get_twilio_config
            response = requests.get(
                audio_url,
                auth=(get_twilio_config('twilio_account_sid'), get_twilio_config('twilio_auth_token')),
                timeout=60
            )
            response.raise_for_status()
            audio_content = response.content

            logger.info(f"Downloaded recording: {len(audio_content)} bytes")

            # 2. Save recording locally as hot cache for instant playback
            local_file_path = self._save_recording_locally(recording_sid, audio_content)

            # Format duration as M:SS for display
            minutes = duration // 60
            seconds = duration % 60
            duration_str = f"{minutes}:{seconds:02d}"
            staff_display = staff_name or (staff_email.split('@')[0] if staff_email else 'Staff')

            # 3. Upload to Google Drive (12-month warm storage)
            drive_file_id = self._upload_to_drive(recording_sid, audio_content, {
                'call_type': call_type,
                'from_number': from_number,
                'to_number': to_number,
                'duration': duration,
                'staff_name': staff_display,
                'call_sid': call_sid,
            })
            if drive_file_id:
                logger.info(f"Recording uploaded to Drive: {drive_file_id}")
            else:
                logger.warning(f"Failed to upload recording to Drive - will only be in local + Groups")

            # 4. Email to Google Group via Mabel (permanent archive)
            # Build email subject
            if call_type == 'inbound':
                subject = f"📞 Inbound Call Recording - {from_number} → {staff_display} ({duration_str})"
            elif call_type == 'outbound':
                subject = f"📤 Outbound Call Recording - {staff_display} → {to_number} ({duration_str})"
            else:
                subject = f"📞 Call Recording - {from_number} ↔ {to_number} ({duration_str})"

            # Build email body
            body = f"""Call Recording

Type: {call_type.title() if call_type else 'Unknown'}
From: {from_number or 'Unknown'}
To: {to_number or 'Unknown'}
Duration: {duration_str}
Staff: {staff_display}

Call SID: {call_sid}
Recording SID: {recording_sid}"""

            filename = f"recording_{recording_sid}.mp3"

            # Send via email service
            google_message_id = None
            from rinq.integrations import get_email_service
            email_svc = get_email_service()
            # Use tenant's recordings email if set, fall back to config
            try:
                from flask import g
                tenant = getattr(g, 'tenant', None)
                recordings_email = (tenant.get('recordings_group_email') if tenant else None) or config.recordings_group_email
            except RuntimeError:
                recordings_email = config.recordings_group_email
            if email_svc and recordings_email:
                google_message_id = email_svc.send_email(
                    to=recordings_email,
                    subject=subject,
                    text_body=body,
                    attachments=[{
                        'filename': filename,
                        'content_type': 'audio/mpeg',
                        'content_base64': base64.b64encode(audio_content).decode('utf-8'),
                    }],
                    metadata={
                        'caller': 'tina',
                        'recording_sid': recording_sid,
                        'call_sid': call_sid,
                    },
                )

            # 5. Log in database
            now = datetime.utcnow().isoformat()
            log_data = {
                'recording_sid': recording_sid,
                'call_sid': call_sid,
                'from_number': from_number,
                'to_number': to_number,
                'duration_seconds': duration,
                'recording_url': recording_url,
                'emailed_to': recordings_email if google_message_id else None,
                'emailed_at': now if google_message_id else None,
                'deleted_from_twilio': 0,
                'created_at': now,
                'google_message_id': google_message_id,
                'call_type': call_type,
                'staff_email': staff_email,
                'staff_name': staff_name,
                'local_file_path': local_file_path,
                'caller_name': caller_name,
            }
            recording_id = self.db.log_recording(log_data)
            logger.info(f"Recording logged in database, id={recording_id}")

            # Update drive_file_id separately (column added in migration 021)
            if drive_file_id:
                self.db.update_recording_drive_file(recording_sid, drive_file_id)

            # 6. Delete from Twilio to save storage (only if we have at least one backup)
            deleted_from_twilio = False
            if google_message_id or drive_file_id:
                delete_result = get_twilio_service().delete_recording(recording_sid)
                if delete_result.get('success'):
                    self.db.mark_recording_deleted(recording_sid)
                    deleted_from_twilio = True
                    logger.info(f"Recording deleted from Twilio")
                else:
                    logger.warning(
                        f"Failed to delete recording from Twilio: "
                        f"{delete_result.get('error')}"
                    )

            return {
                'success': True,
                'recording_id': recording_id,
                'google_message_id': google_message_id,
                'drive_file_id': drive_file_id,
                'emailed': bool(google_message_id),
                'uploaded_to_drive': bool(drive_file_id),
                'deleted_from_twilio': deleted_from_twilio,
            }

        except requests.RequestException as e:
            logger.error(f"Failed to download recording from Twilio: {e}")
            return {'success': False, 'error': f'Failed to download recording: {e}'}
        except Exception as e:
            logger.exception(f"Error processing recording: {e}")
            return {'success': False, 'error': str(e)}

    def start_recording(self, call_sid: str) -> dict:
        """Start recording an active call.

        Uses Twilio's REST API to start recording on the call.

        Args:
            call_sid: The Twilio call SID to record

        Returns:
            Dict with 'success' and 'recording_sid' or 'error'
        """
        try:
            client = get_twilio_service().client
            recording = client.calls(call_sid).recordings.create(
                recording_status_callback=f"{config.webhook_base_url}/api/voice/recording-status",
                recording_status_callback_event=['completed', 'absent'],
            )
            logger.info(f"Started recording for call {call_sid}: {recording.sid}")
            return {
                'success': True,
                'recording_sid': recording.sid,
            }
        except Exception as e:
            logger.error(f"Failed to start recording for call {call_sid}: {e}")
            return {'success': False, 'error': str(e)}

    def stop_recording(self, call_sid: str) -> dict:
        """Stop recording an active call.

        Stops all active recordings on the call.

        Args:
            call_sid: The Twilio call SID

        Returns:
            Dict with 'success' and 'stopped_count' or 'error'
        """
        try:
            client = get_twilio_service().client
            recordings = [r for r in twilio_list(client.calls(call_sid).recordings) if r.status == 'in-progress']

            stopped = 0
            for recording in recordings:
                recording.update(status='stopped')
                stopped += 1
                logger.info(f"Stopped recording {recording.sid}")

            return {
                'success': True,
                'stopped_count': stopped,
            }
        except Exception as e:
            logger.error(f"Failed to stop recording for call {call_sid}: {e}")
            return {'success': False, 'error': str(e)}

    def get_recording_status(self, call_sid: str) -> dict:
        """Get recording status for a call.

        Args:
            call_sid: The Twilio call SID

        Returns:
            Dict with 'recording' (bool), 'recording_sid' (if recording)
        """
        try:
            client = get_twilio_service().client
            recordings = [r for r in twilio_list(client.calls(call_sid).recordings) if r.status == 'in-progress']

            if recordings:
                return {
                    'recording': True,
                    'recording_sid': recordings[0].sid,
                }
            return {'recording': False}
        except Exception as e:
            logger.error(f"Failed to get recording status for call {call_sid}: {e}")
            return {'recording': False, 'error': str(e)}

    def get_user_recording_preference(self, email: str) -> bool:
        """Get whether a user has recording enabled by default."""
        return self.db.get_user_recording_default(email)

    def set_user_recording_preference(self, email: str, enabled: bool,
                                       updated_by: str) -> None:
        """Set whether a user has recording enabled by default."""
        self.db.set_user_recording_default(email, enabled, updated_by)

    def purge_stale_recordings(self, days: int = 30) -> dict:
        """Purge local recording files that haven't been accessed recently.

        Deletes local cached files for recordings not accessed in the given
        number of days. The Google Group archive remains the permanent store.

        Args:
            days: Number of days since last access before purging (default 30)

        Returns:
            Dict with 'success', 'purged_count', and 'errors'
        """
        stale_recordings = self.db.get_stale_recordings(days=days)
        logger.info(f"Found {len(stale_recordings)} recordings to purge (not accessed in {days} days)")

        purged_count = 0
        errors = []

        for rec in stale_recordings:
            recording_sid = rec['recording_sid']
            local_path = rec.get('local_file_path')

            if not local_path:
                continue

            try:
                # Build full path and delete file
                full_path = os.path.join(self.RECORDINGS_DIR, local_path)
                if os.path.exists(full_path):
                    os.remove(full_path)
                    logger.info(f"Deleted local file for recording {recording_sid}")

                # Clear the local_file_path in database
                self.db.clear_recording_local_file(recording_sid)
                purged_count += 1

            except Exception as e:
                error_msg = f"Failed to purge recording {recording_sid}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        logger.info(f"Purged {purged_count} recordings, {len(errors)} errors")

        return {
            'success': len(errors) == 0,
            'purged_count': purged_count,
            'errors': errors if errors else None,
        }


# Singleton instance
recording_service = RecordingService()
