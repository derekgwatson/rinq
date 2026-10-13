"""Weekly Tina-vs-Twilio cross-check. See services/cross_check.py.

Registered via register(api_bp) at import time. Called by the derek-user cron
on do-personal (Monday morning) over the unix socket, which has no session and
so no tenant: like the other cron endpoints it iterates every tenant.
"""

import logging
from datetime import date

from flask import jsonify, request

from rinq.database.db import get_db

try:
    from shared.auth.bot_api import api_or_session_auth
except ImportError:
    from rinq.auth.decorators import api_or_session_auth

logger = logging.getLogger(__name__)


def register(bp):

    @bp.route('/stats/cross-check', methods=['POST'])
    @api_or_session_auth
    def cross_check():
        """Compare Tina's call records with Twilio's for a week and email the admins.

        Request body (optional):
            {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "email": false}
            Defaults: last full Monday-Sunday week, email on.
        """
        from flask import session
        from rinq.services import cross_check as cc
        from rinq.services.twilio_service import get_twilio_service
        from rinq.tenant.context import iter_tenant_contexts

        # silent=True: the cron POSTs an empty body with no Content-Type
        data = request.get_json(silent=True) or {}
        start, end = cc.last_week()
        try:
            if data.get('start'):
                start = date.fromisoformat(data['start'])
            if data.get('end'):
                end = date.fromisoformat(data['end'])
        except ValueError:
            return jsonify({'error': 'start/end must be YYYY-MM-DD'}), 400
        send = data.get('email', True)

        def _one(tenant):
            from flask import g
            db = get_db()
            result = cc.run(db, get_twilio_service().client, start, end)
            cc.save(db, result)
            # A quiet tenant (e.g. a test one) gets an email only when something's wrong
            busy_enough = (result['status'] != 'ok'
                           or result.get('incoming_calls', 0) >= cc.MIN_WEEKLY_CALLS_TO_EMAIL)
            if send and busy_enough:
                product = (tenant or getattr(g, 'tenant', None) or {}).get('product_name') or 'Tina'
                result['emailed'] = cc.email_result(tenant or g.tenant, result, product)
            return result

        if session.get('user_id'):
            from flask import g
            return jsonify(_one(getattr(g, 'tenant', None)))

        results, errors = {}, []
        for tenant in iter_tenant_contexts():
            try:
                results[tenant['id']] = _one(tenant)
            except Exception as e:
                logger.error(f"Cross-check failed for tenant {tenant['id']}: {e}", exc_info=True)
                errors.append(f"{tenant['id']}: {e}")
        return jsonify({'results': results, 'errors': errors or None}), 200 if not errors else 207
