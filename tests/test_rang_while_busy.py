"""Rang while you were on a call (16 Nov 2026).

Alison rang back twice during Brittany's 13-minute call. Tina rightly made no
"press 1 for Brittany" offer - Brittany couldn't answer - but nothing told
Brittany either. Now that skipped offer becomes a note on Brittany's phone
page, shown once her call ends, and Stuart is told she's on another call.
"""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import ring

BRITT = 'brittany@watsonblinds.com.au'
CALLER = '+61400111222'


def _utc_minutes_ago(n):
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def on_a_call(monkeypatch):
    """Emails the call log says are on a call right now."""
    from rinq.database.db import Database
    busy = set()
    monkeypatch.setattr(Database, 'get_active_calls',
                        lambda self: [{'agent_email': e} for e in busy])
    return busy


def _britt_rang_them(store):
    store.db.upsert_user({'sid': 'USbritt', 'username': 'britt', 'friendly_name': 'Brittany Moore',
                          'staff_email': BRITT, 'is_active': 1, 'synced_at': '2026-11-16'})
    store.db.log_call({'call_sid': 'CAout', 'direction': 'outbound', 'from_number': store.number,
                       'to_number': CALLER, 'agent_email': BRITT, 'status': 'completed',
                       'started_at': _utc_minutes_ago(60)})


def _login(client, email):
    from rinq.database.master import get_master_db
    master = get_master_db()
    user = master.get_or_create_user(email, email.split('@')[0])
    master.add_user_to_tenant('watson', user['id'], role='user')
    with client.session_transaction() as s:
        s['user_id'], s['user_email'], s['tenant_id'] = user['id'], email, 'watson'


def _fresh():
    """make_store keeps one app context pushed, so get_current_user()'s
    per-request cache would carry the last user over; production has none."""
    from flask import g, has_app_context
    if has_app_context():
        g.pop('_current_user', None)


def _dismiss(client, note_id):
    _fresh()
    return client.post(f'/api/rang-while-busy/{note_id}/dismiss').status_code


def _notes(client):
    _fresh()
    r = client.get('/api/rang-while-busy')
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()['notes']


def test_busy_staff_member_gets_a_note_and_no_offer(client, make_store, on_a_call):
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    _britt_rang_them(store)
    on_a_call.add(BRITT)
    twiml = ring(client, store)
    assert 'Press 1' not in twiml
    _login(client, BRITT)
    notes = _notes(client)
    assert [n['from_number'] for n in notes] == [CALLER]
    assert notes[0]['times'] == 1


def test_ringing_again_counts_on_the_same_note(client, make_store, on_a_call):
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    _britt_rang_them(store)
    on_a_call.add(BRITT)
    ring(client, store, sid='CAfirst')
    ring(client, store, sid='CAsecond')
    _login(client, BRITT)
    notes = _notes(client)
    assert len(notes) == 1 and notes[0]['times'] == 2


def test_free_staff_member_gets_the_offer_not_a_note(client, make_store, on_a_call):
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    store.db.create_staff_extension(BRITT, 'test', extension='6801')
    _britt_rang_them(store)
    ring(client, store)
    _login(client, BRITT)
    assert _notes(client) == []


def test_note_goes_once_she_rings_them_back(client, make_store, on_a_call):
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    _britt_rang_them(store)
    on_a_call.add(BRITT)
    ring(client, store)
    store.db.log_call({'call_sid': 'CAback', 'direction': 'outbound', 'from_number': store.number,
                       'to_number': CALLER, 'agent_email': BRITT, 'status': 'answered',
                       'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')})
    _login(client, BRITT)
    assert _notes(client) == []


def test_note_goes_once_a_colleague_answers_them(client, make_store, on_a_call):
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    _britt_rang_them(store)
    on_a_call.add(BRITT)
    ring(client, store)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    store.db.log_call({'call_sid': 'CAanna', 'direction': 'inbound', 'from_number': CALLER,
                       'to_number': store.number, 'agent_email': 'anna@watsonblinds.com.au',
                       'status': 'answered', 'started_at': now})
    store.db.update_call_log('CAanna', {'answered_at': now})
    _login(client, BRITT)
    assert _notes(client) == []


def test_a_call_stuart_handled_alone_does_not_clear_it(client, make_store, on_a_call):
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    _britt_rang_them(store)
    on_a_call.add(BRITT)
    ring(client, store)  # this call itself goes on to Stuart
    _login(client, BRITT)
    assert len(_notes(client)) == 1


def test_dismiss_is_her_own_only(client, make_store, on_a_call):
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    _britt_rang_them(store)
    on_a_call.add(BRITT)
    ring(client, store)
    _login(client, 'someone.else@watsonblinds.com.au')
    assert _notes(client) == []
    _login(client, BRITT)
    note_id = _notes(client)[0]['id']
    _login(client, 'someone.else@watsonblinds.com.au')
    assert _dismiss(client, note_id) == 404
    _login(client, BRITT)
    assert _dismiss(client, note_id) == 200
    assert _notes(client) == []


def test_cannot_tell_who_is_busy_means_no_note(client, make_store, monkeypatch):
    from rinq.database.db import Database

    def boom(self):
        raise RuntimeError("db locked")
    monkeypatch.setattr(Database, 'get_active_calls', boom)
    store = make_store(no_answer='ai_receptionist').with_staff(BRITT)
    _britt_rang_them(store)
    ring(client, store)
    _login(client, BRITT)
    assert _notes(client) == []


def test_stuart_is_told_she_is_on_another_call(make_store, on_a_call):
    from rinq.api.callback_routes import called_by_for_stuart
    store = make_store()
    _britt_rang_them(store)
    assert 'called_by_busy' not in called_by_for_stuart(CALLER, store.db)
    on_a_call.add(BRITT)
    params = called_by_for_stuart(CALLER, store.db)
    assert params['called_by_name'] == 'Brittany'
    assert params['called_by_busy'] == '1'
