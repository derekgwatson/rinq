"""Watson AI receptionist integration via Rosie bot."""

import logging
import os
from typing import Optional

try:
    from shared.http_client import BotHttpClient
except ImportError:
    from rinq.vendor.http_client import BotHttpClient
try:
    from shared.config.ports import get_bot_url
except ImportError:
    from rinq.vendor.ports import get_bot_url

from rinq.integrations.base import AIReceptionist

logger = logging.getLogger(__name__)


class WatsonAIReceptionist(AIReceptionist):
    """AI receptionist backed by Rosie (bot-team AI voice bot)."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('rosie'), timeout=10)
        return self._client

    def notify_call_ended(self, call_sid: str, call_status: str) -> bool:
        try:
            response = self.client.post('/api/voice/status', json={
                'CallSid': call_sid,
                'CallStatus': call_status,
            })
            return response.ok
        except Exception as e:
            logger.warning(f"Failed to notify Rosie of call end: {e}")
        return False

    def get_answer_url(self) -> Optional[str]:
        webhook_url = os.environ.get('ROSIE_WEBHOOK_URL')
        if webhook_url:
            return f"{webhook_url}/api/voice/answer"
        return None
