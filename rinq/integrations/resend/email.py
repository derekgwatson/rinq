"""Native Resend email service — direct API.

Config via env vars:
    RESEND_API_KEY=re_xxx
    RESEND_FROM_EMAIL=notifications@yourdomain.com
"""

import base64
import logging
import os
from typing import Optional

import requests

from rinq.integrations.base import EmailService

logger = logging.getLogger(__name__)


class ResendEmailService(EmailService):
    """Email service using the Resend API."""

    def __init__(self):
        self.api_key = os.environ.get('RESEND_API_KEY', '')
        self.from_email = os.environ.get('RESEND_FROM_EMAIL', '')

    @property
    def is_configured(self):
        return bool(self.api_key and self.from_email)

    def send_email(self, to: str, subject: str, text_body: str,
                   attachments: list[dict] = None,
                   metadata: dict = None) -> Optional[str]:
        if not self.is_configured:
            logger.warning("Resend not configured — skipping email")
            return None

        try:
            payload = {
                'from': self.from_email,
                'to': [to],
                'subject': subject,
                'text': text_body,
            }

            if attachments:
                payload['attachments'] = [
                    {
                        'filename': att.get('filename', 'file'),
                        'content': att.get('content_base64', ''),
                        'content_type': att.get('content_type', 'application/octet-stream'),
                    }
                    for att in attachments
                ]

            response = requests.post(
                'https://api.resend.com/emails',
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=30,
            )

            if response.status_code == 200:
                message_id = response.json().get('id')
                logger.info(f"Email sent via Resend: {message_id}")
                return message_id
            else:
                logger.error(f"Resend email failed: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to send email via Resend: {e}")
        return None
