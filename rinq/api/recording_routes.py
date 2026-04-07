"""Recording API routes — start/stop, status, listing, playback, purge.

Extracted from routes.py. Registered via register(api_bp) at import time.
"""

import logging

from flask import jsonify, request, send_file

from rinq.api.identity import normalize_staff_identifier as _normalize_staff_identifier
from rinq.config import config
from rinq.database.db import get_db
from rinq.services.auth import login_required, get_current_user

try:
    from shared.auth.bot_api import api_or_session_auth, get_api_caller
except ImportError:
    from rinq.auth.decorators import api_or_session_auth, get_api_caller

logger = logging.getLogger(__name__)


def register(bp):
    """Register all recording routes on the given blueprint."""

    @bp.route('/voice/recording/start', methods=['POST'])
    @api_or_session_auth
    def start_recording():
        """Start recording an active call."""
        from rinq.services.recording_service import recording_service

        data = request.get_json() or {}
        call_sid = data.get('call_sid')
        if not call_sid:
            return jsonify({"error": "call_sid required"}), 400

        result = recording_service.start_recording(call_sid)
        if result.get('success'):
            db = get_db()
            db.log_activity(
                action="recording_started",
                target=call_sid,
                details=f"Recording SID: {result.get('recording_sid')}",
                performed_by=get_api_caller()
            )
            return jsonify(result)
        return jsonify(result), 400

    @bp.route('/voice/recording/stop', methods=['POST'])
    @api_or_session_auth
    def stop_recording():
        """Stop recording an active call."""
        from rinq.services.recording_service import recording_service

        data = request.get_json() or {}
        call_sid = data.get('call_sid')
        if not call_sid:
            return jsonify({"error": "call_sid required"}), 400

        result = recording_service.stop_recording(call_sid)
        if result.get('success'):
            db = get_db()
            db.log_activity(
                action="recording_stopped",
                target=call_sid,
                details=f"Stopped {result.get('stopped_count')} recordings",
                performed_by=get_api_caller()
            )
            return jsonify(result)
        return jsonify(result), 400

    @bp.route('/voice/recording/status', methods=['GET'])
    @api_or_session_auth
    def get_recording_status():
        """Get recording status for a call."""
        from rinq.services.recording_service import recording_service

        call_sid = request.args.get('call_sid')
        if not call_sid:
            return jsonify({"error": "call_sid required"}), 400

        return jsonify(recording_service.get_recording_status(call_sid))

    @bp.route('/voice/recording-status', methods=['POST'])
    def recording_status_webhook():
        """Handle recording status webhook from Twilio.

        Downloads recording, emails to Google Group, and logs it.
        No auth required — Twilio calls this directly.
        """
        from rinq.services.recording_service import recording_service
        from rinq.services.twilio_service import get_twilio_service

        recording_sid = request.form.get('RecordingSid')
        call_sid = request.form.get('CallSid')
        recording_url = request.form.get('RecordingUrl')
        recording_status = request.form.get('RecordingStatus')
        recording_duration = request.form.get('RecordingDuration', '0')

        logger.info(f"Recording status webhook: {recording_sid} -> {recording_status}")

        if recording_status != 'completed':
            logger.info(f"Recording {recording_sid} status {recording_status} - ignoring")
            return '', 200

        db = get_db()
        existing = db.get_recording_by_sid(recording_sid)
        if existing:
            logger.info(f"Recording {recording_sid} already exists (type={existing.get('call_type')}) - skipping")
            return '', 200

        from_number = ''
        to_number = ''
        try:
            twilio = get_twilio_service()
            call = twilio.client.calls(call_sid).fetch()
            from_number = call.from_formatted or call.from_ or ''
            to_number = call.to_formatted or call.to or ''
        except Exception as e:
            logger.warning(f"Could not fetch call details from Twilio: {e}")

        call_type = 'unknown'
        staff_email = None
        staff_name = None
        caller_name = None

        queued_call = db.get_queued_call_by_sid(call_sid)
        if queued_call:
            call_type = 'inbound'
            answered_by = queued_call.get('answered_by')
            from_number = from_number or queued_call.get('caller_number', '')
            caller_name = queued_call.get('customer_name')
            staff_email, staff_name = _normalize_staff_identifier(answered_by)
        else:
            activities = db.get_activity_log(limit=20)
            for activity in activities:
                if call_sid in (activity.get('details') or ''):
                    if 'outbound_call' in activity.get('action', ''):
                        call_type = 'outbound'
                        to_number = to_number or activity.get('target', '')
                    elif 'incoming_call' in activity.get('action', ''):
                        call_type = 'inbound'
                    performed_by = activity.get('performed_by', '')
                    staff_email, staff_name = _normalize_staff_identifier(performed_by)
                    break

            if not staff_email and from_number and from_number.startswith('client:'):
                staff_email, staff_name = _normalize_staff_identifier(from_number)
                call_type = 'outbound'

        result = recording_service.process_completed_recording(
            recording_sid=recording_sid,
            call_sid=call_sid,
            recording_url=recording_url,
            duration=int(recording_duration),
            from_number=from_number,
            to_number=to_number,
            call_type=call_type,
            staff_email=staff_email,
            staff_name=staff_name,
            caller_name=caller_name,
        )

        if result.get('success'):
            logger.info(f"Recording {recording_sid} processed successfully")
        else:
            logger.error(f"Failed to process recording {recording_sid}: {result.get('error')}")

        return '', 200

    @bp.route('/users/me/recording-default', methods=['GET'])
    @login_required
    def get_my_recording_default():
        """Get current user's call recording default setting."""
        from rinq.services.recording_service import recording_service
        user = get_current_user()
        enabled = recording_service.get_user_recording_preference(user.email)
        return jsonify({"recording_enabled": enabled})

    @bp.route('/users/me/recording-default', methods=['PUT'])
    @login_required
    def set_my_recording_default():
        """Set current user's call recording default setting."""
        from rinq.services.recording_service import recording_service
        user = get_current_user()
        data = request.get_json() or {}
        enabled = data.get('enabled', True)

        recording_service.set_user_recording_preference(
            user.email, enabled, f"session:{user.email}"
        )

        db = get_db()
        db.log_activity(
            action="recording_default_changed",
            target=user.email,
            details=f"Recording default set to {enabled}",
            performed_by=f"session:{user.email}"
        )
        return jsonify({"success": True, "recording_enabled": enabled})

    @bp.route('/recordings', methods=['GET'])
    @api_or_session_auth
    def list_recordings():
        """List call recordings."""
        db = get_db()
        limit = request.args.get('limit', 100, type=int)
        call_type = request.args.get('call_type')
        staff_email = request.args.get('staff_email')

        if staff_email:
            recordings = db.get_recordings_for_staff(staff_email, limit)
        else:
            recordings = db.get_recording_log(limit=limit, call_type=call_type, exclude_voicemail=True)

        return jsonify({"recordings": recordings, "count": len(recordings)})

    @bp.route('/recordings/<recording_sid>/audio', methods=['GET'])
    @api_or_session_auth
    def get_recording_audio(recording_sid: str):
        """Stream recording audio file.

        Checks local cache → Google Drive → returns 404.
        """
        from rinq.services.recording_service import recording_service

        db = get_db()
        recording = db.get_recording_by_sid(recording_sid)
        if not recording:
            return jsonify({"error": "Recording not found"}), 404

        # 1. Local cache
        file_path = recording_service.get_recording_file_path(recording_sid)
        if file_path:
            db.update_recording_last_accessed(recording_sid)
            return send_file(file_path, mimetype='audio/mpeg', as_attachment=False,
                             download_name=f'{recording_sid}.mp3')

        # 2. Google Drive
        drive_file_id = recording.get('drive_file_id')
        if drive_file_id:
            logger.info(f"Cache miss for recording {recording_sid}, fetching from Google Drive")
            try:
                audio_content = recording_service.fetch_from_drive(drive_file_id)
                if audio_content:
                    local_file_path = recording_service._save_recording_locally(recording_sid, audio_content)
                    db.update_recording_local_file(recording_sid, local_file_path)
                    logger.info(f"Fetched and cached recording {recording_sid} from Drive ({len(audio_content)} bytes)")
                    return send_file(recording_service.get_recording_file_path(recording_sid),
                                     mimetype='audio/mpeg', as_attachment=False,
                                     download_name=f'{recording_sid}.mp3')
                else:
                    logger.warning(f"Failed to fetch recording {recording_sid} from Drive")
            except Exception as e:
                logger.error(f"Error fetching recording from Drive: {e}")

        # 3. Google Groups (cold archive — not accessible)
        google_message_id = recording.get('google_message_id')
        if google_message_id:
            return jsonify({
                "error": "Recording not in local cache or Drive. "
                         "It may be archived in Google Groups but is not accessible for playback."
            }), 404

        return jsonify({"error": "Recording not available"}), 404

    @bp.route('/recordings/purge', methods=['POST'])
    @api_or_session_auth
    def purge_stale_recordings():
        """Purge local recording cache files not accessed recently."""
        from rinq.services.recording_service import recording_service

        data = request.get_json() or {}
        days = data.get('days', 21)
        if not isinstance(days, int) or days < 1:
            return jsonify({"error": "days must be a positive integer"}), 400

        result = recording_service.purge_stale_recordings(days=days)
        status = 200 if result['success'] else 207
        return jsonify(result), status

    @bp.route('/recordings/purge-drive', methods=['POST'])
    @api_or_session_auth
    def purge_drive_recordings():
        """Purge old recordings from Google Drive."""
        from rinq.services.drive_service import drive_service

        data = request.get_json() or {}
        days = data.get('days', 365)
        if not isinstance(days, int) or days < 1:
            return jsonify({"error": "days must be a positive integer"}), 400

        result = drive_service.purge_old_recordings(days=days)
        if 'error' in result:
            return jsonify({"error": result['error']}), 500

        status = 200 if result['success'] else 207
        return jsonify(result), status
