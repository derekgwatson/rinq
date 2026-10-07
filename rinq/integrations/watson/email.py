"""Email service backed by Mabel (bot-team email bot).

Mabel sends via Watson's Google Workspace SMTP, so no Watson-specific
credentials end up in Rinq. Configured via:
    WATSON_MABEL_URL=https://mabel.watsonblinds.com.au
    BOT_API_KEY=shared-bot-team-key
"""

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


class WatsonMabelEmailService(EmailService):
    """Email service that delegates to Mabel over HTTP."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            # Longer timeout — voicemail emails carry base64 MP3 attachments
            self._client = BotHttpClient(get_bot_url('mabel'), timeout=30)
        return self._client

    def send_email(self, to: str, subject: str, text_body: str,
                   attachments: list[dict] = None,
                   metadata: dict = None) -> Optional[str]:
        payload = {
            'to': to,
            'subject': subject,
            'text_body': text_body,
        }
        if attachments:
            payload['attachments'] = [
                {
                    'filename': att.get('filename', 'file'),
                    'content_type': att.get('content_type', 'application/octet-stream'),
                    'content_base64': att.get('content_base64', ''),
                }
                for att in attachments
            ]
        if metadata:
            payload['metadata'] = metadata

        try:
            # Mabel mounts its email routes under /api. '/send-email' 404'd on every
            # recording archive email (2,076 in two weeks, none ever sent) until
            # 2026-10-07. No from_address: Mabel's default sender is noreply@,
            # which Google's relay accepts (mabel.bot@ was refused).
            response = self.client.post('/api/send-email', json=payload)
            if response.status_code == 200:
                message_id = response.json().get('message_id')
                logger.info(f"Email sent via Mabel: {message_id}")
                return message_id
            logger.error(f"Mabel email failed: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to send email via Mabel: {e}")
        return None
