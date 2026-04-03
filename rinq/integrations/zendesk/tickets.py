"""Native Zendesk ticket service — direct API, no bot-team dependency.

Config via env vars:
    ZENDESK_SUBDOMAIN=yourcompany
    ZENDESK_EMAIL=user@example.com
    ZENDESK_API_TOKEN=xxx
"""

import base64
import logging
import os
from typing import Optional

import requests

from rinq.integrations.base import TicketService

logger = logging.getLogger(__name__)


class ZendeskTicketService(TicketService):
    """Ticket service using the Zendesk API directly."""

    def __init__(self):
        self.subdomain = os.environ.get('ZENDESK_SUBDOMAIN', '')
        self.email = os.environ.get('ZENDESK_EMAIL', '')
        self.api_token = os.environ.get('ZENDESK_API_TOKEN', '')

    @property
    def base_url(self):
        return f"https://{self.subdomain}.zendesk.com/api/v2"

    @property
    def auth(self):
        return (f"{self.email}/token", self.api_token)

    @property
    def is_configured(self):
        return bool(self.subdomain and self.email and self.api_token)

    def create_ticket(self, subject: str, description: str,
                      priority: str = 'normal', ticket_type: str = 'task',
                      tags: list[str] = None,
                      requester_email: str = None,
                      requester_name: str = None,
                      group_id: str = None,
                      attachments: list[dict] = None) -> Optional[dict]:
        if not self.is_configured:
            logger.warning("Zendesk not configured — skipping ticket creation")
            return None

        try:
            # Upload attachments first if any
            attachment_tokens = []
            if attachments:
                for att in attachments:
                    token = self._upload_attachment(att)
                    if token:
                        attachment_tokens.append(token)

            # Build ticket payload
            ticket = {
                'subject': subject,
                'comment': {
                    'body': description,
                },
                'priority': priority,
                'type': ticket_type,
            }
            if tags:
                ticket['tags'] = tags
            if requester_email:
                ticket['requester'] = {'email': requester_email}
                if requester_name:
                    ticket['requester']['name'] = requester_name
            if group_id:
                ticket['group_id'] = int(group_id)
            if attachment_tokens:
                ticket['comment']['uploads'] = attachment_tokens

            response = requests.post(
                f"{self.base_url}/tickets.json",
                auth=self.auth,
                json={'ticket': ticket},
                timeout=60,
            )

            if response.status_code == 201:
                data = response.json().get('ticket', {})
                logger.info(f"Zendesk ticket #{data.get('id')} created")
                return {'id': data.get('id')}
            else:
                logger.error(f"Zendesk ticket creation failed: {response.status_code} - {response.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to create Zendesk ticket: {e}")
        return None

    def add_comment(self, ticket_id: str, body: str,
                    public: bool = False) -> bool:
        if not self.is_configured:
            return False

        try:
            response = requests.put(
                f"{self.base_url}/tickets/{ticket_id}.json",
                auth=self.auth,
                json={'ticket': {'comment': {'body': body, 'public': public}}},
                timeout=30,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Failed to add comment to ticket {ticket_id}: {e}")
        return False

    def get_groups(self) -> list[dict]:
        if not self.is_configured:
            return []

        try:
            response = requests.get(
                f"{self.base_url}/groups.json",
                auth=self.auth,
                timeout=10,
            )
            if response.status_code == 200:
                return [
                    {'id': g['id'], 'name': g['name']}
                    for g in response.json().get('groups', [])
                ]
        except Exception as e:
            logger.warning(f"Failed to fetch Zendesk groups: {e}")
        return []

    def _upload_attachment(self, attachment: dict) -> Optional[str]:
        """Upload an attachment to Zendesk and return the upload token."""
        try:
            filename = attachment.get('filename', 'file')
            content_type = attachment.get('content_type', 'application/octet-stream')
            content_b64 = attachment.get('content_base64', '')

            if not content_b64:
                return None

            file_data = base64.b64decode(content_b64)

            response = requests.post(
                f"{self.base_url}/uploads.json?filename={filename}",
                auth=self.auth,
                headers={'Content-Type': content_type},
                data=file_data,
                timeout=60,
            )

            if response.status_code == 201:
                return response.json().get('upload', {}).get('token')
            else:
                logger.warning(f"Zendesk attachment upload failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Failed to upload Zendesk attachment: {e}")
        return None
