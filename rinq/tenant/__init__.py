"""
Multi-tenant support for Rinq.

Each tenant gets their own database and Twilio config.
Tenants are resolved from session (web) or from the called
phone number (Twilio webhooks).
"""
