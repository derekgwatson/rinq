"""Watson permission service integration via Grant bot."""

import logging

try:
    from shared.http_client import BotHttpClient
except ImportError:
    from rinq.vendor.http_client import BotHttpClient
try:
    from shared.config.ports import get_bot_url
except ImportError:
    from rinq.vendor.ports import get_bot_url

from rinq.integrations.base import PermissionService

logger = logging.getLogger(__name__)


class WatsonPermissionService(PermissionService):
    """Permission service backed by Grant (bot-team RBAC bot)."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('grant'), timeout=10)
        return self._client

    def get_permissions(self, bot: str) -> list[dict]:
        try:
            response = self.client.get('/api/permissions', params={'bot': bot})
            if response.status_code == 200:
                return response.json().get('permissions', [])
            logger.warning(f"Grant permissions fetch failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not fetch permissions from Grant: {e}")
        return []

    def add_permission(self, email: str, bot: str, role: str,
                       granted_by: str) -> bool:
        try:
            response = self.client.post('/api/permissions', json={
                'email': email,
                'bot': bot,
                'role': role,
                'granted_by': granted_by,
            })
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Failed to add permission via Grant: {e}")
        return False

    def remove_permission(self, email: str, bot: str,
                          revoked_by: str) -> bool:
        try:
            response = self.client.delete('/api/permissions', json={
                'email': email,
                'bot': bot,
                'revoked_by': revoked_by,
            })
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Failed to remove permission via Grant: {e}")
        return False
