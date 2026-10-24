"""Who has left is decided by Peter, not by an admin in Tina.

For a tenant backed by Peter (integration_provider = 'watson'), every staff
extension is checked against Peter's current staff:

* **Not current in Peter** (left, or never a person in Peter) — switched off
  and held off (`is_active_locked = 1`), so auto-activation can't switch them
  back on from their old call history.
* **Current in Peter** — any hold is released. Whether they show as active
  is still decided by use (auto_activate_staff), because "active" in Tina
  means "uses the phones", not "works here": most current staff never touch
  Tina and switching them all on would put them in every picker and in
  Peter's phone list as an extension.

There used to be manual Activate/Deactivate buttons that set the same hold.
They were removed on 2026-10-24 after a current employee (Chris Savage) sat
switched off for months behind an old manual hold and couldn't be put on a
shared desk phone. Offboarding still sets the hold directly; for tenants
without Peter it is the only thing that does.

Peter answering with nobody, or with a list that would switch off a large
share of the active staff at once, is treated as Peter being broken, and
nothing is changed.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Refuse a run that would switch off more than this share of active staff.
MAX_SWITCH_OFF_SHARE = 0.25


def tenant_uses_peter(tenant) -> bool:
    return bool(tenant) and tenant.get('integration_provider') == 'watson'


def _emails_for(person: dict) -> set[str]:
    found = {person.get('google_primary_email'), person.get('work_email')}
    aliases = person.get('emails') or []
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except ValueError:
            aliases = []
    for a in aliases:
        found.add(a.get('email') if isinstance(a, dict) else a)
    return {e.lower().strip() for e in found if isinstance(e, str) and e.strip()}


def current_emails(peter_staff: list[dict]) -> set[str]:
    emails = set()
    for person in peter_staff or []:
        if str(person.get('status', 1)) != '1':
            continue
        emails |= _emails_for(person)
    return emails


def sync_from_peter(db, peter_staff: list[dict], updated_by: str = 'system:peter') -> dict:
    """Apply Peter's current staff to staff_extensions. Returns what changed."""
    current = current_emails(peter_staff)
    if not current:
        logger.error("Staff status: Peter returned no current staff - changing nothing")
        return {'skipped': 'Peter returned no current staff', 'switched_off': [], 'released': []}

    extensions = db.get_all_staff_extensions()
    switch_off = [e for e in extensions
                  if e['email'].lower() not in current
                  and (e.get('is_active') or not e.get('is_active_locked'))]
    release = [e for e in extensions
               if e['email'].lower() in current and e.get('is_active_locked')]

    active_count = sum(1 for e in extensions if e.get('is_active'))
    switching_off_active = sum(1 for e in switch_off if e.get('is_active'))
    if active_count and switching_off_active > max(3, active_count * MAX_SWITCH_OFF_SHARE):
        logger.error(f"Staff status: Peter's list would switch off {switching_off_active} of "
                     f"{active_count} active staff - changing nothing")
        return {'skipped': f'would switch off {switching_off_active} of {active_count} active staff',
                'switched_off': [], 'released': []}

    for e in switch_off:
        db.set_staff_extension_active_locked(e['email'], False, locked=True, updated_by=updated_by)
    for e in release:
        db.set_staff_extension_active_locked(e['email'], bool(e.get('is_active')), locked=False,
                                             updated_by=updated_by)

    switched_off = [e['email'] for e in switch_off]
    released = [e['email'] for e in release]
    if switched_off or released:
        parts = []
        if switched_off:
            parts.append(f"switched off (not current in Peter): {', '.join(switched_off)}")
        if released:
            parts.append(f"released (current in Peter): {', '.join(released)}")
        db.log_activity('staff_status_synced', 'peter', '; '.join(parts), updated_by)
    return {'skipped': None, 'switched_off': switched_off, 'released': released}


def sync_tenant_from_peter(db, tenant, updated_by: str = 'system:peter') -> dict | None:
    """Fetch Peter's staff and apply it, for a Peter-backed tenant only."""
    if not tenant_uses_peter(tenant):
        return None
    from rinq.integrations.watson.staff import WatsonStaffDirectory
    try:
        staff = WatsonStaffDirectory().get_active_staff()
    except Exception as e:
        logger.warning(f"Staff status: could not reach Peter: {e}")
        return {'skipped': 'could not reach Peter', 'switched_off': [], 'released': []}
    return sync_from_peter(db, staff, updated_by=updated_by)
