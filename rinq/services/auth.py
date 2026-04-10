"""
Authentication compatibility layer for Rinq.

Auth is handled by GatewayAuth in app.py, which injects the actual
decorators into this module at runtime for backward compatibility
with routes that import from here.
"""


# These get set at runtime by app.py via GatewayAuth
auth = None
login_required = None
admin_required = None
get_current_user = None


