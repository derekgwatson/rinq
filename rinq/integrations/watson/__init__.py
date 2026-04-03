"""
Watson Blinds (bot-team) integration implementations.

These use BotHttpClient to call other bots in the bot-team ecosystem.
"""

from rinq.integrations.watson.staff import WatsonStaffDirectory
from rinq.integrations.watson.tickets import WatsonTicketService
from rinq.integrations.watson.permissions import WatsonPermissionService
from rinq.integrations.watson.customers import WatsonCustomerLookup
from rinq.integrations.watson.orders import WatsonOrderLookup
from rinq.integrations.watson.ai_receptionist import WatsonAIReceptionist

__all__ = [
    'WatsonStaffDirectory',
    'WatsonTicketService',
    'WatsonPermissionService',
    'WatsonCustomerLookup',
    'WatsonOrderLookup',
    'WatsonAIReceptionist',
]
