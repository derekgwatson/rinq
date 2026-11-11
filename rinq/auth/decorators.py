"""
Standalone auth decorators for Rinq.

Provides the same interface as GatewayAuth:
- login_required
- admin_required
- get_current_user()
- api_or_session_auth (for API endpoints)
"""

import hmac
import os
import logging
from functools import wraps
from flask import session, redirect, url_for, g, jsonify, request

logger = logging.getLogger(__name__)


class User:
    """User object matching the interface GatewayAuth users provide."""
    def __init__(self, id, email, name='', picture='', role='user', can_view_all_recordings=False):
        self.id = id
        self.email = email
        self.name = name
        self.picture = picture
        self._role = role
        self.role = role
        self.is_admin = (role == 'admin')
        self.is_manager = (role in ('admin', 'manager'))
        self.can_view_all_recordings = bool(can_view_all_recordings) or self.is_manager
        self.is_authenticated = True

    def __repr__(self):
        return f"<User {self.email} role={self.role}>"


def get_current_user():
    """Get the current logged-in user, or None."""
    if hasattr(g, '_current_user'):
        return g._current_user

    user_id = session.get('user_id')
    if not user_id:
        g._current_user = None
        return None

    role = 'user'
    can_view_all_recordings = False
    tenant = getattr(g, 'tenant', None)
    if tenant:
        from rinq.database.master import get_master_db
        master_db = get_master_db()
        data = master_db.get_user_tenant_data(user_id, tenant['id'])
        if data:
            role = data['role'] or 'user'
            can_view_all_recordings = bool(data.get('can_view_all_recordings'))
        else:
            # No membership row for the current tenant — do NOT default to a
            # 'user' role in someone else's tenant. Treat as unauthenticated
            # for this tenant (login_required sends them back to /login).
            logger.warning(
                f"User {session.get('user_email', user_id)} has no membership "
                f"in tenant '{tenant['id']}' — denying"
            )
            g._current_user = None
            return None

    user = User(
        id=user_id,
        email=session.get('user_email', ''),
        name=session.get('user_name', ''),
        picture=session.get('user_picture', ''),
        role=role,
        can_view_all_recordings=can_view_all_recordings,
    )
    g._current_user = user
    return user


def login_required(f):
    """Require authenticated user with access to the current tenant."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.headers.get('X-API-Key') or \
               request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json':
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('standalone_auth.login'))
        # Session exists but the user may have no membership in the current
        # tenant (get_current_user returns None in that case) — send them
        # back through login rather than serving another tenant's data.
        if get_current_user() is None:
            if request.headers.get('X-API-Key') or \
               request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json':
                return jsonify({'error': 'No access to this tenant'}), 403
            # Clear the whole session: /login redirects logged-in users back
            # to '/', so keeping user_id here would loop forever.
            session.clear()
            return redirect(url_for('standalone_auth.login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Require authenticated admin user."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.headers.get('X-API-Key'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('standalone_auth.login'))

        user = get_current_user()
        if not user or not user.is_admin:
            if request.headers.get('X-API-Key'):
                return jsonify({'error': 'Admin access required'}), 403
            return "Access denied", 403

        return f(*args, **kwargs)
    return decorated



def manager_required(f):
    """Require authenticated manager or admin user."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.headers.get('X-API-Key'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('standalone_auth.login'))

        user = get_current_user()
        if not user or not user.is_manager:
            if request.headers.get('X-API-Key'):
                return jsonify({'error': 'Manager access required'}), 403
            return "Access denied", 403

        return f(*args, **kwargs)
    return decorated


def _valid_bot_key(header_key) -> bool:
    expected = os.environ.get('BOT_API_KEY', '')
    return bool(header_key and expected) and hmac.compare_digest(str(header_key), expected)


def _api_auth(view_func, require_admin):
    """Shared body of api_or_session_auth / admin_or_api."""
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        # Direct unix socket access (no proxy) — trusted local request
        if not request.headers.get('X-Forwarded-For'):
            g.api_caller = 'local'
            return view_func(*args, **kwargs)

        header_key = request.headers.get("X-API-Key")

        if header_key:
            if _valid_bot_key(header_key):
                g.api_caller = 'bot'
                return view_func(*args, **kwargs)
            return jsonify({"error": "Invalid API key"}), 403

        # Session auth: the person must still be a member of the current
        # tenant (get_current_user() is None otherwise).
        if session.get('user_id'):
            user = get_current_user() if getattr(g, 'tenant', None) else None
            if user is None:
                return jsonify({"error": "No access to this tenant"}), 403
            if require_admin and not user.is_admin:
                return jsonify({"error": "Admin access required"}), 403
            g.api_caller = f"session:{session.get('user_email', 'unknown')}"
            return view_func(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return wrapper


def api_or_session_auth(view_func):
    """Allow either API key, session auth, or direct unix socket access.

    Standalone version — checks BOT_API_KEY env var for API access,
    falls back to session auth for web UI (the user must be a member of the
    current tenant), and allows unauthenticated requests that arrive directly
    on the unix socket (no X-Forwarded-For header means no nginx proxy, so it
    must be a local process).
    """
    return _api_auth(view_func, require_admin=False)


def admin_or_api(view_func):
    """Like api_or_session_auth, but a session caller must be a tenant admin.

    For admin, bot and cron endpoints: the bot key and local (unix socket)
    callers are accepted as before.
    """
    return _api_auth(view_func, require_admin=True)


def is_session_caller() -> bool:
    """True when the current API request was authorised by a user session."""
    return (getattr(g, 'api_caller', None) or '').startswith('session:')


def get_api_caller():
    """Get the caller identity string (matches shared.auth.bot_api interface).

    Returns prefixed strings like 'session:user@example.com' or 'bot'.
    Use this for audit/logging. For a clean email, use get_api_caller_email().
    """
    return getattr(g, 'api_caller', None)


def get_api_caller_email():
    """Get the caller's email address (no prefix).

    Returns the raw email for session callers, 'bot' for API callers,
    or None if unauthenticated.
    """
    caller = getattr(g, 'api_caller', None)
    if caller and caller.startswith('session:'):
        return caller[8:]
    return caller
