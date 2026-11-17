"""Who rang a number the caller GAVE Stuart (#325, Derek 2026-11-18).

Sam's caller ID was hidden, so Tina couldn't tell Stuart "Rhae rang this
number an hour ago" as the call came in. Sam read his number out, and Stuart
now asks Tina the same question with it - one function answers both ways,
so wherever the number came from, Stuart knows the same."""

from datetime import datetime, timedelta, timezone

import pytest

KEY = 'test-bot-key'
PROXIED = {'X-Forwarded-For': '1.2.3.4'}
RHAE = 'rhae@watsonblinds.com.au'


def _utc_minutes_ago(n):
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def rhae_rang_sam(make_store):
    store = make_store().with_staff(RHAE)
    store.tenant_id = 'watson'
    store.db.upsert_user({'sid': 'USrhae', 'username': 'rhae', 'friendly_name': 'Rhae Tan',
                          'staff_email': RHAE, 'is_active': 1, 'synced_at': '2026-11-17'})
    store.db.log_call({'call_sid': 'CAout', 'direction': 'outbound', 'from_number': store.number,
                       'to_number': '+61404330839', 'agent_email': RHAE, 'status': 'completed',
                       'started_at': _utc_minutes_ago(55)})
    return store


def _ask(client, number, tenant='watson', headers=None):
    return client.get('/api/stuart/called-by', query_string={'tenant': tenant, 'number': number},
                      headers=headers if headers is not None else {**PROXIED, 'X-API-Key': KEY})


def test_a_number_read_out_finds_who_rang_it_same_as_caller_id(client, rhae_rang_sam, monkeypatch):
    from rinq.api.callback_routes import called_by_for_stuart
    monkeypatch.setenv('BOT_API_KEY', KEY)
    r = _ask(client, '0404330839')          # as the caller says it
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['called_by_name'] == 'Rhae'
    # exactly what Tina hands Stuart when the number shows
    assert r.get_json() == called_by_for_stuart('+61404330839', rhae_rang_sam.db)


def test_nobody_rang_it(client, rhae_rang_sam, monkeypatch):
    monkeypatch.setenv('BOT_API_KEY', KEY)
    r = _ask(client, '0400111222')
    assert r.status_code == 200 and r.get_json() == {}


def test_never_open_to_a_logged_in_user(client, rhae_rang_sam):
    with client.session_transaction() as s:
        s['user_id'] = 1
        s['user_email'] = 'someone@watsonblinds.com.au'
    assert _ask(client, '0404330839', headers=PROXIED).status_code == 403


def test_unknown_tenant_is_refused(client, rhae_rang_sam, monkeypatch):
    monkeypatch.setenv('BOT_API_KEY', KEY)
    assert _ask(client, '0404330839', tenant='nope').status_code == 404
