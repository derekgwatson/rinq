"""
Caller enrichment service for Rinq.

Looks up caller information from customer and order integrations
to provide context about who is calling.
"""

import json
import logging
from typing import Optional

from rinq.database.db import get_db

logger = logging.getLogger(__name__)


class CallerEnrichmentService:
    """Service to enrich caller data from customer and order integrations."""

    def enrich_caller(self, phone_number: str) -> dict:
        """
        Look up caller information by phone number.

        Returns:
            Dict with:
                customer_id: Clara customer ID (if found)
                customer_name: Customer name (if found)
                customer_email: Customer email (if found)
                order_data: JSON string with order info (if found)
                priority: 'high', 'medium', 'normal', or 'unknown'
                priority_reason: Why this priority was assigned
                call_history: Dict with total_calls, recent_calls, last_call_date
        """
        result = {
            'customer_id': None,
            'customer_name': None,
            'customer_email': None,
            'order_data': None,
            'priority': 'unknown',
            'priority_reason': 'No customer record found',
        }

        # Step 1: Look up call history from local database (always do this)
        # Even for unknown callers, we may have talked to them before
        call_history = self._lookup_call_history(phone_number)
        result['call_history'] = call_history

        # Step 2: Look up customer in Clara by phone
        customer = self._lookup_customer_by_phone(phone_number)

        if not customer:
            # No CRM record — check address book for a name match by mobile
            ab_match = self._lookup_address_book(phone_number)
            if ab_match:
                result['customer_name'] = ab_match['name']
                result['priority'] = 'normal'
                result['priority_reason'] = 'Known contact (address book)'
            return result

        result['customer_id'] = customer.get('id')
        result['customer_name'] = customer.get('name')
        result['customer_email'] = customer.get('primary_email') or customer.get('email')
        result['priority'] = 'normal'
        result['priority_reason'] = 'Known customer'

        # Step 3: Look up orders in Otto (if we have customer info)
        customer_email = result['customer_email']
        if customer_email:
            order_info = self._lookup_orders(customer_email, customer.get('name'))
            if order_info:
                result['order_data'] = json.dumps(order_info)
                result['priority'] = order_info.get('priority', 'normal')
                result['priority_reason'] = order_info.get('priority_reason', 'Has active orders')

        return result

    def _lookup_customer_by_phone(self, phone_number: str) -> Optional[dict]:
        """Search for a customer by phone number via customer lookup integration."""
        from rinq.integrations import get_customer_lookup
        lookup = get_customer_lookup()
        if not lookup:
            return None
        return lookup.find_by_phone(phone_number)

    def _lookup_orders(self, customer_email: str, customer_name: str = None) -> Optional[dict]:
        """Look up orders for a customer via order lookup integration."""
        from rinq.integrations import get_order_lookup
        order_lookup = get_order_lookup()
        if not order_lookup:
            return None

        try:
            orders = order_lookup.get_wip_orders()
            if not orders:
                return None

            # Filter to this customer's orders
            customer_orders = []
            for order in orders:
                order_email = order.get('customer_email', '').lower()
                order_name = order.get('customer_name', '').lower()

                if (customer_email and customer_email.lower() == order_email) or \
                   (customer_name and customer_name.lower() == order_name):
                    customer_orders.append(order)

            if not customer_orders:
                return None

            # Analyze orders for priority
            result = {
                'active_orders': len(customer_orders),
                'next_installation': None,
                'order_summary': f"{len(customer_orders)} active order(s)",
                'priority': 'medium',
                'priority_reason': 'Has active orders',
            }

            # Check for upcoming installations
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            soon = now + timedelta(hours=48)

            for order in customer_orders:
                install_date_str = order.get('installation_date') or order.get('delivery_date')
                if install_date_str:
                    try:
                        install_date = datetime.fromisoformat(install_date_str.replace('Z', '+00:00'))
                        if install_date <= soon:
                            result['priority'] = 'high'
                            result['priority_reason'] = "Installation within 48 hours"
                            result['next_installation'] = install_date_str
                            break
                        elif not result.get('next_installation'):
                            result['next_installation'] = install_date_str
                    except (ValueError, TypeError):
                        pass

            return result

        except Exception as e:
            logger.error(f"Error looking up orders: {e}")
            return None

    def _lookup_address_book(self, phone_number: str) -> dict | None:
        """Check the tenant address book for this number."""
        try:
            from rinq.services.address_book_sync import _normalise_mobile
            e164 = _normalise_mobile(phone_number)
            if not e164:
                return None
            db = get_db()
            return db.get_address_book_by_mobile(e164)
        except Exception as e:
            logger.debug(f"Address book lookup failed for {phone_number}: {e}")
            return None

    def _lookup_call_history(self, phone_number: str) -> dict:
        """Look up call history from local call_log database.

        Args:
            phone_number: Phone number to search for

        Returns:
            dict with:
                - total_calls: Total call count
                - recent_calls: List of recent calls
                - last_call_date: Date of most recent call
        """
        try:
            db = get_db()
            history = db.get_call_history_by_phone(phone_number, limit=10)
            return {
                'total_calls': history.get('total_calls', 0),
                'recent_calls': history.get('calls', []),
                'last_call_date': history.get('last_call_date'),
            }
        except Exception as e:
            logger.error(f"Error looking up call history: {e}")
            return {'total_calls': 0, 'recent_calls': [], 'last_call_date': None}


# Singleton
_enrichment_service = None


def get_enrichment_service() -> CallerEnrichmentService:
    """Get caller enrichment service singleton."""
    global _enrichment_service
    if _enrichment_service is None:
        _enrichment_service = CallerEnrichmentService()
    return _enrichment_service
