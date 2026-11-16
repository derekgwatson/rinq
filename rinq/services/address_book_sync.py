"""Address book sync service.

Pulls contacts from an external source and upserts them into the tenant
address_book table. Any tenant can maintain a manual address book; Watson
additionally gets automatic daily sync from Peter.

Usage (manual trigger or cron):
    from rinq.services.address_book_sync import sync_address_book
    added, updated, removed = sync_address_book(db)
"""

import logging
import re

logger = logging.getLogger(__name__)

# E.164 normalisation for Australian mobiles.
# Handles: 04xxxxxxxx, +614xxxxxxxx, 614xxxxxxxx
_AU_MOBILE_RE = re.compile(r'^(?:\+?61|0)(4\d{8})$')


def _normalise_mobile(raw: str) -> str | None:
    """Return E.164 form of an AU mobile, or None if not recognised."""
    if not raw:
        return None
    digits = re.sub(r'[\s\-\(\)]', '', raw)
    m = _AU_MOBILE_RE.match(digits)
    if m:
        return f'+61{m.group(1)}'
    # Pass through non-AU numbers that already look like E.164
    if digits.startswith('+') and len(digits) >= 8:
        return digits
    return None


# external_id suffix for a staff member's private (unlisted) mobile
PRIVATE_SUFFIX = ':private'


class AddressBookSource:
    """Abstract interface for an address book sync source."""

    source_name: str = 'unknown'

    def get_entries(self) -> list[dict]:
        """Return a list of contact dicts with keys:
            external_id (str), name (str), mobile (str),
            section (str|None), position (str|None)
        """
        raise NotImplementedError


class PeterAddressBookSource(AddressBookSource):
    """Sync source backed by Peter (Watson HR bot).

    Pulls all active staff with a mobile number. This includes internal
    staff, fitters, and reps — anyone Peter knows about with a phone.
    """

    source_name = 'peter'

    def __init__(self, staff_directory=None):
        self._directory = staff_directory
        self.keep_private = False

    @property
    def directory(self):
        if self._directory is None:
            from rinq.integrations.watson.staff import WatsonStaffDirectory
            self._directory = WatsonStaffDirectory()
        return self._directory

    def get_entries(self) -> list[dict]:
        staff_list = self.directory.get_active_staff()
        entries = []
        for person in staff_list:
            mobile = person.get('phone_mobile', '')
            if not mobile:
                continue
            name = person.get('name', '').strip()
            if not name:
                continue
            # Peter staff IDs are integers; stringify for external_id
            external_id = str(person.get('id', ''))
            if not external_id:
                continue

            # Prefer section name; fall back to position label
            section = person.get('section') or None
            position = person.get('position') or None
            email = (person.get('google_primary_email')
                     or person.get('work_email')
                     or person.get('email')
                     or None)

            entries.append({
                'external_id': external_id,
                'name': name,
                'mobile': mobile,
                'section': section,
                'position': position,
                'email': email,
            })
        return entries + self._private_entries()

    def _private_entries(self) -> list[dict]:
        """Staff private mobiles (Peter's "Private mobile"): recognised by
        number, never listed. If Peter can't be asked, keep the ones we have
        rather than prune them."""
        getter = getattr(self.directory, 'get_private_mobiles', None)
        people = getter() if getter else []
        if people is None:
            self.keep_private = True
            return []
        self.keep_private = False
        out = []
        for person in people:
            if not person.get('mobile') or not person.get('id'):
                continue
            out.append({
                'external_id': f"{person['id']}{PRIVATE_SUFFIX}",
                'name': (person.get('name') or '').strip(),
                'mobile': person['mobile'],
                'section': person.get('section') or None,
                'position': person.get('position') or None,
                'email': person.get('email') or None,
                'unlisted': True,
            })
        return out


def sync_address_book(db, source: AddressBookSource = None) -> tuple[int, int, int]:
    """Sync contacts from source into the tenant address_book table.

    Args:
        db: Tenant Database instance
        source: AddressBookSource to pull from. Defaults to PeterAddressBookSource.

    Returns:
        (added, updated, removed) counts
    """
    if source is None:
        source = PeterAddressBookSource()

    source_name = source.source_name
    logger.info(f"Address book sync starting: source={source_name}")

    try:
        entries = source.get_entries()
    except Exception as e:
        logger.error(f"Address book sync failed to fetch entries from {source_name}: {e}")
        raise

    logger.info(f"Address book sync: {len(entries)} entries from {source_name}")

    seen_external_ids = []
    added = updated = 0

    for entry in entries:
        mobile_e164 = _normalise_mobile(entry['mobile'])
        if not mobile_e164:
            logger.debug(f"Skipping {entry['name']}: unrecognised mobile {entry['mobile']!r}")
            continue

        external_id = entry['external_id']
        seen_external_ids.append(external_id)

        existing = db.get_address_book_by_source_id(source_name, external_id)
        db.upsert_address_book_entry(
            name=entry['name'],
            display_mobile=entry['mobile'],
            mobile_e164=mobile_e164,
            section=entry.get('section'),
            position=entry.get('position'),
            source=source_name,
            external_id=external_id,
            email=entry.get('email'),
            unlisted=bool(entry.get('unlisted')),
        )
        if existing:
            updated += 1
        else:
            added += 1

    if getattr(source, 'keep_private', False):
        # Peter didn't answer for private mobiles: keep the ones we have
        seen_external_ids += [e['external_id'] for e in db.get_address_book(include_unlisted=True)
                              if e.get('source') == source_name
                              and (e.get('external_id') or '').endswith(PRIVATE_SUFFIX)]

    removed = db.delete_address_book_by_source(source_name,
                                               keep_external_ids=seen_external_ids)

    logger.info(f"Address book sync complete: +{added} ~{updated} -{removed}")
    return added, updated, removed
