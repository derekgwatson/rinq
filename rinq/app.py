"""
Rinq — Cloud Phone System
"""

import sys
import os
import json
import logging
from pathlib import Path

# Add parent directory to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from rinq.config import config
from rinq.integrations import init_integrations

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-prod')
app.config['BOT_NAME'] = config.name

# Add custom Jinja2 filters
@app.template_filter('fromjson')
def fromjson_filter(value):
    """Parse JSON string into Python object."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}

# ProxyFix for production (nginx)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Initialize auth — standalone (default) or GatewayAuth (bot-team)
auth_mode = os.environ.get('RINQ_AUTH', 'standalone')
import rinq.services.auth as auth_module

if auth_mode == 'standalone':
    from rinq.auth.oauth import auth_bp as standalone_auth_bp
    from rinq.auth.decorators import (
        login_required, admin_required, manager_required, get_current_user,
    )
    app.register_blueprint(standalone_auth_bp)
    auth_module.login_required = login_required
    auth_module.admin_required = admin_required
    auth_module.manager_required = manager_required
    auth_module.get_current_user = get_current_user
    logger.info("Auth mode: standalone (direct Google OAuth)")
else:
    from shared.auth import GatewayAuth
    auth = GatewayAuth(app, config)
    auth_module.auth = auth
    auth_module.login_required = auth.login_required
    auth_module.admin_required = auth.admin_required
    auth_module.get_current_user = auth.get_current_user
    logger.info("Auth mode: gateway (Chester/GatewayAuth)")

# Initialize integrations (none by default, watson for bot-team)
integration_provider = os.environ.get('RINQ_INTEGRATIONS', 'none')
if integration_provider != 'none':
    init_integrations(integration_provider)

# Multi-tenant middleware (optional)
if config.multi_tenant:
    from rinq.tenant.middleware import resolve_tenant
    app.before_request(resolve_tenant)
    logger.info("Multi-tenant mode enabled")

# Context processor for shared CSS and staging banner
@app.context_processor
def inject_globals():
    ctx = {}
    try:
        from shared.config.ports import get_shared_css_context, get_staging_banner_context
        ctx = get_shared_css_context()
        ctx.update(get_staging_banner_context())
    except (ImportError, Exception):
        # Standalone mode — no shared CSS from Chester
        ctx['shared_css_url'] = None
        ctx['staging_banner'] = False
    ctx['config'] = config
    ctx['product_name'] = config.product_name
    return ctx

# Import blueprints AFTER auth is initialized
from rinq.api.routes import api_bp
from rinq.web.routes import web_bp

# Register blueprints
app.register_blueprint(api_bp, url_prefix='/api')
app.register_blueprint(web_bp)

# Register error handlers
try:
    from shared.error_handlers import register_error_handlers
    register_error_handlers(app, logger, superadmin_emails='auto')
except ImportError:
    # Standalone mode — basic error handlers
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(500)
    def server_error(e):
        logger.exception("Server error")
        return jsonify({'error': 'Internal server error'}), 500


# =============================================================================
# System Endpoints
# =============================================================================

@app.route('/switch-tenant/<tenant_id>')
def switch_tenant(tenant_id):
    """Switch to a different tenant."""
    from flask import session
    if not session.get('user_id'):
        return redirect('/login')
    if config.multi_tenant:
        from rinq.database.master import get_master_db
        master_db = get_master_db()
        tenant = master_db.get_tenant(tenant_id)
        if tenant:
            session['tenant_id'] = tenant_id
    return redirect('/')


@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'bot': config.name,
        'version': config.version
    })


@app.route('/info')
def info():
    """Bot information endpoint."""
    return jsonify({
        'name': config.name,
        'description': config.description,
        'version': config.version,
        'emoji': '📞',
        'roles': [
            {'value': 'user', 'label': 'User - Phone access'},
            {'value': 'manager', 'label': 'Manager - Reports, leaderboard, team management'},
            {'value': 'admin', 'label': 'Admin - Full access including system config'},
        ],
        'endpoints': {
            'web': {
                'GET /': 'Dashboard - phone numbers and forwarding',
                'GET /activity': 'Activity log',
                'GET /recordings': 'Call recordings log',
                'GET /setup': 'Configuration status',
            },
            'api': {
                'GET /api/status': 'Twilio connection status',
                'GET /api/phone-numbers': 'List phone numbers',
                'POST /api/phone-numbers/sync': 'Sync from Twilio',
                'POST /api/phone-numbers/<sid>/forward': 'Update forwarding',
                'GET /api/sip-domains': 'List SIP domains',
                'GET /api/credential-lists': 'List credential lists',
                'POST /api/users': 'Create SIP user (onboarding)',
                'DELETE /api/users/<cred_list>/<cred_sid>': 'Delete SIP user (offboarding)',
                'POST /api/voice/incoming': 'Incoming call handler (from Twilio)',
                'POST /api/voice/voicemail': 'Voicemail handler (from Twilio)',
                'POST /api/test-call': 'Make a test call',
                'GET /api/staff-phones': 'List staff extensions (for PAM)',
                'GET /api/staff-phones/<email>': 'Get staff extension details',
            },
            'system': {
                'GET /health': 'Health check',
                'GET /info': 'Bot information',
            }
        }
    })


# =============================================================================
# Main
# =============================================================================

if __name__ == '__main__':
    print("\n" + "="*50)
    print(f"📞 Hi! I'm {config.product_name}")
    print(f"   {config.description}")
    print(f"   Running on http://localhost:{config.server_port}")
    print("="*50 + "\n")

    app.run(
        host=config.server_host,
        port=config.server_port,
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    )
