"""Watson email service integration via Mabel bot."""

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

from rinq.integrations.base import EmailService

logger = logging.getLogger(__name__)


class WatsonEmailService(EmailService):
    """Email service backed by Mabel (bot-team email bot)."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('mabel'), timeout=30)
        return self._client

    def send_email(self, to: str, subject: str, text_body: str,
                   attachments: list[dict] = None,
                   metadata: dict = None) -> Optional[str]:
        try:
            payload = {
                'to': to,
                'subject': subject,
                'text_body': text_body,
            }
            if attachments:
                payload['attachments'] = attachments
            if metadata:
                payload['metadata'] = metadata

            response = self.client.post('/api/send-email', json=payload)
            if response.status_code == 200:
                message_id = response.json().get('message_id')
                logger.info(f"Email sent via Mabel, message_id={message_id}")
                return message_id
            logger.error(f"Mabel email failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Failed to send email via Mabel: {e}")
        return None
