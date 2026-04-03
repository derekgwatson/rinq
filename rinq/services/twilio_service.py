"""
Twilio service for Tina.

Handles all Twilio API interactions:
- Phone number management
- Call forwarding configuration
- SIP domain and credential management
- Call recording management
"""

import logging
from datetime import datetime
from typing import Optional

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

from rinq.config import config
from rinq.database.db import get_db

logger = logging.getLogger(__name__)


class TwilioService:
    """Service for Twilio PBX operations."""

    def __init__(self):
        self._clients = {}  # Cache clients per account SID
        self._thread_account_sid = None  # Captured for background threads

    @property
    def db(self):
        """Get database for current tenant context (not cached)."""
        return get_db()

    def _get_tenant_twilio_creds(self):
        """Get Twilio creds for current tenant, falling back to thread capture, then global config."""
        try:
            from flask import g
            tenant = getattr(g, 'tenant', None)
            if tenant and tenant.get('twilio_account_sid') and tenant.get('twilio_auth_token'):
                return tenant['twilio_account_sid'], tenant['twilio_auth_token']
        except RuntimeError:
            pass
        # In background threads, use captured creds
        if self._thread_account_sid and self._thread_account_sid in self._clients:
            return self._thread_account_sid, None  # Client already cached
        return config.twilio_account_sid, config.twilio_auth_token

    def capture_for_thread(self):
        """Capture current tenant's Twilio client for use in background threads.
        Call this from request context before spawning threads."""
        account_sid, auth_token = self._get_tenant_twilio_creds()
        if account_sid and auth_token:
            if account_sid not in self._clients:
                self._clients[account_sid] = Client(account_sid, auth_token)
            self._thread_account_sid = account_sid

    @property
    def client(self) -> Client:
        """Get Twilio client for current tenant."""
        account_sid, auth_token = self._get_tenant_twilio_creds()
        if not account_sid:
            raise ValueError("Twilio credentials not configured")
        if account_sid in self._clients:
            return self._clients[account_sid]
        if not auth_token:
            raise ValueError("Twilio credentials not configured")
        self._clients[account_sid] = Client(account_sid, auth_token)
        return self._clients[account_sid]

    @property
    def is_configured(self) -> bool:
        """Check if Twilio is configured for current tenant."""
        account_sid, auth_token = self._get_tenant_twilio_creds()
        return bool(account_sid)

    # =========================================================================
    # Account Info
    # =========================================================================

    def get_account_info(self) -> dict:
        """Get Twilio account information."""
        try:
            account = self.client.api.accounts(config.twilio_account_sid).fetch()
            return {
                "sid": account.sid,
                "friendly_name": account.friendly_name,
                "status": account.status,
                "type": account.type,
                "date_created": str(account.date_created),
            }
        except TwilioRestException as e:
            logger.error(f"Failed to get account info: {e}")
            return {"error": str(e)}

    # =========================================================================
    # Phone Numbers
    # =========================================================================

    def sync_phone_numbers(self, performed_by: str = "system") -> dict:
        """Sync phone numbers from Twilio to local database.

        This will:
        - Add new numbers from Twilio
        - Update existing numbers
        - Remove numbers that no longer exist in Twilio
        """
        try:
            numbers = self.client.incoming_phone_numbers.list()
            synced_at = datetime.utcnow().isoformat()
            count = 0

            # Track which SIDs we see from Twilio
            twilio_sids = set()

            for number in numbers:
                twilio_sids.add(number.sid)
                self.db.upsert_phone_number({
                    "sid": number.sid,
                    "phone_number": number.phone_number,
                    "friendly_name": number.friendly_name,
                    "forward_to": self._extract_forward_to(number),
                    "is_active": 1,
                    "synced_at": synced_at,
                })
                count += 1

            # Remove numbers that are in our DB but no longer in Twilio
            removed = self.db.remove_phone_numbers_not_in(twilio_sids)

            self.db.log_activity(
                action="sync_phone_numbers",
                target="all",
                details=f"Synced {count} phone numbers from Twilio, removed {removed} stale numbers",
                performed_by=performed_by
            )

            logger.info(f"Synced {count} phone numbers from Twilio, removed {removed} stale")
            return {"success": True, "count": count, "removed": removed}

        except TwilioRestException as e:
            logger.error(f"Failed to sync phone numbers: {e}")
            return {"success": False, "error": str(e)}

    def _extract_forward_to(self, number) -> Optional[str]:
        """Extract the forwarding number from a Twilio number's voice URL."""
        # If using TwiML Bin, the forwarding number might be in the voice_url
        # For now, return None - will be set manually or via API
        return None

    def configure_status_callbacks(self, performed_by: str = "system") -> dict:
        """Set status callback URL on all Twilio phone numbers.

        One-time operation to ensure Twilio sends call status events to Tina.
        """
        if not config.webhook_base_url:
            return {"success": False, "error": "TINA_WEBHOOK_URL not configured"}

        status_callback = f"{config.webhook_base_url}/api/voice/call-status"
        try:
            numbers = self.client.incoming_phone_numbers.list()
            updated = 0
            for number in numbers:
                number.update(
                    status_callback=status_callback,
                    status_callback_method='POST',
                )
                updated += 1

            self.db.log_activity(
                action="configure_status_callbacks",
                target="all",
                details=f"Set status callback on {updated} phone numbers: {status_callback}",
                performed_by=performed_by
            )
            logger.info(f"Configured status callback on {updated} phone numbers")
            return {"success": True, "updated": updated}

        except TwilioRestException as e:
            logger.error(f"Failed to configure status callbacks: {e}")
            return {"success": False, "error": str(e)}

    def get_phone_numbers(self) -> list[dict]:
        """Get all phone numbers from local database."""
        return self.db.get_phone_numbers()

    def update_forwarding(self, phone_sid: str, forward_to: str, performed_by: str) -> dict:
        """Update the forwarding number for a phone number.

        Updates the local database. If TINA_WEBHOOK_URL is configured,
        also updates the Twilio phone number's voice URL to point to Tina.
        """
        try:
            # Format number to E.164 if needed
            forward_to_e164 = self._format_phone_number(forward_to)

            phone_number = self.db.get_phone_number(phone_sid)
            if not phone_number:
                return {"success": False, "error": "Phone number not found"}

            # Update Twilio's voice URL and status callback if webhook URL is configured
            if config.webhook_base_url:
                voice_url = f"{config.webhook_base_url}/api/voice/incoming"
                status_callback = f"{config.webhook_base_url}/api/voice/call-status"
                self.client.incoming_phone_numbers(phone_sid).update(
                    voice_url=voice_url,
                    status_callback=status_callback,
                    status_callback_method='POST',
                )
                logger.info(f"Set voice URL to {voice_url}, status callback to {status_callback}")

            # Update local database
            self.db.update_forward_to(phone_sid, forward_to_e164, performed_by)

            self.db.log_activity(
                action="update_forwarding",
                target=phone_number["phone_number"],
                details=f"Forwarding to {forward_to_e164}",
                performed_by=performed_by
            )

            logger.info(f"Updated forwarding for {phone_number['phone_number']} to {forward_to_e164}")
            return {"success": True, "forward_to": forward_to_e164}

        except TwilioRestException as e:
            logger.error(f"Failed to update forwarding: {e}")
            return {"success": False, "error": str(e)}

    def _format_phone_number(self, number: str) -> str:
        """Format phone number to E.164 (Australian)."""
        # Handle SIP URI format: sip:62804443@domain.com;transport=UDP
        if number.startswith('sip:'):
            # Extract the user part (before @)
            number = number[4:]  # Remove 'sip:'
            if '@' in number:
                number = number.split('@')[0]

        # Remove any non-digit characters
        digits = "".join(c for c in number if c.isdigit())

        # Already has country code
        if number.startswith("+"):
            return number

        # Handle already E.164 formatted (without +)
        if digits.startswith("61") and len(digits) == 11:
            return f"+{digits}"

        # Handle Australian mobile (04xx) - 10 digits
        if digits.startswith("04") and len(digits) == 10:
            return f"+61{digits[1:]}"

        # Handle Australian landline with area code (0x) - 10 digits
        if digits.startswith("0") and len(digits) == 10:
            return f"+61{digits[1:]}"

        # Handle 1300/1800 numbers (10 digits) and 13xx short numbers (6 digits)
        if digits.startswith("1300") or digits.startswith("1800"):
            return f"+61{digits}"
        if digits.startswith("13") and len(digits) == 6:
            return f"+61{digits}"

        # Handle 9-digit (national format without leading 0)
        if len(digits) == 9:
            return f"+61{digits}"

        # Handle 8-digit local number (no area code)
        # Assume Canberra (02) area code for local numbers
        if len(digits) == 8:
            return f"+612{digits}"

        return number

    # =========================================================================
    # Call Recording
    # =========================================================================

    def get_recording(self, recording_sid: str) -> dict:
        """Get a recording from Twilio."""
        try:
            recording = self.client.recordings(recording_sid).fetch()
            return {
                "sid": recording.sid,
                "call_sid": recording.call_sid,
                "duration": recording.duration,
                "status": recording.status,
                "uri": recording.uri,
                "media_url": f"https://api.twilio.com{recording.uri.replace('.json', '.mp3')}",
            }
        except TwilioRestException as e:
            logger.error(f"Failed to get recording {recording_sid}: {e}")
            return {"error": str(e)}

    def delete_recording(self, recording_sid: str) -> dict:
        """Delete a recording from Twilio."""
        try:
            self.client.recordings(recording_sid).delete()
            logger.info(f"Deleted recording {recording_sid} from Twilio")
            return {"success": True}
        except TwilioRestException as e:
            logger.error(f"Failed to delete recording {recording_sid}: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # SIP Domain & Credentials (for staff extensions)
    # =========================================================================

    def get_sip_domains(self) -> list[dict]:
        """Get all SIP domains."""
        try:
            domains = self.client.sip.domains.list()
            return [
                {
                    "sid": d.sid,
                    "domain_name": d.domain_name,
                    "friendly_name": d.friendly_name,
                }
                for d in domains
            ]
        except TwilioRestException as e:
            logger.error(f"Failed to get SIP domains: {e}")
            return []

    def create_sip_domain(self, domain_name: str, friendly_name: str) -> dict:
        """Create a SIP domain for the organization."""
        try:
            domain = self.client.sip.domains.create(
                domain_name=domain_name,
                friendly_name=friendly_name
            )
            logger.info(f"Created SIP domain: {domain.domain_name}")
            return {
                "success": True,
                "sid": domain.sid,
                "domain_name": domain.domain_name,
            }
        except TwilioRestException as e:
            logger.error(f"Failed to create SIP domain: {e}")
            return {"success": False, "error": str(e)}

    def _extract_credential_list_sid(self, mapping) -> str:
        """Extract the credential list SID from a mapping object.

        The Twilio SDK may store this under different attribute names depending
        on the SDK version. We try multiple approaches.
        """
        # Try direct attribute
        if hasattr(mapping, 'credential_list_sid'):
            return mapping.credential_list_sid

        # Try _properties dict (common in Twilio SDK)
        if hasattr(mapping, '_properties'):
            props = mapping._properties
            if 'credential_list_sid' in props:
                return props['credential_list_sid']

        # The mapping SID itself might be the credential list SID reference
        # (based on how Twilio structures some of these resources)
        return mapping.sid

    def get_domain_credential_list_mappings(self, domain_sid: str) -> list[dict]:
        """Get credential lists mapped to a SIP domain for authentication."""
        try:
            mappings = self.client.sip.domains(domain_sid).auth.calls.credential_list_mappings.list()
            result = []
            for m in mappings:
                result.append({
                    "sid": m.sid,
                    "credential_list_sid": self._extract_credential_list_sid(m),
                    "friendly_name": getattr(m, 'friendly_name', None),
                })
            return result
        except TwilioRestException as e:
            logger.error(f"Failed to get credential list mappings: {e}")
            return []
        except Exception as e:
            logger.error(f"Error in credential list mappings: {e}")
            return []

    def get_domain_registration_credential_list_mappings(self, domain_sid: str) -> list[dict]:
        """Get credential lists mapped to a SIP domain for REGISTRATION authentication."""
        try:
            mappings = self.client.sip.domains(domain_sid).auth.registrations.credential_list_mappings.list()
            result = []
            for m in mappings:
                result.append({
                    "sid": m.sid,
                    "credential_list_sid": self._extract_credential_list_sid(m),
                    "friendly_name": getattr(m, 'friendly_name', None),
                })
            return result
        except TwilioRestException as e:
            logger.error(f"Failed to get registration credential list mappings: {e}")
            return []
        except Exception as e:
            logger.error(f"Error in registration credential list mappings: {e}")
            return []

    def associate_credential_list_with_domain(self, domain_sid: str, credential_list_sid: str) -> dict:
        """Associate a credential list with a SIP domain for authentication.

        Links the credential list for BOTH calls and registrations, which is
        required for SIP phones to register and make calls.
        """
        results = {"success": True, "calls": None, "registrations": None}

        # Link for CALLS authentication
        try:
            existing = self.get_domain_credential_list_mappings(domain_sid)
            already_mapped = any(m["credential_list_sid"] == credential_list_sid for m in existing)

            if already_mapped:
                results["calls"] = "already_linked"
            else:
                self.client.sip.domains(domain_sid).auth.calls.credential_list_mappings.create(
                    credential_list_sid=credential_list_sid
                )
                results["calls"] = "linked"
                logger.info(f"Linked credential list {credential_list_sid} to domain {domain_sid} for CALLS")
        except TwilioRestException as e:
            logger.error(f"Failed to link credential list for calls: {e}")
            results["calls"] = f"error: {e}"
            results["success"] = False

        # Link for REGISTRATIONS authentication (required for SIP phones to register)
        try:
            existing = self.get_domain_registration_credential_list_mappings(domain_sid)
            already_mapped = any(m["credential_list_sid"] == credential_list_sid for m in existing)

            if already_mapped:
                results["registrations"] = "already_linked"
            else:
                self.client.sip.domains(domain_sid).auth.registrations.credential_list_mappings.create(
                    credential_list_sid=credential_list_sid
                )
                results["registrations"] = "linked"
                logger.info(f"Linked credential list {credential_list_sid} to domain {domain_sid} for REGISTRATIONS")
        except TwilioRestException as e:
            logger.error(f"Failed to link credential list for registrations: {e}")
            results["registrations"] = f"error: {e}"
            results["success"] = False

        return results

    def check_sip_domain_setup(self) -> dict:
        """Check if SIP domain and credential list are properly configured.

        Returns status dict with:
        - has_domain: bool
        - has_credential_list: bool
        - calls_linked: bool
        - registrations_linked: bool
        - domain_name: str (if exists)
        - issues: list of setup issues
        """
        result = {
            "has_domain": False,
            "has_credential_list": False,
            "calls_linked": False,
            "registrations_linked": False,
            "is_linked": False,  # True only if BOTH are linked
            "domain_name": None,
            "domain_sid": None,
            "issues": [],
        }

        # Check for SIP domain
        domains = self.get_sip_domains()
        if domains:
            result["has_domain"] = True
            result["domain_name"] = domains[0]["domain_name"]
            result["domain_sid"] = domains[0]["sid"]
        else:
            result["issues"].append("No SIP domain found. Create one in Twilio Console.")

        # Check for credential list
        if config.sip_credential_list_sid:
            result["has_credential_list"] = True
        else:
            result["issues"].append("TWILIO_SIP_CREDENTIAL_LIST_SID not configured in .env")

        # Check if linked for both calls and registrations
        if result["has_domain"] and result["has_credential_list"]:
            # Check calls
            calls_mappings = self.get_domain_credential_list_mappings(result["domain_sid"])
            for mapping in calls_mappings:
                if mapping["credential_list_sid"] == config.sip_credential_list_sid:
                    result["calls_linked"] = True
                    break

            # Check registrations
            reg_mappings = self.get_domain_registration_credential_list_mappings(result["domain_sid"])
            for mapping in reg_mappings:
                if mapping["credential_list_sid"] == config.sip_credential_list_sid:
                    result["registrations_linked"] = True
                    break

            # Both must be linked for SIP phones to work
            result["is_linked"] = result["calls_linked"] and result["registrations_linked"]

            if not result["calls_linked"]:
                result["issues"].append("Credential list not linked for CALLS (outbound calls won't authenticate)")
            if not result["registrations_linked"]:
                result["issues"].append("Credential list not linked for REGISTRATIONS (SIP phones can't register - causes 403)")

        return result

    def get_credential_lists(self) -> list[dict]:
        """Get all credential lists."""
        try:
            cred_lists = self.client.sip.credential_lists.list()
            return [
                {
                    "sid": cl.sid,
                    "friendly_name": cl.friendly_name,
                }
                for cl in cred_lists
            ]
        except TwilioRestException as e:
            logger.error(f"Failed to get credential lists: {e}")
            return []

    def get_credentials_in_list(self, credential_list_sid: str = None) -> list[dict]:
        """Get all credentials in a credential list.

        Args:
            credential_list_sid: The SID of the credential list. If not provided,
                                uses the configured default from TWILIO_SIP_CREDENTIAL_LIST_SID.
        """
        cred_list_sid = credential_list_sid or config.sip_credential_list_sid
        if not cred_list_sid:
            logger.warning("No credential list SID configured")
            return []

        try:
            credentials = self.client.sip.credential_lists(cred_list_sid).credentials.list()
            return [
                {
                    "sid": c.sid,
                    "username": c.username,
                    "date_created": str(c.date_created) if c.date_created else None,
                    "date_updated": str(c.date_updated) if c.date_updated else None,
                }
                for c in credentials
            ]
        except TwilioRestException as e:
            logger.error(f"Failed to get credentials: {e}")
            return []

    def sync_credentials(self, performed_by: str = "system") -> dict:
        """Sync SIP credentials from Twilio to local database.

        This will:
        - Add new credentials from Twilio
        - Update existing credentials
        - Mark credentials that no longer exist in Twilio as inactive
        """
        if not config.sip_credential_list_sid:
            return {"success": False, "error": "No credential list SID configured"}

        try:
            twilio_creds = self.get_credentials_in_list()
            synced_at = datetime.utcnow().isoformat()
            count = 0
            added = 0
            updated = 0

            # Track which SIDs we see from Twilio
            twilio_sids = set()

            for cred in twilio_creds:
                twilio_sids.add(cred["sid"])

                # Check if exists locally
                existing = self.db.get_user(cred["sid"])

                self.db.upsert_user({
                    "sid": cred["sid"],
                    "username": cred["username"],
                    "friendly_name": existing["friendly_name"] if existing else cred["username"],
                    "staff_email": existing["staff_email"] if existing else None,
                    "is_active": 1,
                    "synced_at": synced_at,
                })

                if existing:
                    updated += 1
                else:
                    added += 1
                count += 1

            # Mark credentials not in Twilio as inactive
            deactivated = self.db.deactivate_users_not_in(twilio_sids)

            self.db.log_activity(
                action="sync_credentials",
                target="all",
                details=f"Synced {count} credentials ({added} added, {updated} updated, {deactivated} deactivated)",
                performed_by=performed_by
            )

            logger.info(f"Synced {count} credentials from Twilio")
            return {
                "success": True,
                "count": count,
                "added": added,
                "updated": updated,
                "deactivated": deactivated
            }

        except TwilioRestException as e:
            logger.error(f"Failed to sync credentials: {e}")
            return {"success": False, "error": str(e)}

    def create_user_credential(
        self,
        credential_list_sid: str,
        username: str,
        password: str,
        friendly_name: str = None,
        staff_email: str = None
    ) -> dict:
        """Create a SIP credential for a user (for onboarding)."""
        try:
            cred = self.client.sip.credential_lists(credential_list_sid).credentials.create(
                username=username,
                password=password
            )
            logger.info(f"Created SIP credential for {username}")

            # Store in local database
            self.db.upsert_user({
                "sid": cred.sid,
                "username": username,
                "friendly_name": friendly_name or username,
                "staff_email": staff_email,
                "is_active": 1,
                "synced_at": datetime.utcnow().isoformat(),
            })

            # Store password locally for later retrieval
            self.db.update_user_password(cred.sid, password)

            return {
                "success": True,
                "sid": cred.sid,
                "username": username,
            }
        except TwilioRestException as e:
            logger.error(f"Failed to create SIP credential: {e}")
            return {"success": False, "error": str(e)}

    def update_user_credential_password(
        self,
        credential_list_sid: str,
        credential_sid: str,
        new_password: str
    ) -> dict:
        """Update a SIP credential's password."""
        try:
            self.client.sip.credential_lists(credential_list_sid).credentials(credential_sid).update(
                password=new_password
            )
            logger.info(f"Updated password for SIP credential {credential_sid}")

            # Store password locally for later retrieval
            self.db.update_user_password(credential_sid, new_password)

            return {"success": True}
        except TwilioRestException as e:
            logger.error(f"Failed to update SIP credential password: {e}")
            return {"success": False, "error": str(e)}

    def delete_user_credential(self, credential_list_sid: str, credential_sid: str) -> dict:
        """Delete a SIP credential (for offboarding)."""
        try:
            self.client.sip.credential_lists(credential_list_sid).credentials(credential_sid).delete()
            logger.info(f"Deleted SIP credential {credential_sid}")
            return {"success": True}
        except TwilioRestException as e:
            logger.error(f"Failed to delete SIP credential: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Verified Caller IDs (Outgoing Caller IDs)
    # =========================================================================

    def get_outgoing_caller_ids(self) -> list[dict]:
        """Get all verified outgoing caller IDs from Twilio.

        These are external numbers verified in Twilio that can be used
        as outbound caller ID even though they're not owned/ported.
        """
        try:
            caller_ids = self.client.outgoing_caller_ids.list()
            return [
                {
                    "sid": cid.sid,
                    "phone_number": cid.phone_number,
                    "friendly_name": cid.friendly_name,
                }
                for cid in caller_ids
            ]
        except TwilioRestException as e:
            logger.error(f"Failed to get outgoing caller IDs: {e}")
            return []

    def sync_verified_caller_ids(self, performed_by: str = "system") -> dict:
        """Sync verified caller IDs from Twilio to local database.

        This will:
        - Add new verified caller IDs from Twilio
        - Keep existing local data (name, section) for numbers still in Twilio
        - Mark numbers no longer in Twilio as inactive
        """
        try:
            twilio_caller_ids = self.get_outgoing_caller_ids()
            synced_at = datetime.utcnow().isoformat()
            added = 0
            updated = 0

            # Track which phone numbers we see from Twilio
            twilio_numbers = set()

            for cid in twilio_caller_ids:
                phone_number = cid["phone_number"]
                twilio_numbers.add(phone_number)

                # Check if exists locally
                existing = self.db.get_verified_caller_id(phone_number)

                if existing:
                    # Update - keep local name/section but mark as active
                    self.db.update_verified_caller_id(
                        phone_number,
                        is_active=True,
                        updated_by=performed_by
                    )
                    updated += 1
                else:
                    # Add new - use Twilio's friendly_name as initial name
                    self.db.add_verified_caller_id(
                        phone_number=phone_number,
                        friendly_name=cid.get("friendly_name"),
                        section=None,
                        notes="Synced from Twilio",
                        created_by=performed_by
                    )
                    added += 1

            # Mark numbers not in Twilio as inactive
            deactivated = self.db.deactivate_verified_caller_ids_not_in(twilio_numbers)

            self.db.log_activity(
                action="sync_verified_caller_ids",
                target="all",
                details=f"Synced {len(twilio_caller_ids)} verified caller IDs ({added} added, {updated} updated, {deactivated} deactivated)",
                performed_by=performed_by
            )

            logger.info(f"Synced {len(twilio_caller_ids)} verified caller IDs from Twilio")
            return {
                "success": True,
                "count": len(twilio_caller_ids),
                "added": added,
                "updated": updated,
                "deactivated": deactivated
            }

        except TwilioRestException as e:
            logger.error(f"Failed to sync verified caller IDs: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Test Call (for verification)
    # =========================================================================

    def make_test_call(self, from_number: str, to_number: str) -> dict:
        """Make a test call between two numbers.

        For trial accounts, both numbers must be verified.
        """
        try:
            to_e164 = self._format_phone_number(to_number)

            call = self.client.calls.create(
                to=to_e164,
                from_=from_number,
                twiml='<Response><Say>Hello! This is a test call from Tina. Your phone system is working correctly.</Say></Response>'
            )

            logger.info(f"Initiated test call: {call.sid}")
            return {
                "success": True,
                "call_sid": call.sid,
                "status": call.status,
            }
        except TwilioRestException as e:
            logger.error(f"Failed to make test call: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Queue Management
    # =========================================================================

    def get_queue_by_name(self, queue_name: str):
        """Get a Twilio queue by its friendly name.

        Returns the queue object or None if not found.
        """
        try:
            queues = self.client.queues.list(friendly_name=queue_name)
            if queues:
                return queues[0]
            return None
        except TwilioRestException as e:
            logger.error(f"Failed to get queue {queue_name}: {e}")
            return None

    def initiate_call(self, to: str, from_number: str, url: str) -> dict:
        """Initiate an outbound call that fetches TwiML from a URL.

        Args:
            to: The phone number to call
            from_number: The caller ID to display
            url: URL that returns TwiML when the call is answered

        Returns:
            Dict with call_sid and status, or error
        """
        try:
            to_e164 = self._format_phone_number(to)
            from_e164 = self._format_phone_number(from_number)

            call = self.client.calls.create(
                to=to_e164,
                from_=from_e164,
                url=url
            )

            logger.info(f"Initiated outbound call to {to}: {call.sid}")
            return {
                "success": True,
                "call_sid": call.sid,
                "status": call.status,
            }
        except TwilioRestException as e:
            logger.error(f"Failed to initiate call to {to}: {e}")
            return {"success": False, "error": str(e)}


    # =========================================================================
    # Active Call Queries
    # =========================================================================

    def list_in_progress_calls(self) -> list[dict]:
        """Get all currently in-progress calls from Twilio.

        Returns a list of dicts with call_sid, from_number, to_number, direction,
        start_time for each active call.
        """
        try:
            calls = self.client.calls.list(status='in-progress', limit=100)
            result = []
            for call in calls:
                result.append({
                    'call_sid': call.sid,
                    'from_number': call.from_formatted or call.from_,
                    'to_number': call.to_formatted or call.to,
                    'direction': call.direction,
                    'start_time': call.start_time.isoformat() if call.start_time else None,
                })
            return result
        except TwilioRestException as e:
            logger.error(f"Failed to list active calls from Twilio: {e}")
            return []


# Singleton instance
_service = None


def get_twilio_service() -> TwilioService:
    """Get Twilio service singleton."""
    global _service
    if _service is None:
        _service = TwilioService()
    return _service
