"""Who may call the JSON API: membership, admin-only endpoints, offboarding
scope and recordings scope.

- A session only counts while the person is still a member of the tenant.
- Admin/system endpoints take an admin session, the bot key, or a local
  (unix-socket) caller - never a plain staff session.
- Offboarding from a session stays inside that session's tenant; the bot key
  (Olive) still removes the person everywhere.
- The recordings API follows the same reports_to scope as the Recordings page.
"""
from unittest.mock import MagicMock

import pytest

from rinq.database.master import get_master_db

XFF = {'X-Forwarded-For': '203.0.113.9'}   # nginx always adds this
BOT_KEY = 'the-real-bot-key'

ADMIN = 'admin@example.com'
ALICE = 'alice@example.com'      # plain user, Bob reports to her
BOB = 'bob@example.com'
CAROL = 'carol@example.com'      # nobody's reportee


@pytest.fixture
def client(app):
    """The test client, but each request works out the user afresh.

    make_store keeps an app context pushed, so Flask reuses one `g` across
    requests here and get_current_user()'s per-request cache would carry over
    from one request to the next. In production every request gets its own.
    """
    from flask import g, has_app_context
    from flask.testing import FlaskClient

    class FreshUserClient(FlaskClient):
        def open(self, *args, **kwargs):
            if has_app_context():
                g.pop('_current_user', None)
            return super().open(*args, **kwargs)

    return FreshUserClient(app, app.response_class, use_cookies=True)


@pytest.fixture(autouse=True)
def quiet_outside(monkeypatch):
    """Nothing below may reach Twilio, Drive or Peter."""
    monkeypatch.setenv('BOT_API_KEY', BOT_KEY)
    fake = MagicMock()
    import rinq.services.twilio_service as ts
    import rinq.services.offboarding as off
    monkeypatch.setattr(ts, 'get_twilio_service', lambda: fake)
    monkeypatch.setattr(off, 'get_twilio_service', lambda: fake)
    from rinq.services.drive_service import drive_service
    monkeypatch.setattr(drive_service, 'purge_old_recordings',
                        lambda days: {'success': True, 'deleted': 0})


def _member(email, tenant_id='watson', role='user'):
    master = get_master_db()
    user = master.get_or_create_user(email, email.split('@')[0])
    master.add_user_to_tenant(tenant_id, user['id'], role=role)
    return user['id']


def _login(client, email, tenant_id='watson', role='user'):
    user_id = _member(email, tenant_id, role)
    with client.session_transaction() as s:
        s['user_id'], s['user_email'], s['tenant_id'] = user_id, email, tenant_id
    return user_id


# =============================================================================
# A. A session counts only while the person is still a member
# =============================================================================

def test_removed_member_session_is_refused(client, make_store):
    make_store()
    user_id = _login(client, ALICE)
    assert client.get('/api/dnd', headers=XFF).status_code == 200
    get_master_db().remove_user_from_tenant('watson', user_id)
    assert client.get('/api/dnd', headers=XFF).status_code == 403


def test_session_with_no_membership_is_refused(client, make_store):
    make_store()
    with client.session_transaction() as s:
        s['user_id'], s['user_email'], s['tenant_id'] = 999, 'stranger@example.com', 'watson'
    assert client.get('/api/dnd', headers=XFF).status_code == 403


def test_wrong_bot_key_refused_right_one_accepted(client, make_store):
    make_store()
    bad = client.post('/api/queue/cleanup', headers={**XFF, 'X-API-Key': 'nope'})
    assert bad.status_code == 403
    good = client.post('/api/queue/cleanup', headers={**XFF, 'X-API-Key': BOT_KEY})
    assert good.status_code == 200


# =============================================================================
# B. Admin/system endpoints: admin session, bot key or local caller only
# =============================================================================

ADMIN_ONLY = [
    ('POST', '/api/users', {'credential_list_sid': 'CL1', 'username': 'x', 'password': 'y'}),
    ('DELETE', '/api/users/CL1/CR1', None),
    ('GET', '/api/sip-domains', None),
    ('GET', '/api/credential-lists', None),
    ('POST', '/api/phone-numbers/sync', None),
    ('POST', '/api/phone-numbers/configure-status-callbacks', None),
    ('POST', '/api/phone-numbers/PNwatson/forward', {'forward_to': '+61400000000'}),
    ('POST', '/api/test-call', {'from_number': '+61244180000', 'to_number': '+61400000000'}),
    ('POST', '/api/staff/offboard', {'email': BOB}),
    ('POST', '/api/staff/offboard', {'email': BOB, 'dry_run': True}),
    ('POST', '/api/staff/sync', None),
    ('POST', '/api/staff/import-hierarchy', None),
    ('POST', '/api/address-book', {'name': 'Someone', 'mobile': '0400 000 000'}),
    ('DELETE', '/api/address-book/1', None),
    ('POST', '/api/address-book/sync', None),
    ('POST', '/api/recordings/purge', None),
    ('POST', '/api/recordings/purge-drive', None),
    ('POST', '/api/stats/aggregate', None),
    ('POST', '/api/queue/cleanup', None),
    ('POST', '/api/security/sip-check', None),
    ('POST', '/api/voicemail/cleanup', None),
    ('POST', '/api/stats/cross-check', {'email': False}),
]


@pytest.mark.parametrize('method,path,body', ADMIN_ONLY,
                         ids=[f'{m} {p}' for m, p, _ in ADMIN_ONLY])
def test_admin_endpoint_refuses_staff_session(client, make_store, method, path, body):
    store = make_store()
    store.db.create_staff_extension(BOB, 'test')
    before = store.db.get_staff_extension(BOB)
    _login(client, ALICE)
    r = client.open(path, method=method, json=body, headers=XFF)
    assert r.status_code == 403, r.get_data(as_text=True)
    # Nothing happened on the way to the refusal
    assert store.db.get_staff_extension(BOB) == before


@pytest.mark.parametrize('role', ['manager'])
def test_manager_is_not_admin_either(client, make_store, role):
    make_store()
    _login(client, ALICE, role=role)
    assert client.post('/api/staff/sync', headers=XFF).status_code == 403


def test_admin_session_still_works(client, make_store):
    make_store()
    _login(client, ADMIN, role='admin')
    assert client.post('/api/queue/cleanup', headers=XFF).status_code == 200
    r = client.post('/api/address-book', json={'name': 'Someone', 'mobile': '0400 000 000'},
                    headers=XFF)
    assert r.status_code == 201


def test_cron_on_the_socket_still_works(client, make_store):
    make_store()
    # No X-Forwarded-For: a local process on the unix socket
    assert client.post('/api/queue/cleanup').status_code == 200
    assert client.post('/api/recordings/purge').status_code == 200


def test_bot_key_still_works_for_admin_endpoints(client, make_store):
    make_store()
    r = client.post('/api/staff/offboard', json={'email': BOB, 'dry_run': True},
                    headers={**XFF, 'X-API-Key': BOT_KEY})
    assert r.status_code == 200


# =============================================================================
# C. Offboarding from a session stays in that tenant
# =============================================================================

@pytest.fixture
def two_tenants(make_store):
    other = make_store(tenant_id='other', name='Other')
    watson = make_store()          # last pushed context: g.tenant = watson
    for store in (other, watson):
        store.db.create_staff_extension(BOB, 'test')
        store.db.set_staff_extension_active_locked(email=BOB, is_active=True, locked=False,
                                                   updated_by='test')
    _member(BOB, 'watson')
    _member(BOB, 'other')
    return watson, other


def _bob_tenants():
    master = get_master_db()
    return {t['id'] for t in master.get_user_tenants(master.get_user_by_email(BOB)['id'])}


def test_session_offboard_touches_only_own_tenant(client, two_tenants):
    watson, other = two_tenants
    _login(client, ADMIN, role='admin')
    r = client.post('/api/staff/offboard', json={'email': BOB}, headers=XFF)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert set(body['tenants']) == {'watson'}
    assert body['tenant_access_revoked'] == ['watson']
    assert not watson.db.get_staff_extension(BOB)['is_active']
    assert other.db.get_staff_extension(BOB)['is_active']
    assert _bob_tenants() == {'other'}


def test_session_offboard_preview_shows_only_own_tenant(client, two_tenants):
    _login(client, ADMIN, role='admin')
    r = client.post('/api/staff/offboard', json={'email': BOB, 'dry_run': True}, headers=XFF)
    body = r.get_json()
    assert set(body['tenants']) == {'watson'}
    assert [a['tenant'] for a in body['tenant_access']] == ['watson']


def test_bot_offboard_still_covers_every_tenant(client, two_tenants):
    watson, other = two_tenants
    r = client.post('/api/staff/offboard', json={'email': BOB},
                    headers={**XFF, 'X-API-Key': BOT_KEY})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert set(r.get_json()['tenants']) == {'watson', 'other'}
    assert not watson.db.get_staff_extension(BOB)['is_active']
    assert not other.db.get_staff_extension(BOB)['is_active']
    assert _bob_tenants() == set()


# =============================================================================
# D. The recordings API follows reports_to, like the Recordings page
# =============================================================================

@pytest.fixture
def recordings(make_store, tmp_path, monkeypatch):
    store = make_store()
    for email in (ALICE, BOB, CAROL):
        store.db.create_staff_extension(email, 'test')
        store.db.set_staff_extension_active_locked(email=email, is_active=True, locked=False,
                                                   updated_by='test')
    store.db.update_staff_reports_to(BOB, ALICE, 'test')
    for sid, email in (('REalice', ALICE), ('REbob', BOB), ('REcarol', CAROL)):
        store.db.log_recording({'recording_sid': sid, 'call_sid': f'CA{sid}',
                                'call_type': 'inbound', 'staff_email': email, 'duration_seconds': 30,
                                'created_at': '2026-10-28T00:00:00'})
    audio = tmp_path / 'a.mp3'
    audio.write_bytes(b'ID3audio')
    from rinq.services.recording_service import recording_service
    monkeypatch.setattr(recording_service, 'get_recording_file_path', lambda sid: str(audio))
    return store


def _listed(client, query=''):
    r = client.get(f'/api/recordings{query}', headers=XFF)
    assert r.status_code == 200, r.get_data(as_text=True)
    return {x['recording_sid'] for x in r.get_json()['recordings']}


def test_user_lists_own_and_reportees_recordings_only(client, recordings):
    _login(client, ALICE)
    assert _listed(client) == {'REalice', 'REbob'}
    assert _listed(client, f'?staff_email={BOB}') == {'REbob'}
    assert _listed(client, f'?staff_email={CAROL}') == set()


def test_user_cannot_play_a_recording_outside_scope(client, recordings):
    _login(client, ALICE)
    assert client.get('/api/recordings/REbob/audio', headers=XFF).status_code == 200
    assert client.get('/api/recordings/REcarol/audio', headers=XFF).status_code == 404


def test_reportee_cannot_hear_manager(client, recordings):
    _login(client, BOB)
    assert _listed(client) == {'REbob'}
    assert client.get('/api/recordings/REalice/audio', headers=XFF).status_code == 404


def test_admin_and_view_all_users_see_everything(client, recordings):
    _login(client, ADMIN, role='admin')
    assert _listed(client) == {'REalice', 'REbob', 'REcarol'}
    assert client.get('/api/recordings/REcarol/audio', headers=XFF).status_code == 200

    user_id = _login(client, CAROL)
    get_master_db().set_user_recordings_access(user_id, 'watson', True)
    assert _listed(client) == {'REalice', 'REbob', 'REcarol'}


def test_recordings_page_uses_the_same_scope(client, recordings):
    _login(client, ALICE)
    page = client.get('/recordings').get_data(as_text=True)
    assert 'REbob' in page and 'REcarol' not in page
