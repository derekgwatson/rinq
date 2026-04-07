"""Local staff directory backed by staff_extensions table."""

import logging
from typing import Optional

from rinq.integrations.base import StaffDirectory

logger = logging.getLogger(__name__)


class LocalStaffDirectory(StaffDirectory):
    """Staff directory backed by tenant's staff_extensions table."""

    def _get_db(self):
        from rinq.database.db import get_db
        return get_db()

    def _ext_to_staff(self, ext: dict) -> dict:
        """Convert a staff_extensions row to the standard staff dict format."""
        email = ext.get('email', '')
        # Derive name from email (firstname.lastname@domain)
        name_part = email.split('@')[0] if '@' in email else email
        name = name_part.replace('.', ' ').title()
        return {
            'email': email,
            'name': name,
            'extension': ext.get('extension'),
            'phone_mobile': ext.get('forward_to'),
            'reports_to': ext.get('reports_to'),
        }

    def get_active_staff(self) -> list[dict]:
        db = self._get_db()
        extensions = db.get_active_staff_extensions()
        return [self._ext_to_staff(ext) for ext in extensions]

    def get_staff_by_email(self, email: str) -> Optional[dict]:
        db = self._get_db()
        ext = db.get_staff_extension(email)
        if ext:
            return self._ext_to_staff(ext)
        return None

    def get_sections(self) -> list[dict]:
        return []

    def get_reportees(self, manager_email: str, recursive: bool = True) -> list[dict]:
        db = self._get_db()
        all_extensions = db.get_active_staff_extensions()

        manager_email_lower = manager_email.lower()

        if not recursive:
            return [
                self._ext_to_staff(ext) for ext in all_extensions
                if (ext.get('reports_to') or '').lower() == manager_email_lower
            ]

        # Recursive: walk the tree
        result = []
        seen = set()
        queue = [manager_email_lower]

        # Index by email for fast lookup
        by_manager = {}
        for ext in all_extensions:
            mgr = (ext.get('reports_to') or '').lower()
            if mgr:
                by_manager.setdefault(mgr, []).append(ext)

        while queue:
            mgr = queue.pop(0)
            for ext in by_manager.get(mgr, []):
                email = ext['email'].lower()
                if email not in seen:
                    seen.add(email)
                    result.append(self._ext_to_staff(ext))
                    queue.append(email)

        return result
