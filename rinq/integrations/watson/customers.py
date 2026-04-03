"""Watson customer lookup integration via Clara bot."""

import logging
from typing import Optional

try:
    from shared.http_client import BotHttpClient
except ImportError:
    from rinq.vendor.http_client import BotHttpClient
try:
    from shared.config.ports import get_bot_url
except ImportError:
    from rinq.vendor.ports import get_bot_url

from rinq.integrations.base import CustomerLookup

logger = logging.getLogger(__name__)


class WatsonCustomerLookup(CustomerLookup):
    """Customer lookup backed by Clara (bot-team CRM bot)."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('clara'), timeout=10)
        return self._client

    def find_by_phone(self, phone_number: str) -> Optional[dict]:
        try:
            # Normalize phone for search
            search_phone = phone_number.replace(' ', '').replace('-', '')
            if search_phone.startswith('+61'):
                search_phone = '0' + search_phone[3:]

            response = self.client.get('/api/customers', params={'q': search_phone, 'limit': 1})
            if response.status_code == 200:
                customers = response.json().get('customers', [])
                if customers:
                    c = customers[0]
                    logger.info(f"Found customer for {phone_number}: {c.get('name')} (ID: {c.get('id')})")
                    return {
                        'id': c.get('id'),
                        'name': c.get('name'),
                        'email': c.get('primary_email') or c.get('email'),
                    }
                logger.debug(f"No customer found for phone {phone_number}")
            else:
                logger.warning(f"Clara search failed: {response.status_code}")
        except Exception as e:
            logger.error(f"Error looking up customer: {e}")
        return None
