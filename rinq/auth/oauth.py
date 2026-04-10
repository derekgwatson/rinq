"""
Google OAuth authentication for Rinq standalone mode.

Direct OAuth flow — no Chester gateway needed.
"""

import logging
from flask import Blueprint, redirect, url_for, session, request, flash, render_template
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)

auth_bp = Blueprint('standalone_auth', __name__)

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
]

SAAS_DOMAINS = ('rinq.cc', 'localhost', '127.0.0.1')


def _get_config():
    from rinq.config import config
    return config


def _get_flow(state=None):
    """Create Google OAuth flow using the current request's domain."""
    config = _get_config()
    try:
        from flask import request as flask_request
        callback_url = flask_request.host_url.rstrip('/')
    except RuntimeError:
        callback_url = config.webhook_base_url
    if not callback_url:
        callback_url = 'http://localhost:' + str(config.server_port)
    callback_url = callback_url.rstrip('/') + '/auth/callback'

    flow = Flow.from_client_config(
        {
            'web': {
                'client_id': config.google_client_id,
                'client_secret': config.google_client_secret,
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': [callback_url],
            }
        },
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = callback_url
    return flow


@auth_bp.route('/login')
def login():
    """Show login page."""
    config = _get_config()
    if session.get('user_id'):
        return redirect('/')

    from datetime import datetime
    from rinq.database.master import get_master_db

    host = request.host.split(':')[0].lower()
    is_saas = host in SAAS_DOMAINS
    product_name = config.product_name

    # Resolve tenant product name from domain for non-SaaS domains
    if not is_saas:
        master_db = get_master_db()
        tenant = master_db.get_tenant_by_domain(host)
        if tenant and tenant.get('product_name'):
            product_name = tenant['product_name']

    return render_template('login.html',
                          product_name=product_name,
                          is_saas=is_saas,
                          now=datetime.now())


@auth_bp.route('/login/go')
def do_login():
    """Redirect to Google OAuth consent screen."""
    config = _get_config()
    if not config.google_client_id or not config.google_client_secret:
        return "Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", 500

    flow = _get_flow()
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='select_account',
    )
    session['oauth_state'] = state
    session['code_verifier'] = flow.code_verifier
    return redirect(authorization_url)


@auth_bp.route('/auth/callback')
def callback():
    """Handle Google OAuth callback."""
    config = _get_config()
    state = session.pop('oauth_state', None)
    if not state:
        flash("Invalid authentication state. Please try again.", "error")
        return redirect(url_for('standalone_auth.login'))
    code_verifier = session.pop('code_verifier', None)
    flow = _get_flow(state=state)
    flow.code_verifier = code_verifier

    try:
        # Behind reverse proxy, request.url may be http:// even though the
        # actual request was https://. Force https to match the redirect_uri.
        auth_response = request.url
        if auth_response.startswith('http://') and 'localhost' not in auth_response:
            auth_response = 'https://' + auth_response[7:]
        flow.fetch_token(authorization_response=auth_response)
    except Exception as e:
        logger.error(f"OAuth token fetch failed: {e}", exc_info=True)
        flash(f"Authentication failed: {e}", "error")
        return redirect(url_for('standalone_auth.login'))

    credentials = flow.credentials
    try:
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            config.google_client_id,
        )
    except Exception as e:
        logger.error(f"Token verification failed: {e}")
        flash("Authentication failed. Please try again.", "error")
        return redirect(url_for('standalone_auth.login'))

    email = id_info.get('email', '').lower().strip()
    name = id_info.get('name', '')
    picture = id_info.get('picture', '')
    google_sub = id_info.get('sub', '')

    if not email:
        flash("Could not get email from Google.", "error")
        return redirect(url_for('standalone_auth.login'))

    from rinq.database.master import get_master_db
    master_db = get_master_db()
    user = master_db.get_or_create_user(
        email=email, name=name, picture=picture, google_sub=google_sub
    )
    session['user_id'] = user['id']
    session['user_email'] = user['email']
    session['user_name'] = user['name']
    session['user_picture'] = user.get('picture', '')

    # Auto-select first tenant
    tenants = master_db.get_user_tenants(user['id'])
    if not tenants:
        # Auto-provision: check if email domain matches any tenant's allowed_domains
        email_domain = email.split('@')[-1]
        matching_tenants = master_db.get_tenants_for_email_domain(email_domain)
        for tenant in matching_tenants:
            master_db.add_user_to_tenant(tenant['id'], user['id'], role='user')
            logger.info(f"Auto-provisioned {email} into tenant {tenant['id']} (domain match: {email_domain})")
        tenants = master_db.get_user_tenants(user['id'])

    if tenants:
        # Pick tenant: match by request domain first, then first available
        host = request.host.split(':')[0]
        domain_match = next((t for t in tenants if t.get('domain') == host), None)
        selected = domain_match or tenants[0]
        session['tenant_id'] = selected['id']
    else:
        flash("You don't have access to any tenants.", "error")
        session.clear()
        return redirect(url_for('standalone_auth.login'))

    logger.info(f"User logged in: {email}")
    return redirect('/')


@auth_bp.route('/logout')
def logout():
    """Log out the current user."""
    email = session.get('user_email', 'unknown')
    session.clear()
    logger.info(f"User logged out: {email}")
    return redirect(url_for('standalone_auth.login'))
