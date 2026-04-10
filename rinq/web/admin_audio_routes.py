"""Admin audio and TTS routes — file management, upload, TTS generation.

Extracted from web/routes.py. Registered via register(web_bp) at import time.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from flask import request, redirect, url_for, flash, jsonify, render_template, Response

from rinq.services.auth import login_required, admin_required, get_current_user
from rinq.database.db import get_db
from rinq.config import config
from rinq.tenant.context import get_twilio_config

logger = logging.getLogger(__name__)

ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'wav', 'ogg', 'm4a'}
AUDIO_FOLDER = config.base_dir / 'audio'


def _audit_tag(user):
    return f"session:{user.email}"


def allowed_audio_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_AUDIO_EXTENSIONS


def register(bp):
    """Register audio/TTS routes on the given blueprint."""

    @bp.route('/admin/audio/upload', methods=['POST'])
    @admin_required
    def upload_audio():
        """Upload an audio file for greetings, hold music, etc."""
        from werkzeug.utils import secure_filename
        import os

        name = request.form.get('name', '').strip()
        file_type = request.form.get('file_type', 'greeting')
        description = request.form.get('description', '').strip()
        tts_text = request.form.get('tts_text', '').strip()

        if not name:
            flash("Audio file name is required", "error")
            return redirect(url_for('web.admin_audio'))

        if 'audio_file' not in request.files:
            flash("No audio file uploaded", "error")
            return redirect(url_for('web.admin_audio'))

        file = request.files['audio_file']
        if file.filename == '':
            flash("No audio file selected", "error")
            return redirect(url_for('web.admin_audio'))

        if not allowed_audio_file(file.filename):
            flash(f"Invalid file type. Allowed: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}", "error")
            return redirect(url_for('web.admin_audio'))

        user = get_current_user()
        db = get_db()

        # Ensure audio folder exists
        AUDIO_FOLDER.mkdir(exist_ok=True)

        # Generate unique filename
        ext = file.filename.rsplit('.', 1)[1].lower()
        safe_name = secure_filename(name.replace(' ', '_').lower())
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{safe_name}_{timestamp}.{ext}"
        file_path = AUDIO_FOLDER / filename

        try:
            # Save the file
            file.save(str(file_path))

            # Try to detect audio duration
            duration_seconds = None
            try:
                import subprocess
                result = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', str(file_path)],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    duration_seconds = round(float(result.stdout.strip()))
            except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
                pass  # ffprobe not available or failed — duration stays None

            # Store just the path - full URL is constructed at runtime
            file_url = f"/audio/{filename}"

            # Create database record
            audio_id = db.create_audio_file(
                data={
                    'name': name,
                    'description': description or None,
                    'file_type': file_type,
                    'file_url': file_url,
                    'file_path': str(file_path),
                    'tts_text': tts_text or None,
                    'duration_seconds': duration_seconds,
                },
                created_by=_audit_tag(user)
            )

            db.log_activity(
                action="upload_audio",
                target=name,
                details=f"Uploaded {file_type} audio: {filename}",
                performed_by=_audit_tag(user)
            )
            flash(f"Uploaded audio file '{name}'", "success")
        except Exception as e:
            flash(f"Failed to upload audio: {e}", "error")

        return redirect(url_for('web.admin_audio'))

    @bp.route('/audio/<filename>')
    def serve_audio(filename):
        """Serve audio files to Twilio (no auth required for Twilio access)."""
        from flask import send_from_directory
        return send_from_directory(str(AUDIO_FOLDER), filename)

    @bp.route('/admin/audio/<int:audio_id>/edit', methods=['POST'])
    @admin_required
    def update_audio(audio_id):
        """Update an audio file's metadata (name, type, description, spoken text)."""
        name = request.form.get('name', '').strip()
        file_type = request.form.get('file_type', 'greeting')
        description = request.form.get('description', '').strip()
        tts_text = request.form.get('tts_text', '').strip()

        if not name:
            flash("Audio file name is required", "error")
            return redirect(url_for('web.admin_audio'))

        user = get_current_user()
        db = get_db()

        audio = db.get_audio_file(audio_id)
        if not audio:
            flash("Audio file not found", "error")
            return redirect(url_for('web.admin_audio'))

        try:
            db.update_audio_file(
                audio_id=audio_id,
                data={
                    'name': name,
                    'description': description or None,
                    'file_type': file_type,
                    'tts_text': tts_text or None,
                },
                updated_by=_audit_tag(user)
            )

            db.log_activity(
                action="update_audio",
                target=name,
                details=f"Updated audio file ID {audio_id} (type={file_type})",
                performed_by=_audit_tag(user)
            )
            flash(f"Updated audio file '{name}'", "success")
        except Exception as e:
            flash(f"Failed to update audio: {e}", "error")

        return redirect(url_for('web.admin_audio'))

    @bp.route('/admin/audio/<int:audio_id>/delete', methods=['POST'])
    @admin_required
    def delete_audio(audio_id):
        """Delete an audio file."""
        import os

        user = get_current_user()
        db = get_db()

        audio = db.get_audio_file(audio_id)
        if not audio:
            flash("Audio file not found", "error")
            return redirect(url_for('web.admin_audio'))

        try:
            # Delete file from disk if it exists
            if audio.get('file_path') and os.path.exists(audio['file_path']):
                os.remove(audio['file_path'])

            # Soft delete in database (set is_active = 0)
            db.deactivate_audio_file(audio_id)

            db.log_activity(
                action="delete_audio",
                target=audio['name'],
                details=f"Deleted audio file ID {audio_id}",
                performed_by=_audit_tag(user)
            )
            flash(f"Deleted audio file '{audio['name']}'", "success")
        except Exception as e:
            flash(f"Failed to delete audio: {e}", "error")

        return redirect(url_for('web.admin_audio'))

    # =========================================================================
    # Admin Settings
    # =========================================================================

    @bp.route('/admin/settings')
    @admin_required
    def admin_settings():
        """Admin settings page - configure bot-wide settings."""
        user = get_current_user()
        db = get_db()

        settings = db.get_bot_settings()
        audio_files = db.get_audio_files()

        return render_template('admin_settings.html',
                               settings=settings,
                               audio_files=audio_files,
                               current_user=user)

    @bp.route('/admin/settings', methods=['POST'])
    @admin_required
    def save_admin_settings():
        """Save admin settings."""
        user = get_current_user()
        db = get_db()

        # Save Drive folder ID
        drive_folder_id = request.form.get('drive_recordings_folder_id', '').strip()

        if drive_folder_id:
            db.set_bot_setting('drive_recordings_folder_id', drive_folder_id, f'session:{user.email}')
            flash('Settings saved. Drive folder configured.', 'success')
        else:
            db.set_bot_setting('drive_recordings_folder_id', '', f'session:{user.email}')
            flash('Settings saved. Drive folder cleared.', 'warning')

        # Clear the cached folder ID in drive_service so it picks up the new value
        from rinq.services.drive_service import drive_service
        drive_service._recordings_folder_id = None

        # Save extension directory number
        ext_dir_number = request.form.get('extension_directory_number', '').strip()
        db.set_bot_setting('extension_directory_number', ext_dir_number, f'session:{user.email}')

        # Save connecting prefix audio path
        connecting_prefix = request.form.get('connecting_prefix_audio_path', '').strip()
        db.set_bot_setting('connecting_prefix_audio_path', connecting_prefix, f'session:{user.email}')

        return redirect(url_for('web.admin_settings'))

    # =========================================================================
    # Text-to-Speech (TTS) Generation
    # =========================================================================

    @bp.route('/admin/tts')
    @admin_required
    def admin_tts():
        """Redirect to unified audio page."""
        return redirect(url_for('web.admin_audio'))

    @bp.route('/admin/tts/settings', methods=['POST'])
    @admin_required
    def save_tts_settings():
        """Save default TTS voice settings."""
        provider = request.form.get('provider', 'elevenlabs')
        voice = request.form.get('voice', '')

        if not voice:
            flash("Voice is required", "error")
            return redirect(url_for('web.admin_audio'))

        user = get_current_user()
        db = get_db()

        try:
            db.set_tts_setting('default_provider', provider, _audit_tag(user))
            db.set_tts_setting('default_voice', voice, _audit_tag(user))

            db.log_activity(
                action="update_tts_settings",
                target="default_voice",
                details=f"Set default TTS to {provider} voice {voice}",
                performed_by=_audit_tag(user)
            )

            flash("Voice settings saved", "success")
        except Exception as e:
            logger.exception(f"Failed to save TTS settings: {e}")
            flash(f"Failed to save settings: {e}", "error")

        return redirect(url_for('web.admin_audio'))

    @bp.route('/admin/tts/preview', methods=['POST'])
    @admin_required
    def preview_tts():
        """Generate TTS audio for preview (returns audio blob)."""
        from rinq.services.tts_service import get_tts_service

        provider = request.form.get('provider', 'elevenlabs')
        text = request.form.get('text', '').strip()
        voice = request.form.get('voice', '')

        if not text:
            return jsonify({'error': 'Text is required'}), 400

        if not voice:
            return jsonify({'error': 'Voice is required'}), 400

        tts = get_tts_service()

        try:
            if provider == 'elevenlabs':
                if not tts.elevenlabs_available:
                    return jsonify({'error': 'ElevenLabs API key not configured'}), 400
                stability = float(request.form.get('stability', 0.5))
                audio_bytes = tts.generate_elevenlabs(text, voice_id=voice, stability=stability)

            elif provider == 'cartesia':
                if not tts.cartesia_available:
                    return jsonify({'error': 'Cartesia API key not configured'}), 400
                speed = float(request.form.get('speed', 1.0))
                audio_bytes = tts.generate_cartesia(text, voice_id=voice, speed=speed)

            elif provider == 'google':
                if not tts.google_available:
                    return jsonify({'error': 'Google TTS API key not configured'}), 400
                speed = float(request.form.get('speed', 1.0))
                audio_bytes = tts.generate_google(text, voice_name=voice, speaking_rate=speed)

            else:
                return jsonify({'error': f'Unknown provider: {provider}'}), 400

            return Response(audio_bytes, mimetype='audio/mpeg')

        except Exception as e:
            logger.exception(f"TTS preview failed: {e}")
            return jsonify({'error': str(e)}), 500

    @bp.route('/admin/tts/save', methods=['POST'])
    @admin_required
    def save_tts_audio():
        """Save TTS audio file (uses uploaded preview audio, not regenerating)."""
        from werkzeug.utils import secure_filename
        from rinq.services.tts_service import get_tts_service

        provider = request.form.get('provider', 'elevenlabs')
        text = request.form.get('text', '').strip()
        voice = request.form.get('voice', '')
        name = request.form.get('name', '').strip()
        file_type = request.form.get('file_type', 'greeting')
        description = request.form.get('description', '').strip()

        if not text:
            flash("Text is required", "error")
            return redirect(url_for('web.admin_audio'))

        if not name:
            flash("Name is required", "error")
            return redirect(url_for('web.admin_audio'))

        if not voice:
            flash("Voice is required", "error")
            return redirect(url_for('web.admin_audio'))

        # Check for uploaded audio data (from preview)
        audio_file = request.files.get('audio_data')
        if not audio_file:
            flash("No audio data - please preview first", "error")
            return redirect(url_for('web.admin_audio'))

        user = get_current_user()
        db = get_db()
        tts = get_tts_service()

        try:
            # Read the uploaded audio bytes
            audio_bytes = audio_file.read()

            # Get voice name for description
            if provider == 'elevenlabs':
                voices = tts.get_elevenlabs_voices()
                voice_name = voices.get(voice, {}).get('name', voice)
                provider_info = f"ElevenLabs {voice_name}"
            elif provider == 'cartesia':
                voices = tts.get_cartesia_voices()
                voice_name = voices.get(voice, {}).get('name', voice)
                provider_info = f"Cartesia {voice_name}"
            else:
                provider_info = f"Google Cloud {voice}"

            # Ensure audio folder exists
            AUDIO_FOLDER.mkdir(exist_ok=True)

            # Generate unique filename
            safe_name = secure_filename(name.replace(' ', '_').lower())
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{safe_name}_{timestamp}.mp3"
            file_path = AUDIO_FOLDER / filename

            # Save the file
            with open(file_path, 'wb') as f:
                f.write(audio_bytes)

            # Store just the path - full URL is constructed at runtime
            file_url = f"/audio/{filename}"

            # Create database record with TTS metadata
            full_description = description
            if full_description:
                full_description += f" [TTS: {provider_info}]"
            else:
                full_description = f"TTS: {provider_info}"

            # Build TTS settings for storage
            tts_settings = {}
            if provider == 'elevenlabs':
                stability = float(request.form.get('stability', 0.5))
                tts_settings['stability'] = stability
            elif provider in ('cartesia', 'google'):
                speed = float(request.form.get('speed', 1.0))
                tts_settings['speed'] = speed

            audio_id = db.create_audio_file(
                data={
                    'name': name,
                    'description': full_description,
                    'file_type': file_type,
                    'file_url': file_url,
                    'file_path': str(file_path),
                    'tts_text': text,
                    'tts_provider': provider,
                    'tts_voice': voice,
                    'tts_settings': json.dumps(tts_settings),
                },
                created_by=_audit_tag(user)
            )

            db.log_activity(
                action="save_tts_audio",
                target=name,
                details=f"Saved {file_type} audio with {provider_info}",
                performed_by=_audit_tag(user)
            )

            flash(f"Saved audio file '{name}'", "success")
            return redirect(url_for('web.admin_audio'))

        except Exception as e:
            logger.exception(f"TTS save failed: {e}")
            flash(f"Failed to save audio: {e}", "error")
            return redirect(url_for('web.admin_audio'))
