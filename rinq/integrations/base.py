"""
Abstract base classes for Rinq integrations.

Each interface defines the contract that implementations must fulfill.
All methods return dicts or lists — no implementation-specific types leak out.
"""

from abc import ABC, abstractmethod
from typing import Optional


class StaffDirectory(ABC):
    """Interface for looking up staff/employee information."""

    @abstractmethod
    def get_active_staff(self) -> list[dict]:
        """Get all active staff members.

        Returns:
            List of dicts with at minimum:
                email: str
                name: str
            Optional fields (if available):
                extension: str
                phone_mobile: str
                section: str
        """

    @abstractmethod
    def get_staff_by_email(self, email: str) -> Optional[dict]:
        """Get a single staff member by email.

        Returns:
            Dict with staff info, or None if not found.
        """

    @abstractmethod
    def get_sections(self) -> list[dict]:
        """Get organizational sections/departments.

        Returns:
            List of dicts with at minimum:
                name: str
        """

    @abstractmethod
    def get_reportees(self, manager_email: str, recursive: bool = True) -> list[dict]:
        """Get staff who report to a manager.

        Args:
            manager_email: The manager's email address
            recursive: Include indirect reports

        Returns:
            List of staff dicts (same format as get_active_staff).
        """


class TicketService(ABC):
    """Interface for creating and managing support tickets."""

    @abstractmethod
    def create_ticket(self, subject: str, description: str,
                      priority: str = 'normal', ticket_type: str = 'task',
                      tags: list[str] = None,
                      requester_email: str = None,
                      requester_name: str = None,
                      group_id: str = None,
                      attachments: list[dict] = None) -> Optional[dict]:
        """Create a support ticket.

        Args:
            subject: Ticket subject line
            description: Full ticket description
            priority: 'low', 'normal', 'high', 'urgent'
            ticket_type: 'question', 'incident', 'problem', 'task'
            tags: List of tag strings
            requester_email: Email of the requester
            requester_name: Display name of the requester
            group_id: Assign to a specific group/team
            attachments: List of dicts with 'filename', 'content_type', 'content_base64'

        Returns:
            Dict with 'id' (ticket ID) on success, None on failure.
        """

    @abstractmethod
    def add_comment(self, ticket_id: str, body: str,
                    public: bool = False) -> bool:
        """Add a comment to an existing ticket.

        Returns:
            True on success, False on failure.
        """

    @abstractmethod
    def get_groups(self) -> list[dict]:
        """Get available ticket groups/teams.

        Returns:
            List of dicts with 'id' and 'name'.
        """


class PermissionService(ABC):
    """Interface for managing user permissions/roles."""

    @abstractmethod
    def get_permissions(self, bot: str) -> list[dict]:
        """Get all permissions for a bot/app.

        Returns:
            List of dicts with 'email' and 'role'.
        """

    @abstractmethod
    def add_permission(self, email: str, bot: str, role: str,
                       granted_by: str) -> bool:
        """Grant a role to a user.

        Returns:
            True on success, False on failure.
        """

    @abstractmethod
    def remove_permission(self, email: str, bot: str,
                          revoked_by: str) -> bool:
        """Remove a user's role.

        Returns:
            True on success, False on failure.
        """


class CustomerLookup(ABC):
    """Interface for looking up customer information."""

    @abstractmethod
    def find_by_phone(self, phone_number: str) -> Optional[dict]:
        """Find a customer by phone number.

        Args:
            phone_number: Phone number to search (any format)

        Returns:
            Dict with 'id', 'name', 'email' on success, None if not found.
        """


class OrderLookup(ABC):
    """Interface for looking up order information."""

    @abstractmethod
    def get_wip_orders(self) -> list[dict]:
        """Get all work-in-progress orders.

        Returns:
            List of order dicts with at minimum:
                customer_email: str
                customer_name: str
            Optional:
                installation_date: str (ISO format)
                delivery_date: str (ISO format)
        """


class EmailService(ABC):
    """Interface for sending emails."""

    @abstractmethod
    def send_email(self, to: str, subject: str, text_body: str,
                   attachments: list[dict] = None,
                   metadata: dict = None) -> Optional[str]:
        """Send an email.

        Args:
            to: Recipient email address
            subject: Email subject
            text_body: Plain text body
            attachments: List of dicts with 'filename', 'content_type', 'content_base64'
            metadata: Optional metadata dict

        Returns:
            Message ID on success, None on failure.
        """


class AIReceptionist(ABC):
    """Interface for AI receptionist integration."""

    @abstractmethod
    def notify_call_ended(self, call_sid: str, call_status: str) -> bool:
        """Notify the AI receptionist that a call has ended.

        Returns:
            True on success, False on failure.
        """

    @abstractmethod
    def get_answer_url(self) -> Optional[str]:
        """Get the webhook URL for the AI receptionist to answer calls.

        Returns:
            URL string, or None if not configured.
        """
