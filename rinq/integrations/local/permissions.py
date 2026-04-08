"""Local permission service — stores roles in the tenant DB."""

import logging
from rinq.database.db import get_db
from rinq.integrations.base import PermissionService

logger = logging.getLogger(__name__)


class LocalPermissionService(PermissionService):

    def get_permissions(self, bot: str) -> list[dict]:
        db = get_db()
        return db.get_permissions()

    def add_permission(self, email: str, bot: str, role: str,
                       granted_by: str) -> bool:
        db = get_db()
        return db.set_permission(email, role, granted_by)

    def remove_permission(self, email: str, bot: str,
                          revoked_by: str) -> bool:
        db = get_db()
        return db.remove_permission(email)
