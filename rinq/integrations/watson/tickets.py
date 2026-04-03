"""Watson ticket service integration via Sadie bot (Zendesk)."""

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

from rinq.integrations.base import TicketService

logger = logging.getLogger(__name__)


class WatsonTicketService(TicketService):
    """Ticket service backed by Sadie (bot-team Zendesk bot)."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('sadie'), timeout=60)
        return self._client

    def create_ticket(self, subject: str, description: str,
                      priority: str = 'normal', ticket_type: str = 'task',
                      tags: list[str] = None,
                      requester_email: str = None,
                      requester_name: str = None,
                      group_id: str = None,
                      attachments: list[dict] = None) -> Optional[dict]:
        try:
            payload = {
                'subject': subject,
                'description': description,
                'priority': priority,
                'type': ticket_type,
            }
            if tags:
                payload['tags'] = tags
            if requester_email:
                payload['requester_email'] = requester_email
            if requester_name:
                payload['requester_name'] = requester_name
            if group_id:
                payload['group_id'] = group_id
            if attachments:
                payload['attachments'] = attachments

            response = self.client.post('/api/tickets', json=payload)
            if response.status_code == 201:
                return response.json().get('ticket')
            logger.error(f"Sadie ticket creation failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Failed to create ticket via Sadie: {e}")
        return None

    def add_comment(self, ticket_id: str, body: str,
                    public: bool = False) -> bool:
        try:
            response = self.client.post(
                f'/api/tickets/{ticket_id}/comments',
                json={'body': body, 'public': public},
                timeout=30,
            )
            return response.status_code in (200, 201)
        except Exception as e:
            logger.warning(f"Failed to add comment to ticket {ticket_id}: {e}")
        return False

    def get_groups(self) -> list[dict]:
        try:
            response = self.client.get('/api/groups', timeout=10)
            if response.status_code == 200:
                return response.json().get('groups', [])
            logger.warning(f"Sadie groups fetch failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not fetch groups from Sadie: {e}")
        return []
