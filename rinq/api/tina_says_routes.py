"""Tina Says — the phone page's daily one-liner. See services/tina_says.py.

Registered via register(api_bp) at import time.
"""

import logging

from flask import jsonify

from rinq.database.db import get_db
from rinq.services.auth import login_required, get_current_user

logger = logging.getLogger(__name__)


def register(bp):

    @bp.route('/tina-says', methods=['GET'])
    @login_required
    def tina_says():
        """Today's Tina Says lines plus a private line for the viewer.

        The first request of the day starts generation in the background and
        returns status 'generating' with no lines; the page asks again shortly.
        Never errors at the page: a broken Tina Says must not touch the phone.
        """
        from rinq.services import tina_says as ts

        try:
            db = get_db()
            user = get_current_user()

            staff = []
            try:
                from rinq.integrations import get_staff_directory
                staff_dir = get_staff_directory()
                if staff_dir:
                    staff = [s for s in staff_dir.get_active_staff() if s.get('email')]
            except Exception as e:
                logger.warning(f"Tina Says: couldn't load staff list: {e}")

            status = ts.ensure_today(db, staff)
            lines = [{'text': ln['text'], 'kind': ln['kind']} for ln in ts.todays_lines(db)]

            personal = None
            try:
                personal = ts.personal_line(db, user.email, user.name)
            except Exception as e:
                logger.warning(f"Tina Says: personal line failed for {user.email}: {e}")

            return jsonify({'status': 'done' if lines else status, 'lines': lines, 'personal': personal})
        except Exception as e:
            logger.exception(f"Tina Says endpoint failed: {e}")
            return jsonify({'status': 'failed', 'lines': [], 'personal': None})
