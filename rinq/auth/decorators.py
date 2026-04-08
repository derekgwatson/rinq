"""
Standalone auth decorators for Tina/Rinq.

Provides the same interface as GatewayAuth:
- login_required
- admin_required
- get_current_user()
- api_or_session_auth (for API endpoints)
"""

import os
import logging
from functools import wraps
from flask import session, redirect, url_for, g, jsonify, request

logger = logging.getLogger(__name__)


class User:
    """User object matching the interface GatewayAuth users provide."""
    def __init__(self, id, email, name='', picture='', role='user'):
        self.id = id
        self.email = email
        self.name = name
        self.picture = picture
        self._role = role
        self.role = role
        self.is_admin = (role == 'admin')
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
    tenant = getattr(g, 'tenant', None)
    if tenant:
        from rinq.database.master import get_master_db
        master_db = get_master_db()
        role = master_db.get_user_role_in_tenant(user_id, tenant['id']) or 'user'

    user = User(
        id=user_id,
        email=session.get('user_email', ''),
        name=session.get('user_name', ''),
        picture=session.get('user_picture', ''),
        role=role,
    )
    g._current_user = user
    return user


def login_required(f):
    """Require authenticated user."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            if request.headers.get('X-API-Key') or \
               request.accept_mimetypes.best_match(['application/json', 'text/html']) == 'application/json':
                return jsonify({'error': 'Authentication required'}), 401
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



def api_or_session_auth(view_func):
    """Allow either API key, session auth, or direct unix socket access.

    Standalone version — checks BOT_API_KEY env var for API access,
    falls back to session auth for web UI, and allows unauthenticated
    requests that arrive directly on the unix socket (no X-Forwarded-For
    header means no nginx proxy, so it must be a local process).
    """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        # Direct unix socket access (no proxy) — trusted local request
        if not request.headers.get('X-Forwarded-For'):
            g.api_caller = 'local'
            return view_func(*args, **kwargs)

        header_key = request.headers.get("X-API-Key")

        if header_key:
            bot_api_key = os.environ.get('BOT_API_KEY', '')
            if header_key == bot_api_key:
                g.api_caller = 'bot'
                return view_func(*args, **kwargs)
            return jsonify({"error": "Invalid API key"}), 403

        # Check session auth
        if session.get('user_id'):
            g.api_caller = f"session:{session.get('user_email', 'unknown')}"
            return view_func(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return wrapper


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
