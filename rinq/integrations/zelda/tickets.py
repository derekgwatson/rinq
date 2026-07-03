"""Zelda ticket service integration (Watson bot-team native ticketing).

Replaces the Sadie/Zendesk path (`WatsonTicketService`) after the Zendesk
decommission. Talks straight to Zelda's ticket API.

Config (env):
    RINQ_TICKET_PROVIDER=zelda
    WATSON_ZELDA_URL=https://zelda.watsonblinds.com.au   (standalone deploys)
    BOT_API_KEY=...                                       (already set for Mabel etc.)
"""

import logging
import time
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

# Stored voicemail-destination group ids predate the Zelda cutover, so a value
# this large can only be a Zendesk group id (ZD ids are 10+ digits; Zelda ids
# are small autoincrements).
_ZENDESK_ID_FLOOR = 1_000_000_000

_GROUPS_CACHE_TTL = 300  # seconds


class ZeldaTicketService(TicketService):
    """Ticket service backed by Zelda (bot-team native ticketing)."""

    def __init__(self):
        self._client = None
        self._groups_cache = None
        self._groups_cached_at = 0.0

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('zelda'), timeout=60)
        return self._client

    def create_ticket(self, subject: str, description: str,
                      priority: str = 'normal', ticket_type: str = 'task',
                      tags: list[str] = None,
                      requester_email: str = None,
                      requester_name: str = None,
                      group_id: str = None,
                      attachments: list[dict] = None) -> Optional[dict]:
        # ticket_type has no Zelda equivalent — intentionally dropped.
        try:
            payload = {
                'subject': subject,
                'body': description,
                'priority': priority,
            }
            if tags:
                payload['tags'] = tags
            if requester_email:
                payload['requester_email'] = requester_email
            if requester_name:
                payload['requester_name'] = requester_name
            if group_id:
                resolved = self._resolve_group_id(group_id)
                if resolved:
                    payload['group_id'] = resolved
            if attachments:
                # Rinq interface uses 'content_base64'; Zelda's API calls it 'data'.
                payload['attachments'] = [
                    {
                        'filename': a.get('filename'),
                        'content_type': a.get('content_type'),
                        'data': a.get('content_base64') or a.get('data'),
                    }
                    for a in attachments
                ]

            response = self.client.post('/api/tickets', json=payload)
            if response.status_code == 201:
                return response.json().get('ticket')
            logger.error(f"Zelda ticket creation failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Failed to create ticket via Zelda: {e}")
        return None

    def add_comment(self, ticket_id: str, body: str,
                    public: bool = False) -> bool:
        try:
            response = self.client.post(
                f'/api/tickets/{ticket_id}/comments',
                json={'body': body, 'is_public': public},
                timeout=30,
            )
            return response.status_code in (200, 201)
        except Exception as e:
            logger.warning(f"Failed to add comment to Zelda ticket {ticket_id}: {e}")
        return False

    def get_groups(self) -> list[dict]:
        try:
            response = self.client.get('/api/groups', timeout=10)
            if response.status_code == 200:
                return response.json().get('groups', [])
            logger.warning(f"Zelda groups fetch failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not fetch groups from Zelda: {e}")
        return []

    # -- internal -----------------------------------------------------------

    def _cached_groups(self) -> list[dict]:
        now = time.time()
        if self._groups_cache is None or now - self._groups_cached_at > _GROUPS_CACHE_TTL:
            groups = self.get_groups()
            if groups:
                self._groups_cache = groups
                self._groups_cached_at = now
        return self._groups_cache or []

    def _resolve_group_id(self, group_id) -> Optional[int]:
        """Translate a stored group id to a Zelda group id.

        Voicemail destinations saved before the cutover hold Zendesk group
        ids. Zelda's groups carry their old ``zendesk_id``, so those translate
        transparently; values that already match a Zelda group id pass
        through. Unknown ids are dropped (ticket is created ungrouped and
        Zelda's routing rules take over) rather than written as a dangling
        group reference.
        """
        wanted = str(group_id)
        groups = self._cached_groups()
        if not groups:
            # Can't verify. Pass small ids through (already Zelda ids); drop
            # obvious Zendesk ids rather than store a dangling reference.
            try:
                if int(wanted) < _ZENDESK_ID_FLOOR:
                    return int(wanted)
            except (TypeError, ValueError):
                pass
            logger.warning(f"Zelda groups unavailable; dropping group id {group_id}")
            return None

        for g in groups:
            if str(g.get('id')) == wanted:
                return g['id']
        for g in groups:
            if g.get('zendesk_id') is not None and str(g['zendesk_id']) == wanted:
                logger.info(
                    f"Translated legacy Zendesk group id {group_id} -> Zelda group "
                    f"{g['id']} ({g.get('name')})")
                return g['id']

        logger.warning(f"Group id {group_id} matches no Zelda group; creating ungrouped")
        return None
