"""Watson staff directory integration via Peter bot."""

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

from rinq.integrations.base import StaffDirectory

logger = logging.getLogger(__name__)


class WatsonStaffDirectory(StaffDirectory):
    """Staff directory backed by Peter (bot-team HR bot)."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> BotHttpClient:
        if self._client is None:
            self._client = BotHttpClient(get_bot_url('peter'), timeout=15)
        return self._client

    def get_active_staff(self) -> list[dict]:
        try:
            response = self.client.get('/api/staff', params={'status': 'active'})
            if response.status_code == 200:
                return response.json().get('staff', [])
            logger.warning(f"Peter staff lookup failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not fetch staff from Peter: {e}")
        return []

    def get_staff_by_email(self, email: str) -> Optional[dict]:
        try:
            response = self.client.get('/api/staff/self', params={'email': email})
            if response.status_code == 200:
                return response.json()
            logger.warning(f"Peter staff lookup for {email} failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not fetch staff {email} from Peter: {e}")
        return None

    def get_sections(self) -> list[dict]:
        try:
            response = self.client.get('/api/sections')
            if response.status_code == 200:
                return response.json().get('sections', [])
            logger.warning(f"Peter sections lookup failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not fetch sections from Peter: {e}")
        return []

    def get_reportees(self, manager_email: str, recursive: bool = True) -> list[dict]:
        try:
            params = {'email': manager_email}
            if recursive:
                params['recursive'] = 'true'
            response = self.client.get('/api/staff/reportees', params=params)
            if response.status_code == 200:
                return response.json().get('staff', [])
            logger.warning(f"Peter reportees lookup failed: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not fetch reportees from Peter: {e}")
        return []
