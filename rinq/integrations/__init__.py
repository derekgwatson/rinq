"""
Pluggable integration interfaces for Rinq.

Each interface defines what Rinq needs from an external system.
Implementations live in subpackages (zendesk/, watson/, etc).

Usage:
    from rinq.integrations import get_ticket_service
    tickets = get_ticket_service()
    tickets.create_ticket(subject='Test', description='Hello')
"""

import os
import logging

logger = logging.getLogger(__name__)

# Singleton instances — set during app initialization
_staff_directory = None
_ticket_service = None
_permission_service = None
_customer_lookup = None
_order_lookup = None
_email_service = None
_ai_receptionist = None


def init_integrations(provider: str = 'none', **kwargs):
    """Initialize integration services.

    Args:
        provider: Base provider ('watson' for bot-team, 'none' for standalone)

    Individual integrations can also be configured via env vars:
        RINQ_TICKET_PROVIDER=zendesk  (native Zendesk API)
    """
    global _staff_directory, _ticket_service, _permission_service
    global _customer_lookup, _order_lookup, _email_service, _ai_receptionist

    if provider == 'watson':
        from rinq.integrations.watson import (
            WatsonStaffDirectory,
            WatsonTicketService,
            WatsonPermissionService,
            WatsonCustomerLookup,
            WatsonOrderLookup,
            WatsonEmailService,
            WatsonAIReceptionist,
        )
        _staff_directory = WatsonStaffDirectory()
        _ticket_service = WatsonTicketService()
        _permission_service = WatsonPermissionService()
        _customer_lookup = WatsonCustomerLookup()
        _order_lookup = WatsonOrderLookup()
        _email_service = WatsonEmailService()
        _ai_receptionist = WatsonAIReceptionist()
        logger.info("Integrations initialized: watson (bot-team)")

    # Override individual integrations via env vars
    ticket_provider = os.environ.get('RINQ_TICKET_PROVIDER', '')
    if ticket_provider == 'zendesk':
        from rinq.integrations.zendesk import ZendeskTicketService
        _ticket_service = ZendeskTicketService()
        logger.info("Ticket service: zendesk (native API)")
    elif not _ticket_service:
        # Auto-detect: if Zendesk env vars are set, use native Zendesk
        if os.environ.get('ZENDESK_SUBDOMAIN'):
            from rinq.integrations.zendesk import ZendeskTicketService
            _ticket_service = ZendeskTicketService()
            logger.info("Ticket service: zendesk (auto-detected from env)")

    # Email service
    email_provider = os.environ.get('RINQ_EMAIL_PROVIDER', '')
    if email_provider == 'resend':
        from rinq.integrations.resend import ResendEmailService
        _email_service = ResendEmailService()
        logger.info("Email service: resend (native API)")
    elif not _email_service:
        if os.environ.get('RESEND_API_KEY'):
            from rinq.integrations.resend import ResendEmailService
            _email_service = ResendEmailService()
            logger.info("Email service: resend (auto-detected from env)")


def get_staff_directory():
    """Get the staff directory integration."""
    return _staff_directory


def get_ticket_service():
    """Get the ticket service integration."""
    return _ticket_service


def get_permission_service():
    """Get the permission service integration."""
    return _permission_service


def get_customer_lookup():
    """Get the customer lookup integration."""
    return _customer_lookup


def get_order_lookup():
    """Get the order lookup integration."""
    return _order_lookup


def get_email_service():
    """Get the email service integration."""
    return _email_service


def get_ai_receptionist():
    """Get the AI receptionist integration."""
    return _ai_receptionist
