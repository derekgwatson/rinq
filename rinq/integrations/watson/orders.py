"""Watson order lookup integration via Otto bot."""

import logging

try:
    from shared.http_client import BotHttpClient
except ImportError:
    from rinq.vendor.http_client import BotHttpClient
try:
    from shared.config.ports import get_bot_url
except ImportError:
    from rinq.vendor.ports import get_bot_url

from rinq.integrations.base import OrderLookup

logger = logging.getLogger(__name__)


class WatsonOrderLookup(OrderLookup):
    """Order lookup backed by Otto (bot-team order management bot)."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('otto'), timeout=10)
        return self._client

    def get_wip_orders(self) -> list[dict]:
        try:
            response = self.client.get('/api/orders/wip')
            if response.status_code == 200:
                return response.json().get('orders', [])
            logger.warning(f"Otto WIP orders failed: {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching WIP orders from Otto: {e}")
        return []
