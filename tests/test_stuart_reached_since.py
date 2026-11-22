"""Has a caller who hung up on Stuart since reached the team? (Derek 2026-11-23)

Half of the callers who hung up on Stuart's greeting in a month never reached
anyone. Rosie raises a ring-back ticket for those - and asks Tina first, so a
caller who has since got through to a person, or been rung, isn't rung again."""

import pytest

KEY = 'test-bot-key'
PROXIED = {'X-Forwarded-For': '1.2.3.4'}
BEN = 'ben@watsonblinds.com.au'
CALLER = '+61404330839'
HUNG_UP = '2026-11-22T21:26:10.500054+00:00'     # Rosie's own timestamp format


@pytest.fixture
def store(make_store):
    s = make_store().with_staff(BEN)
    s.tenant_id = 'watson'
    return s


def _log(store, **row):
    store.db.log_call({'call_sid': row.pop('sid', 'CA1'), **row})


def _ask(client, monkeypatch, number='0404330839', since=HUNG_UP, headers=None):
    monkeypatch.setenv('BOT_API_KEY', KEY)
    return client.get('/api/stuart/reached-since',
                      query_string={'tenant': 'watson', 'number': number, 'since': since},
                      headers=headers if headers is not None else {**PROXIED, 'X-API-Key': KEY})


def test_nobody_since(client, store, monkeypatch):
    # before the hang-up doesn't count
    _log(store, direction='inbound', from_number=CALLER, to_number=store.number,
         agent_email=BEN, status='answered', started_at='2026-11-22 20:00:00')
    r = _ask(client, monkeypatch)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {'reached': False}


def test_they_rang_back_and_a_person_answered(client, store, monkeypatch):
    _log(store, direction='inbound', from_number=CALLER, to_number=store.number,
         agent_email=BEN, status='answered', started_at='2026-11-22 21:40:00')
    body = _ask(client, monkeypatch).get_json()
    assert body['reached'] and body['how'] == 'answered' and body['staff_email'] == BEN


def test_we_rang_them_even_unanswered(client, store, monkeypatch):
    _log(store, direction='outbound', from_number=store.number, to_number=CALLER,
         agent_email=BEN, status='missed', started_at='2026-11-22 23:00:00')
    body = _ask(client, monkeypatch).get_json()
    assert body['reached'] and body['how'] == 'rang'


def test_rang_back_but_nobody_answered_or_only_stuart(client, store, monkeypatch):
    _log(store, sid='CA1', direction='inbound', from_number=CALLER, to_number=store.number,
         status='voicemail', started_at='2026-11-22 22:00:00')
    _log(store, sid='CA2', direction='inbound', call_type='ai_receptionist', from_number=CALLER,
         to_number=store.number, status='ai_answered', started_at='2026-11-22 22:10:00')
    _log(store, sid='CA3', direction='inbound', from_number=CALLER, to_number=store.number,
         status='abandoned', started_at='2026-11-22 22:20:00')
    # the customer's own leg of a staff call to someone else's number
    _log(store, sid='CA4', direction='outbound', from_number=store.number,
         to_number='+61400999888', agent_email=BEN, status='completed',
         started_at='2026-11-22 22:30:00')
    assert _ask(client, monkeypatch).get_json() == {'reached': False}


def test_bad_since_is_refused(client, store, monkeypatch):
    assert _ask(client, monkeypatch, since='yesterday').status_code == 400


def test_bot_key_only(client, store):
    with client.session_transaction() as s:
        s['user_id'] = 1
        s['user_email'] = 'someone@watsonblinds.com.au'
    r = client.get('/api/stuart/reached-since',
                   query_string={'tenant': 'watson', 'number': CALLER, 'since': HUNG_UP},
                   headers=PROXIED)
    assert r.status_code == 403
