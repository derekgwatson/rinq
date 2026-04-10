"""Stats, reporting, cleanup, and number provisioning routes.

Extracted from routes.py. Registered via register(api_bp) at import time.
"""

import logging
from datetime import datetime, timezone

from flask import g, jsonify, request

from rinq.config import config
from rinq.database.db import get_db
from rinq.services.twilio_service import get_twilio_service, twilio_list
from rinq.services.auth import login_required, get_current_user

try:
    from shared.auth.bot_api import api_or_session_auth, get_api_caller, get_api_caller_email
except ImportError:
    from rinq.auth.decorators import api_or_session_auth, get_api_caller, get_api_caller_email

logger = logging.getLogger(__name__)


def register(bp):
    """Register all stats/reporting/provisioning routes on the given blueprint."""

    # =============================================================================
    # Call Statistics & Reporting
    # =============================================================================

    @bp.route('/stats/aggregate', methods=['POST'])
    @api_or_session_auth
    def aggregate_stats():
        """Aggregate call statistics for a given date.

        Should be called nightly by Skye BEFORE cleanup_old_queued_calls
        to preserve queue statistics that would otherwise be lost.

        Request body (optional):
            {"date": "YYYY-MM-DD"}  - defaults to yesterday

        Returns:
            {"success": true, "date": "YYYY-MM-DD", "daily_records": N, "hourly_records": N}
        """
        from rinq.services.reporting_service import get_reporting_service

        data = request.get_json() or {}
        target_date = data.get('date')  # None = yesterday

        service = get_reporting_service()
        result = service.aggregate_stats_for_date(target_date)

        caller = get_api_caller()
        db = get_db()
        db.log_activity(
            'stats_aggregated',
            result['date'],
            f"Aggregated {result['daily_records']} daily, {result['hourly_records']} hourly records",
            caller
        )

        return jsonify({
            'success': True,
            'date': result['date'],
            'daily_records': result['daily_records'],
            'hourly_records': result['hourly_records'],
        })


    @bp.route('/stats/summary')
    @api_or_session_auth
    def get_stats_summary():
        """Get call statistics summary for a time period.

        Query params:
            period: 'today', 'yesterday', 'this_week', 'last_week', 'this_month',
                    'last_month', or 'YYYY-MM-DD:YYYY-MM-DD' for custom range

        Returns:
            Complete report data including summary, agent stats, queue stats
        """
        from rinq.services.reporting_service import get_reporting_service

        period = request.args.get('period', 'today')

        service = get_reporting_service()
        report_data = service.get_report_data(period)

        return jsonify(report_data)


    @bp.route('/queue/cleanup', methods=['POST'])
    @api_or_session_auth
    def cleanup_queue():
        """Clean up old queued_calls records.

        Should be called by Skye AFTER stats/aggregate to preserve data.

        Request body (optional):
            {"hours": 24}  - keep records newer than this (default 24)

        Returns:
            {"success": true, "deleted_count": N}
        """
        data = request.get_json() or {}
        hours = data.get('hours', 24)

        if not isinstance(hours, int) or hours < 1:
            return jsonify({"error": "hours must be a positive integer"}), 400

        db = get_db()
        deleted_count = db.cleanup_old_queued_calls(hours=hours)

        # Clean up stale ring attempts (safety net for missed callbacks)
        stale_ring_count = db.cleanup_old_ring_attempts(max_age_minutes=10)

        # Clean up old participant records
        stale_participants = db.cleanup_old_participants(hours=hours)

        caller = get_api_caller()
        db.log_activity(
            'queue_cleanup',
            f'{hours}h',
            f"Deleted {deleted_count} old queued_calls, {stale_ring_count} stale ring_attempts, {stale_participants} old participants",
            caller
        )

        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
        })


    @bp.route('/voicemail/cleanup', methods=['POST'])
    @api_or_session_auth
    def cleanup_voicemail_recordings():
        """Clean up voicemail recordings stuck in Twilio.

        Finds voicemail recordings where transcription callback never arrived
        (older than 1 hour, ticket created, but not deleted from Twilio).
        Deletes them from Twilio to avoid storage costs.

        Should be called by Skye nightly as a safety net.

        Returns:
            {"success": true, "deleted_count": N, "errors": [...]}
        """
        db = get_db()
        twilio_service = get_twilio_service()

        # Find voicemails older than 1 hour that haven't been deleted
        stale = db.get_undeleted_voicemails(hours=1)

        deleted_count = 0
        errors = []

        for recording in stale:
            recording_sid = recording.get('recording_sid')
            try:
                twilio_service.delete_recording(recording_sid)
                db.mark_recording_deleted(recording_sid)
                deleted_count += 1
                logger.info(f"Cleanup: deleted stale recording {recording_sid}")
            except Exception as e:
                error_msg = f"{recording_sid}: {str(e)}"
                errors.append(error_msg)
                logger.warning(f"Cleanup: failed to delete {recording_sid}: {e}")

        if deleted_count > 0 or errors:
            caller = get_api_caller()
            db.log_activity(
                'voicemail_cleanup',
                f'{len(stale)} found',
                f"Deleted {deleted_count}, errors: {len(errors)}",
                caller
            )

        return jsonify({
            'success': True,
            'found': len(stale),
            'deleted_count': deleted_count,
            'errors': errors[:10] if errors else [],  # Limit error list
        })


    # =============================================================================
    # Phone Number Provisioning (Buy/Search)
    # =============================================================================

    @bp.route('/numbers/search', methods=['GET'])
    @login_required
    def search_available_numbers():
        """Search for available phone numbers to purchase.

        GET /api/numbers/search?country=AU&area_code=02&limit=10

        Returns:
            {"numbers": [{"phone_number": "+61...", "locality": "...", "region": "..."}]}
        """
        user = get_current_user()
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403

        country = request.args.get('country', 'AU')
        locality = request.args.get('locality', '')
        region = request.args.get('region', '')
        contains = request.args.get('contains', '')
        limit = min(int(request.args.get('limit', '20')), 50)

        service = get_twilio_service()
        if not service.is_configured:
            return jsonify({'error': 'Twilio not configured'}), 500

        try:
            kwargs = {'limit': limit}
            if locality:
                kwargs['in_locality'] = locality
            if region:
                kwargs['in_region'] = region
            if contains:
                kwargs['contains'] = contains

            numbers = twilio_list(service.client.available_phone_numbers(country).local, **kwargs)
            results = []
            for n in numbers:
                results.append({
                    'phone_number': n.phone_number,
                    'friendly_name': n.friendly_name,
                    'locality': n.locality or '',
                    'region': n.region or '',
                })

            return jsonify({'numbers': results})

        except Exception as e:
            logger.error(f"Number search failed: {e}")
            return jsonify({'error': str(e)}), 500


    @bp.route('/numbers/buy', methods=['POST'])
    @login_required
    def buy_number():
        """Purchase a phone number and register it to the current tenant.

        POST /api/numbers/buy
        {"phone_number": "+61..."}

        Returns:
            {"success": true, "phone_number": "+61...", "sid": "PN..."}
        """
        user = get_current_user()
        if not user or not user.is_admin:
            return jsonify({'error': 'Admin access required'}), 403

        phone_number = request.json.get('phone_number', '').strip()
        if not phone_number:
            return jsonify({'error': 'phone_number is required'}), 400

        service = get_twilio_service()
        if not service.is_configured:
            return jsonify({'error': 'Twilio not configured'}), 500

        try:
            # Get address SID from tenant
            from flask import g as flask_g
            tenant = getattr(flask_g, 'tenant', None)
            address_sid = tenant.get('twilio_address_sid') if tenant else None

            if not address_sid:
                return jsonify({'error': 'Business address required. Please set up your address first.'}), 400

            # Purchase the number
            incoming = service.client.incoming_phone_numbers.create(
                phone_number=phone_number,
                address_sid=address_sid,
                voice_url=f"{config.webhook_base_url}/api/voice/incoming",
                voice_method='POST',
                status_callback=f"{config.webhook_base_url}/api/voice/status",
                status_callback_method='POST',
            )

            # Save to local database
            db = get_db()
            db.upsert_phone_number({
                'sid': incoming.sid,
                'phone_number': incoming.phone_number,
                'friendly_name': incoming.friendly_name or incoming.phone_number,
                'forward_to': None,
                'is_active': 1,
                'synced_at': datetime.now(timezone.utc).isoformat(),
            })

            # Register to tenant in master DB
            try:
                from flask import g
                tenant = getattr(g, 'tenant', None)
                if tenant:
                    from rinq.database.master import get_master_db
                    master_db = get_master_db()
                    master_db.register_phone_number(incoming.phone_number, tenant['id'])
            except Exception as e:
                logger.warning(f"Failed to register number to tenant: {e}")

            db.log_activity(
                action="number_purchased",
                target=incoming.phone_number,
                details=f"SID: {incoming.sid}",
                performed_by=f"session:{user.email}"
            )

            logger.info(f"Purchased number {incoming.phone_number} (SID: {incoming.sid})")

            return jsonify({
                'success': True,
                'phone_number': incoming.phone_number,
                'sid': incoming.sid,
                'friendly_name': incoming.friendly_name,
            })

        except Exception as e:
            logger.error(f"Number purchase failed: {e}")
            return jsonify({'error': str(e)}), 500
