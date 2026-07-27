"""Staff offboarding — revoke a departing person's phone-system access.

Called by Olive (the bot-team's offboarding orchestrator) as one step in a
larger workflow that also suspends Google, Buz, and ticketing access. Olive's
steps are idempotent and individually retryable, so **every step here must be
safe to run twice**: an already-completed step reports 'already_clear', not an
error, and a partial failure never blocks the remaining steps.

Rinq does not know Olive exists. This is a plain endpoint any orchestrator (or
a human on the admin page) can drive.

Why this iterates tenants rather than using the ambient one: the caller is an
API key, not a session, and `resolve_tenant()` only resolves from a session or
from phone numbers on /api/voice|sip paths. `g.tenant` is None for this
request, so touching `get_db()` directly would raise (gotcha 35). Iterating is
also the semantically correct thing — offboarding means "remove this person
everywhere", and a person can hold credentials in more than one tenant.
"""

import logging

from rinq.database.db import get_db
from rinq.database.master import get_master_db
from rinq.services.twilio_service import get_twilio_service
from rinq.tenant.context import get_twilio_config, iter_tenant_contexts

logger = logging.getLogger(__name__)

DONE = 'done'
ALREADY_CLEAR = 'already_clear'
FAILED = 'failed'


def _step(name, status, detail=None):
    return {'step': name, 'status': status, 'detail': detail}


def _revoke_sip_credential(email, performed_by, db):
    """Delete the Twilio SIP credential — the only step that truly cuts access.

    SIP credentials authenticate REGISTER and outbound calls via a Twilio
    credential list, entirely independently of Google. Deactivating the local
    row alone leaves a departed person able to register a softphone and place
    calls billed to the tenant, so the credential must be deleted at Twilio.

    The local row is only removed once Twilio confirms, otherwise a failure
    here would orphan a live credential with no local record of it.
    """
    user = db.get_user_by_email(email)
    if not user:
        return _step('sip_credential', ALREADY_CLEAR, 'No SIP credential')

    cred_list_sid = get_twilio_config('twilio_sip_credential_list_sid')
    if not cred_list_sid:
        return _step('sip_credential', FAILED,
                     'No SIP credential list configured for this tenant')

    result = get_twilio_service().delete_user_credential(
        credential_list_sid=cred_list_sid,
        credential_sid=user['sid'],
    )
    if not result.get('success'):
        return _step('sip_credential', FAILED, result.get('error'))

    db.delete_user(user['sid'])
    if result.get('already_absent'):
        return _step('sip_credential', DONE,
                     f"Removed stale local record of {user['username']} "
                     f"(credential already absent from Twilio)")
    return _step('sip_credential', DONE, f"Deleted credential {user['username']}")


def _deactivate_extension(email, performed_by, db):
    """Deactivate the extension and drop any mobile forward.

    is_active_locked=1 is essential: auto_activate_staff() reactivates anyone
    with usage signals (call history counts), so without the lock a departed
    person is silently switched back on.

    forward_to is cleared because it points at the person's personal mobile —
    leaving it set forwards company calls to someone who has left.
    """
    ext = db.get_staff_extension(email)
    if not ext:
        return _step('extension', ALREADY_CLEAR, 'No extension')

    had_forward = bool(ext.get('forward_to'))
    if not ext.get('is_active') and ext.get('is_active_locked') and not had_forward:
        return _step('extension', ALREADY_CLEAR,
                     f"Extension {ext.get('extension')} already deactivated")

    db.set_staff_extension_active_locked(
        email=email, is_active=False, locked=True, updated_by=performed_by,
    )
    if had_forward:
        db.clear_staff_extension_forwarding(email, updated_by=performed_by)

    detail = f"Deactivated extension {ext.get('extension')}"
    if had_forward:
        detail += f" and cleared mobile forward to {ext['forward_to']}"
    return _step('extension', DONE, detail)


def _remove_queue_memberships(email, performed_by, db):
    """Stop the queues ringing a phone that can no longer answer.

    Removes dormant rows too, not just active ones — an is_active=0 membership
    left behind would come back the moment someone reactivated it.
    """
    removed = db.remove_user_from_all_queues(email)
    if not removed:
        return _step('queue_memberships', ALREADY_CLEAR, 'Not a member of any queue')
    return _step('queue_memberships', DONE, f"Removed from: {', '.join(removed)}")


def _remove_queue_manager_roles(email, performed_by, db):
    """Drop queue-pause rights (queue_managers is separate from membership)."""
    removed = db.remove_user_from_all_queue_manager_roles(email)
    if not removed:
        return _step('queue_manager_roles', ALREADY_CLEAR, 'Not a manager of any queue')
    return _step('queue_manager_roles', DONE,
                 f"Removed as manager of: {', '.join(removed)}")


def _remove_phone_assignments(email, performed_by, db):
    """Revoke rights to answer on / call from specific numbers."""
    assignments = db.get_assignments_for_user(email)
    if not assignments:
        return _step('phone_assignments', ALREADY_CLEAR, 'No phone assignments')

    for assignment in assignments:
        db.remove_assignment(assignment['id'])
    return _step('phone_assignments', DONE,
                 f"Removed {len(assignments)} phone assignment(s)")


TENANT_STEPS = (
    _revoke_sip_credential,
    _deactivate_extension,
    _remove_queue_memberships,
    _remove_queue_manager_roles,
    _remove_phone_assignments,
)


def _has_any_trace(email, db):
    """Whether this tenant holds anything belonging to the person."""
    return bool(
        db.get_user_by_email(email)
        or db.get_staff_extension(email)
        or db.count_queue_links_for_user(email)
        or db.get_assignments_for_user(email)
    )


def preview_offboard_staff(email: str) -> dict:
    """Report what offboarding would remove, changing nothing.

    Worth running before the real thing on anyone whose departure is in doubt
    — the SIP credential deletion is irreversible (re-onboarding mints a new
    password that every one of their devices then needs).
    """
    email = (email or '').strip().lower()
    if not email:
        raise ValueError('email is required')

    preview = {'email': email, 'dry_run': True, 'tenants': {},
               'tenant_access': [], 'orphaned_reports': {}}

    for tenant in iter_tenant_contexts():
        db = get_db()
        if not _has_any_trace(email, db):
            continue

        user = db.get_user_by_email(email)
        ext = db.get_staff_extension(email)
        found = {
            'sip_credential': user['username'] if user else None,
            'extension': ext.get('extension') if ext else None,
            'mobile_forward': ext.get('forward_to') if ext else None,
            'queue_links': db.count_queue_links_for_user(email),
            'active_queue_memberships': [q['name'] for q in db.get_queues_for_user(email)],
            'phone_assignments': len(db.get_assignments_for_user(email)),
        }
        preview['tenants'][tenant['id']] = found

        reports = [r['email'] for r in db.get_staff_reporting_to(email)]
        if reports:
            preview['orphaned_reports'][tenant['id']] = reports

    master_db = get_master_db()
    master_user = master_db.get_user_by_email(email)
    if master_user:
        preview['tenant_access'] = [
            {'tenant': t['id'],
             'role': master_db.get_user_role_in_tenant(master_user['id'], t['id'])}
            for t in master_db.get_user_tenants(master_user['id'])
        ]

    preview['success'] = True
    return preview


def offboard_staff(email: str, performed_by: str) -> dict:
    """Revoke a person's Rinq access across every tenant they appear in.

    Idempotent — safe to call repeatedly. Steps that were already done report
    'already_clear'. A step that fails is recorded and the rest still run, so a
    retry picks up only what's outstanding.

    Call history is deliberately left intact: reporting needs it, and it names
    no live access.

    Returns a dict with per-tenant step results, plus `orphaned_reports`
    naming staff whose `reports_to` pointed at the departing person — those
    need a human to reassign, so they are reported rather than guessed at.
    """
    email = (email or '').strip().lower()
    if not email:
        raise ValueError('email is required')

    result = {
        'email': email,
        'tenants': {},
        'tenant_access_revoked': [],
        'orphaned_reports': {},
        'errors': [],
    }

    for tenant in iter_tenant_contexts():
        tenant_id = tenant['id']
        try:
            db = get_db()
            if not _has_any_trace(email, db):
                continue

            steps = []
            for step_fn in TENANT_STEPS:
                try:
                    steps.append(step_fn(email, performed_by, db))
                except Exception as e:
                    # One failed step must not abandon the others — a retry
                    # would then never reach them either.
                    logger.exception(
                        f"Offboarding step {step_fn.__name__} failed for "
                        f"{email} in tenant {tenant_id}: {e}")
                    steps.append(_step(step_fn.__name__.strip('_'), FAILED, str(e)))

            reports = [r['email'] for r in db.get_staff_reporting_to(email)]
            if reports:
                result['orphaned_reports'][tenant_id] = reports

            result['tenants'][tenant_id] = steps

            failures = [s for s in steps if s['status'] == FAILED]
            for failure in failures:
                result['errors'].append(f"{tenant_id}/{failure['step']}: {failure['detail']}")

            db.log_activity(
                action='offboard_staff',
                target=email,
                details='; '.join(f"{s['step']}={s['status']}" for s in steps),
                performed_by=performed_by,
            )
        except Exception as e:
            logger.exception(f"Offboarding failed for {email} in tenant {tenant_id}: {e}")
            result['errors'].append(f"{tenant_id}: {e}")

    # Tenant access lives in the master DB and needs no tenant context.
    try:
        master_db = get_master_db()
        master_user = master_db.get_user_by_email(email)
        if master_user:
            for tenant in master_db.get_user_tenants(master_user['id']):
                if master_db.remove_user_from_tenant(tenant['id'], master_user['id']):
                    result['tenant_access_revoked'].append(tenant['id'])
    except Exception as e:
        logger.exception(f"Failed to revoke tenant access for {email}: {e}")
        result['errors'].append(f"master: {e}")

    result['success'] = not result['errors']
    logger.info(
        f"Offboarded {email}: tenants={list(result['tenants'])}, "
        f"access_revoked={result['tenant_access_revoked']}, "
        f"errors={len(result['errors'])}")
    return result
