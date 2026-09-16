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
import shutil
import requests
from datetime import datetime, timezone
from urllib.parse import quote

from twilio.base.exceptions import TwilioException

from rinq.config import config
from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service, twilio_list

logger = logging.getLogger(__name__)

# How long a recording stays cached on local disk before the nightly purge
# drops it. Per-tenant, editable at /admin/storage; the constant is only the
# fallback for a tenant that has never set one.
RETENTION_SETTING_KEY = 'recording_retention_days'
DEFAULT_RETENTION_DAYS = 21

# Tenant-wide recording. When on, the SERVER starts a recording as each
# conversation connects, so it does not matter whether the staff member
# answered in the browser, on a desk phone or in a SIP softphone app.
#
# Default OFF, and while it is off nothing changes: the browser keeps
# auto-recording per the user's own preference, exactly as before. Turning it
# on is what makes recording device-independent — and it is also the point at
# which every customer is being recorded, so the greetings need to say so
# first. That is why this ships off rather than on.
RECORD_ALL_SETTING_KEY = 'record_all_calls'


def record_all_enabled(db) -> bool:
    """Is tenant-wide, device-independent recording switched on?

    Anything other than an explicit '1' reads as off. A setting we cannot read
    must never be treated as consent to record.
    """
    try:
        return db.get_bot_setting(RECORD_ALL_SETTING_KEY, '0') == '1'
    except Exception as e:
        logger.warning(f"Could not read {RECORD_ALL_SETTING_KEY} — treating as off: {e}")
        return False


def customer_leg_for_conference(conference_name: str, joining_call_sid: str,
                                role: str) -> str | None:
    """Work out which call leg to record for a conversation.

    We record the CUSTOMER's leg, because it lasts the whole conversation —
    agents come and go through transfers, and their legs end with them.

    The conference name carries the answer, but the prefix means different
    things on different paths, so this is a lookup table rather than a guess:

      hold_room_<sid>  inbound, <sid> is the CUSTOMER  (queue / auto-ring answer)
      call_<sid>       reached by an agent joining -> direct inbound, <sid> is
                       the CUSTOMER. An OUTBOUND conference is also named
                       call_<agent sid>, but on that path the agent joins with
                       inline TwiML and never reaches conference_join, so an
                       agent arriving here can only be direct inbound.
      anything else    a hold room or a transfer/consult room — a mid-call
                       move, not the start of a conversation. Recording is
                       already running on the customer leg, which survives the
                       move, so there is nothing to start.

    Returns the call SID to record, or None to do nothing.
    """
    if role == 'caller':
        # The leg arriving IS the customer — no derivation needed.
        return joining_call_sid

    if not conference_name:
        return None

    if conference_name.startswith('hold_room_'):
        return conference_name[len('hold_room_'):] or None

    # 'hold_' is a different room from 'hold_room_' and must not match here.
    if conference_name.startswith('call_'):
        return conference_name[len('call_'):] or None

    return None


def get_retention_days(db) -> int:
    """Read the tenant's local-cache retention window in days.

    Falls back to DEFAULT_RETENTION_DAYS for an unset, non-numeric or
    nonsensical value — a bad setting must never widen the purge window.
    """
    raw = db.get_bot_setting(RETENTION_SETTING_KEY)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        if raw is not None:
            logger.warning(f"Ignoring non-numeric {RETENTION_SETTING_KEY}: {raw!r}")
        return DEFAULT_RETENTION_DAYS
    return days if days >= 1 else DEFAULT_RETENTION_DAYS


class RecordingService:
    """Service for managing call recordings."""

    # Directory to store local copies of recordings
    RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'recordings')

    def __init__(self):
        self._drive_service = None
        os.makedirs(self.RECORDINGS_DIR, exist_ok=True)

    @property
    def db(self):
        return get_db()

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

            staff_display = staff_name or (staff_email.split('@')[0] if staff_email else 'Staff')

            # 3. Upload to Google Drive (12-month warm storage)
            drive_file_id = self._upload_to_drive(recording_sid, audio_content, {
                'call_type': call_type, 'from_number': from_number,
                'to_number': to_number, 'duration': duration,
                'staff_name': staff_display, 'call_sid': call_sid,
            })
            if drive_file_id:
                logger.info(f"Recording uploaded to Drive: {drive_file_id}")
            else:
                logger.warning(f"Failed to upload recording to Drive - will only be in local + Groups")

            # 4. Email to Google Group (permanent archive)
            google_message_id, recordings_email = self._archive_via_email(
                recording_sid, call_sid, audio_content,
                call_type, from_number, to_number, duration, staff_display,
            )

            # 5. Log in database
            recording_id = self._log_recording(
                recording_sid, call_sid, recording_url, from_number, to_number,
                duration, call_type, staff_email, staff_name, caller_name,
                local_file_path, google_message_id, recordings_email, drive_file_id,
            )

            # 6. Delete from Twilio (only if we have at least one backup)
            deleted_from_twilio = self._delete_from_twilio(
                recording_sid, google_message_id, drive_file_id,
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

    def _archive_via_email(self, recording_sid, call_sid, audio_content,
                           call_type, from_number, to_number, duration, staff_display):
        """Email recording to Google Group for permanent archive.

        Returns (google_message_id, recordings_email) tuple.
        """
        minutes, seconds = divmod(duration, 60)
        duration_str = f"{minutes}:{seconds:02d}"

        if call_type == 'inbound':
            subject = f"📞 Inbound Call Recording - {from_number} → {staff_display} ({duration_str})"
        elif call_type == 'outbound':
            subject = f"📤 Outbound Call Recording - {staff_display} → {to_number} ({duration_str})"
        else:
            subject = f"📞 Call Recording - {from_number} ↔ {to_number} ({duration_str})"

        body = (
            f"Call Recording\n\n"
            f"Type: {call_type.title() if call_type else 'Unknown'}\n"
            f"From: {from_number or 'Unknown'}\n"
            f"To: {to_number or 'Unknown'}\n"
            f"Duration: {duration_str}\n"
            f"Staff: {staff_display}\n\n"
            f"Call SID: {call_sid}\n"
            f"Recording SID: {recording_sid}"
        )

        try:
            from flask import g
            tenant = getattr(g, 'tenant', None)
            recordings_email = (tenant.get('recordings_group_email') if tenant else None) or config.recordings_group_email
        except RuntimeError:
            recordings_email = config.recordings_group_email

        google_message_id = None
        from rinq.integrations import get_email_service
        email_svc = get_email_service()
        if email_svc and recordings_email:
            google_message_id = email_svc.send_email(
                to=recordings_email,
                subject=subject,
                text_body=body,
                attachments=[{
                    'filename': f"recording_{recording_sid}.mp3",
                    'content_type': 'audio/mpeg',
                    'content_base64': base64.b64encode(audio_content).decode('utf-8'),
                }],
                metadata={
                    'caller': 'tina',
                    'recording_sid': recording_sid,
                    'call_sid': call_sid,
                },
            )

        return google_message_id, recordings_email

    def _log_recording(self, recording_sid, call_sid, recording_url,
                       from_number, to_number, duration, call_type,
                       staff_email, staff_name, caller_name,
                       local_file_path, google_message_id, recordings_email,
                       drive_file_id):
        """Log recording to database and update Drive file ID. Returns recording_id."""
        now = datetime.now(timezone.utc).isoformat()
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

        if drive_file_id:
            self.db.update_recording_drive_file(recording_sid, drive_file_id)

        return recording_id

    def _delete_from_twilio(self, recording_sid, google_message_id, drive_file_id):
        """Delete recording from Twilio if we have at least one backup. Returns bool."""
        if not (google_message_id or drive_file_id):
            return False

        delete_result = get_twilio_service().delete_recording(recording_sid)
        if delete_result.get('success'):
            self.db.mark_recording_deleted(recording_sid)
            logger.info(f"Recording {recording_sid} deleted from Twilio")
            return True

        logger.warning(f"Failed to delete recording from Twilio: {delete_result.get('error')}")
        return False

    def _status_callback_url(self, conference_name: str = None,
                             call_type: str = None) -> str:
        """Build the recording-status callback URL.

        When we know what the call is, we say so on the URL rather than
        leaving the webhook to work it out. Its fallback path re-derives the
        answer from the activity log and the conference-name prefix, and that
        prefix is ambiguous — a direct inbound call sits in a conference named
        `call_<sid>`, which the prefix rule reads as outbound. Passing the
        facts we already hold keeps the filed recording honest.
        """
        url = f"{config.webhook_base_url}/api/voice/recording-status"
        params = []
        if conference_name:
            params.append(f"conf={quote(conference_name, safe='')}")
        if call_type:
            params.append(f"ctype={quote(call_type, safe='')}")
        return f"{url}?{'&'.join(params)}" if params else url

    def start_conversation_recording(self, conference_name: str,
                                     joining_call_sid: str, role: str,
                                     call_type: str, db=None) -> dict:
        """Start recording a conversation as it connects, server-side.

        This is the device-independent path: it runs on a Twilio webhook, so
        it works the same whether the staff member answered in the browser, on
        a desk phone or in a SIP softphone app. Called at the moment of
        connection from conference_join and outbound_customer_join.

        Does nothing unless the tenant has switched tenant-wide recording on.
        Never raises — a recording problem must not take a live call down.
        """
        try:
            # Inside the try: this resolves the tenant database, and on a
            # webhook with no resolvable tenant it raises. Losing a recording
            # is survivable; dropping the caller's TwiML is not.
            db = db or self.db
            if not record_all_enabled(db):
                return {'success': False, 'skipped': 'disabled'}

            target_sid = customer_leg_for_conference(
                conference_name, joining_call_sid, role)
            if not target_sid:
                # A mid-call move (hold, transfer): the customer leg is
                # already being recorded and survives the move.
                return {'success': False, 'skipped': 'not_a_conversation_start'}

            # Prefer the leg's own logged direction over the caller's hint.
            # The hint is right at the two points a conversation starts, but
            # conference_join is also reached mid-call (a customer coming back
            # off hold), where it would file an outbound call as inbound.
            call_type = self._logged_direction(db, target_sid) or call_type

            # Atomic claim — only one worker/webhook may start this recording.
            if not db.claim_recording_start(target_sid, conference_name, call_type):
                return {'success': False, 'skipped': 'already_started'}

            try:
                recording = self.client_recordings_create(
                    target_sid, conference_name, call_type)
            except Exception:
                # Let the next participant retry rather than leaving the
                # conversation permanently unrecordable behind a dead claim.
                db.release_recording_start(target_sid)
                raise

            logger.info(
                f"Tenant-wide recording started for {call_type} call: "
                f"leg={target_sid} conference={conference_name} "
                f"recording={recording.sid}"
            )
            return {'success': True, 'recording_sid': recording.sid,
                    'call_sid': target_sid}

        except Exception as e:
            logger.error(
                f"Could not start tenant-wide recording for conference "
                f"{conference_name} (leg {joining_call_sid}, role {role}): {e}"
            )
            return {'success': False, 'error': str(e)}

    @staticmethod
    def _logged_direction(db, call_sid: str) -> str | None:
        """The direction we logged for this leg, if we recognise it.

        Only 'inbound' and 'outbound' are returned — 'internal' and anything
        unexpected fall through to the caller's hint rather than being written
        into a recording's call_type, which the recordings page filters on.
        """
        try:
            direction = db.get_call_log_field(call_sid, 'direction')
        except Exception as e:
            logger.debug(f"No logged direction for {call_sid}: {e}")
            return None
        return direction if direction in ('inbound', 'outbound') else None

    def client_recordings_create(self, call_sid: str, conference_name: str = None,
                                 call_type: str = None):
        """Create a Twilio recording on a call leg. Split out so the start
        path above stays readable and can be exercised on its own."""
        client = get_twilio_service().client
        return client.calls(call_sid).recordings.create(
            recording_status_callback=self._status_callback_url(
                conference_name, call_type),
            recording_status_callback_event=['completed', 'absent'],
        )

    def resolve_recorded_leg(self, call_sid: str, db=None) -> str:
        """Map any leg of a conversation to the leg actually being recorded.

        The Record/Stop button sends whichever SID the caller's own device
        knows about — for a browser agent that is their own leg, while the
        server records the customer's. Without this, pressing Stop would look
        for a recording on a leg that has none and silently do nothing.

        Falls back to the SID it was given, so the pre-existing browser-only
        behaviour is unchanged when tenant-wide recording is off.
        """
        try:
            db = db or self.db
            if db.get_recording_start(call_sid):
                return call_sid
            conference_name = db.get_call_conference(call_sid)
            claimed = db.find_recording_start_in_conference(conference_name)
            if claimed:
                return claimed['call_sid']
        except Exception as e:
            logger.warning(f"Could not resolve recorded leg for {call_sid}: {e}")
        return call_sid

    def start_recording(self, call_sid: str) -> dict:
        """Start or resume recording an active call.

        If a paused recording exists, resumes it (same recording, one file).
        Otherwise creates a new recording.

        Args:
            call_sid: The agent's Twilio call SID

        Returns:
            Dict with 'success' and 'recording_sid' or 'error'
        """
        try:
            client = get_twilio_service().client

            # Already recording? Hand back the recording in flight rather than
            # starting a second one. With tenant-wide recording on, the server
            # has usually started one before anyone touches the Record button,
            # and Twilio will happily record the same leg twice — which bills
            # twice, files two rows and plays back as duplicates.
            running = self._find_recording(call_sid, status='in-progress')
            if running:
                logger.info(f"Recording {running['sid']} already running for {call_sid}")
                return {
                    'success': True,
                    'recording_sid': running['sid'],
                    'already_running': True,
                }

            # Check for a paused recording to resume first
            paused = self._find_recording(call_sid, status='paused')
            if paused:
                paused['resource'].update(status='in-progress')
                logger.info(f"Resumed recording {paused['sid']} for {call_sid}")
                return {
                    'success': True,
                    'recording_sid': paused['sid'],
                    'resumed': True,
                }

            # No paused recording — create a new one
            status_callback = self._status_callback_url()
            recording = client.calls(call_sid).recordings.create(
                recording_status_callback=status_callback,
                recording_status_callback_event=['completed', 'absent'],
            )
            logger.info(f"Started call recording for {call_sid}: {recording.sid}")
            return {
                'success': True,
                'recording_sid': recording.sid,
            }
        except TwilioException as e:
            logger.error(f"Failed to start recording for {call_sid}: {e}")
            return {'success': False, 'error': str(e)}

    def stop_recording(self, call_sid: str) -> dict:
        """Pause recording on an active call.

        Uses pause (not stop) so the recording can be resumed later
        as a single continuous file.

        Args:
            call_sid: The agent's Twilio call SID

        Returns:
            Dict with 'success' and 'paused_count' or 'error'
        """
        try:
            client = get_twilio_service().client
            paused = 0

            # Try conference recordings first
            conference_name = self.db.get_call_conference(call_sid)
            if conference_name:
                confs = twilio_list(client.conferences,
                    friendly_name=conference_name, status='in-progress', limit=1
                )
                if confs:
                    for r in twilio_list(client.conferences(confs[0].sid).recordings):
                        if r.status == 'in-progress':
                            client.conferences(confs[0].sid).recordings(r.sid).update(status='paused')
                            paused += 1
                            logger.info(f"Paused conference recording {r.sid}")

            # Also check call-level recordings
            for r in twilio_list(client.calls(call_sid).recordings):
                if r.status == 'in-progress':
                    r.update(status='paused')
                    paused += 1
                    logger.info(f"Paused call recording {r.sid}")

            return {
                'success': True,
                'stopped_count': paused,  # keep key name for API compat
            }
        except TwilioException as e:
            logger.error(f"Failed to pause recording for {call_sid}: {e}")
            return {'success': False, 'error': str(e)}

    def _find_recording(self, call_sid: str, status: str = 'in-progress') -> dict | None:
        """Find a recording with the given status for a call.

        Checks conference recordings first, then call-level.
        Returns dict with 'sid' and 'resource' (for update calls), or None.
        """
        try:
            client = get_twilio_service().client

            # Conference recordings
            conference_name = self.db.get_call_conference(call_sid)
            if conference_name:
                confs = twilio_list(client.conferences,
                    friendly_name=conference_name, status='in-progress', limit=1
                )
                if confs:
                    for r in twilio_list(client.conferences(confs[0].sid).recordings):
                        if r.status == status:
                            return {
                                'sid': r.sid,
                                'resource': client.conferences(confs[0].sid).recordings(r.sid),
                            }

            # Call-level recordings
            for r in twilio_list(client.calls(call_sid).recordings):
                if r.status == status:
                    return {'sid': r.sid, 'resource': r}

        except TwilioException as e:
            logger.warning(f"Error finding {status} recording for {call_sid}: {e}")
        return None

    def get_recording_status(self, call_sid: str) -> dict:
        """Get recording status for a call.

        Checks both conference recordings and call recordings.

        Args:
            call_sid: The agent's Twilio call SID

        Returns:
            Dict with 'recording' (bool), 'paused' (bool), 'recording_sid'
        """
        active = self._find_recording(call_sid, status='in-progress')
        if active:
            return {'recording': True, 'paused': False, 'recording_sid': active['sid']}

        paused = self._find_recording(call_sid, status='paused')
        if paused:
            return {'recording': True, 'paused': True, 'recording_sid': paused['sid']}

        return {'recording': False, 'paused': False}

    def get_user_recording_preference(self, email: str) -> bool:
        """Get whether a user has recording enabled by default."""
        return self.db.get_user_recording_default(email)

    def set_user_recording_preference(self, email: str, enabled: bool,
                                       updated_by: str) -> None:
        """Set whether a user has recording enabled by default."""
        self.db.set_user_recording_default(email, enabled, updated_by)

    def get_storage_overview(self) -> dict:
        """Disk and local-cache figures for the admin Storage page.

        The recordings directory is shared across tenants (recording SIDs are
        globally unique), so the file count and bytes here are system-wide,
        not per-tenant.

        Returns:
            Dict with disk_total/disk_used/disk_free/disk_percent and
            cache_files/cache_bytes. Disk figures are None if unavailable.
        """
        cache_files = 0
        cache_bytes = 0
        try:
            with os.scandir(self.RECORDINGS_DIR) as entries:
                for entry in entries:
                    if entry.is_file():
                        cache_files += 1
                        cache_bytes += entry.stat().st_size
        except OSError as e:
            logger.error(f"Could not scan recordings directory: {e}")

        disk = {'disk_total': None, 'disk_used': None, 'disk_free': None, 'disk_percent': None}
        try:
            usage = shutil.disk_usage(self.RECORDINGS_DIR)
            disk = {
                'disk_total': usage.total,
                'disk_used': usage.used,
                'disk_free': usage.free,
                'disk_percent': round(usage.used / usage.total * 100) if usage.total else None,
            }
        except OSError as e:
            logger.error(f"Could not read disk usage: {e}")

        return {'cache_files': cache_files, 'cache_bytes': cache_bytes, **disk}

    def purge_stale_recordings(self, days: int = 30, dry_run: bool = False) -> dict:
        """Purge local recording files that haven't been accessed recently.

        Deletes local cached files for recordings not accessed in the given
        number of days. The Google Group archive remains the permanent store.

        Only recordings that have a Google Drive copy are purged — playback
        falls back to Drive and re-caches on a local miss, so those files are
        genuinely cache. A recording with no drive_file_id has the local file
        as its only playable copy (the Google Groups archive is cold storage
        and can't be streamed), so deleting it would lose the audio.

        Args:
            days: Number of days since last access before purging (default 30)
            dry_run: Report what would be purged without deleting anything

        Returns:
            Dict with 'success', 'purged_count', 'purged_bytes',
            'skipped_no_archive', 'dry_run', and 'errors'
        """
        stale_recordings = self.db.get_stale_recordings(days=days)
        logger.info(f"Found {len(stale_recordings)} recordings to purge (not accessed in {days} days)"
                    + (" [dry run]" if dry_run else ""))

        purged_count = 0
        purged_bytes = 0
        skipped_no_archive = 0
        errors = []

        for rec in stale_recordings:
            recording_sid = rec['recording_sid']
            local_path = rec.get('local_file_path')

            if not local_path:
                continue

            if not rec.get('drive_file_id'):
                skipped_no_archive += 1
                continue

            try:
                # Build full path and delete file
                full_path = os.path.join(self.RECORDINGS_DIR, local_path)
                if os.path.exists(full_path):
                    purged_bytes += os.path.getsize(full_path)
                    if not dry_run:
                        os.remove(full_path)
                        logger.info(f"Deleted local file for recording {recording_sid}")

                # Clear the local_file_path in database
                if not dry_run:
                    self.db.clear_recording_local_file(recording_sid)
                purged_count += 1

            except OSError as e:
                error_msg = f"Failed to purge recording {recording_sid}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        if skipped_no_archive:
            logger.warning(
                f"Skipped {skipped_no_archive} stale recordings with no Drive copy — "
                f"the local file is the only playable copy"
            )
        verb = "Would purge" if dry_run else "Purged"
        logger.info(f"{verb} {purged_count} recordings "
                    f"({purged_bytes / 1_000_000:.0f} MB), {len(errors)} errors")

        return {
            'success': len(errors) == 0,
            'purged_count': purged_count,
            'purged_bytes': purged_bytes,
            'skipped_no_archive': skipped_no_archive,
            'dry_run': dry_run,
            'errors': errors if errors else None,
        }


# Singleton instance
recording_service = RecordingService()
