"""The AI receptionist test line - extension 9000 (rinq/api/ai_test_routes.py)."""


def _dial_from_tina(client, digits, staff='client:anna_at_watsonblinds_com_au'):
    r = client.post('/api/voice/outbound',
                    data={'To': digits, 'From': staff, 'CallSid': 'CAstaffleg',
                          'AccountSid': 'ACwatson'})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_9000_from_tina_asks_who_to_pretend_to_be(client, make_store):
    make_store()
    twiml = _dial_from_tina(client, '9000')
    assert '/api/voice/ai-test/start' in twiml
    assert '<Gather' in twiml
    # A browser has no number of its own to fall back on
    assert 'your own number' not in twiml


def test_9000_from_a_desk_phone_sip_uri(client, make_store):
    make_store()
    twiml = _dial_from_tina(client, 'sip:9000@watsonblinds.sip.twilio.com:5060;transport=UDP',
                            staff='sip:desk_hume_3@watsonblinds.sip.twilio.com')
    assert '/api/voice/ai-test/start' in twiml


def test_other_extensions_are_not_the_test_line(client, make_store):
    make_store()
    assert '/api/voice/ai-test/start' not in _dial_from_tina(client, '6840')


# --- From OUTSIDE, via the extension directory: staff mobiles only ----------

STAFF_MOBILE = '+61400999888'


def _press_9000_from(client, store, caller):
    from urllib.parse import quote
    r = client.post(f'/api/voice/extension-dial?called={quote(store.number)}'
                    f'&from={quote(caller)}&flow_id=',
                    data={'Digits': '9000', 'CallSid': 'CAoutside',
                          'To': store.number, 'From': caller})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_a_staff_mobile_can_use_the_test_line_from_outside(client, make_store):
    store = make_store()
    store.db.upsert_address_book_entry('Anna', '0400 999 888', STAFF_MOBILE,
                                       source='peter', external_id='anna')
    twiml = _press_9000_from(client, store, STAFF_MOBILE)
    assert '/api/voice/ai-test/start' in twiml


def test_a_customer_pressing_9000_hears_an_invalid_extension(client, make_store):
    store = make_store()
    twiml = _press_9000_from(client, store, '+61400111222')
    assert '/api/voice/ai-test/start' not in twiml
    assert 'not valid' in twiml


def test_a_withheld_number_cannot_use_the_test_line(client, make_store):
    store = make_store()
    twiml = _press_9000_from(client, store, '+anonymous')
    assert '/api/voice/ai-test/start' not in twiml


# --- Who Stuart thinks is calling -------------------------------------------

def _start(client, store, digits, caller=STAFF_MOBILE):
    from urllib.parse import quote
    r = client.post(f'/api/voice/ai-test/start?called={quote(store.number)}&from={quote(caller)}',
                    data={'Digits': digits, 'CallSid': 'CAtest', 'To': store.number, 'From': caller})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_pressing_hash_alone_tests_as_a_withheld_caller(client, make_store):
    store = make_store()
    twiml = _start(client, store, '')
    assert 'caller_number=anonymous' in twiml
    assert 'withheld number' in twiml
    # NOT the staff member's own mobile
    assert '61400999888' not in twiml.split('Redirect', 1)[1]


def test_a_typed_number_is_who_stuart_thinks_is_calling(client, make_store):
    store = make_store()
    twiml = _start(client, store, '0400111222')
    assert 'caller_number=%2B61400111222' in twiml
    assert 'anonymous' not in twiml


def test_the_prompt_offers_hash_for_withheld(client, make_store):
    make_store()
    twiml = _dial_from_tina(client, '9000')
    assert 'withheld number' in twiml


# --- The test line follows the test store's real hours (Derek 2026-11-18) ---

def _with_tester(store, monkeypatch):
    import rinq.api.ai_test_routes as ai
    rung = []
    monkeypatch.setattr(ai, 'ring_for_takeover', lambda staff, *a: rung.append(staff['email']))
    store.db.upsert_address_book_entry('Anna', '0400 999 888', STAFF_MOBILE, source='peter',
                                       external_id='anna', email='anna@watsonblinds.com.au')
    return rung


def _handover(twiml):
    return twiml.split('Redirect', 1)[1]


def test_open_store_test_call_rings_the_tester_as_the_team(client, make_store, monkeypatch):
    store = make_store()
    rung = _with_tester(store, monkeypatch)
    twiml = _handover(_start(client, store, '0400111222'))
    assert 'ringing=1' in twiml and 'reason=closed' not in twiml
    assert rung == ['anna@watsonblinds.com.au']


def test_after_hours_test_call_is_a_closed_call(client, make_store, monkeypatch):
    """After hours the test line greeted "I'll help while I grab someone"
    while real callers heard the store was closed."""
    store = make_store(open=False)
    rung = _with_tester(store, monkeypatch)
    twiml = _handover(_start(client, store, '0400111222'))
    assert 'reason=closed' in twiml
    assert 'ringing=1' not in twiml
    assert rung == []


# --- "Pretend it's..." - a chosen store at a chosen moment (Derek 2026-11-26) ---

def test_pretend_plays_the_chosen_store_closed_for_its_closure(client, make_store, monkeypatch):
    """Batemans Bay at 10am on the Christmas party Friday: closed by its real
    closure, with the reason and the pretend time for Stuart - while the
    real clock says the store is open."""
    from datetime import datetime, timezone
    from urllib.parse import unquote_plus
    import rinq.api.ai_test_routes as ai
    from conftest import STORE_HOURS
    store = make_store(name='Batemans Bay', hours=STORE_HOURS)
    store.db.add_schedule_holiday(store.schedule_id, 'our staff Christmas party', '2026-11-27',
                                  False, 'test')
    rung = _with_tester(store, monkeypatch)
    ai.set_pretend(store.db, 'Batemans Bay', '2026-11-27T10:00', 'test')
    twiml = unquote_plus(_start(client, store, '0400111222'))
    assert "Pretending it's Batemans Bay, Friday 27 November at 10am" in twiml
    handover = twiml.split('Redirect', 1)[1]
    assert 'queue_name=Batemans Bay' in handover
    assert 'pretend_now=2026-11-27T10:00' in handover
    assert 'reason=closed' in handover
    assert 'closed_for=our staff Christmas party' in handover
    assert rung == []


def test_pretend_an_open_moment_is_an_open_test_call(client, make_store, monkeypatch):
    from conftest import STORE_HOURS
    import rinq.api.ai_test_routes as ai
    store = make_store(name='Batemans Bay', hours=STORE_HOURS)
    rung = _with_tester(store, monkeypatch)
    ai.set_pretend(store.db, 'Batemans Bay', '2026-11-26T11:00', 'test')
    twiml = _handover(_start(client, store, '0400111222'))
    assert 'reason=closed' not in twiml and 'ringing=1' in twiml
    assert 'pretend_now=2026-11-26T11%3A00' in twiml


def test_pretend_ends_by_itself_and_can_be_stopped(make_store):
    from datetime import datetime, timedelta, timezone
    import rinq.api.ai_test_routes as ai
    store = make_store(name='Batemans Bay')
    t0 = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
    ai.set_pretend(store.db, 'Batemans Bay', '2026-11-27T10:00', 'test', now_utc=t0)
    assert ai.get_pretend(store.db, now_utc=t0 + timedelta(minutes=59))['store'] == 'Batemans Bay'
    assert ai.get_pretend(store.db, now_utc=t0 + timedelta(minutes=ai.PRETEND_MINUTES)) is None
    ai.set_pretend(store.db, 'Batemans Bay', '2026-11-27T10:00', 'test')
    ai.clear_pretend(store.db, 'test')
    assert ai.get_pretend(store.db) is None


def test_pretend_refuses_an_unknown_store_or_a_bad_time(make_store):
    import pytest
    import rinq.api.ai_test_routes as ai
    store = make_store(name='Batemans Bay')
    with pytest.raises(ValueError):
        ai.set_pretend(store.db, 'Narnia', '2026-11-27T10:00', 'test')
    with pytest.raises(ValueError):
        ai.set_pretend(store.db, 'Batemans Bay', 'Friday', 'test')


def test_a_real_call_ignores_the_pretend_time(client, make_store):
    """Only the 9000 line reads it."""
    from conftest import STORE_HOURS, ring
    import rinq.api.ai_test_routes as ai
    store = make_store(name='Batemans Bay', hours=STORE_HOURS).with_staff('phil@watsonblinds.com.au')
    ai.set_pretend(store.db, 'Batemans Bay', '2026-11-28T10:00', 'test')   # a Saturday
    twiml = ring(client, store)
    assert '<Enqueue' in twiml and 'pretend_now' not in twiml


def _admin_logged_in(client):
    from flask import g, has_app_context
    from rinq.database.master import get_master_db
    master = get_master_db()
    user = master.get_or_create_user('derek@watsonblinds.com.au', 'Derek')
    user_id = user['id'] if isinstance(user, dict) else user
    master.add_user_to_tenant('watson', user_id, role='admin')
    with client.session_transaction() as s:
        s['user_id'], s['user_email'], s['tenant_id'] = user_id, 'derek@watsonblinds.com.au', 'watson'
    if has_app_context():
        g.pop('_current_user', None)


def test_admin_settings_page_starts_and_stops_pretending(client, make_store):
    import rinq.api.ai_test_routes as ai
    store = make_store(name='Batemans Bay')
    _admin_logged_in(client)
    page = client.get('/admin/settings')
    assert page.status_code == 200, page.get_data(as_text=True)[:500]
    html = page.get_data(as_text=True)
    assert 'Test line (9000)' in html and '<option value="Batemans Bay"' in html

    r = client.post('/admin/settings/test-line-pretend',
                    data={'store': 'Batemans Bay', 'at': '2026-11-27T10:00'})
    assert r.status_code == 302
    assert ai.get_pretend(store.db)['at'] == '2026-11-27T10:00'
    html = client.get('/admin/settings').get_data(as_text=True)
    assert 'Batemans Bay, Friday 27 November at 10am' in html
    assert 'value="2026-11-27T10:00"' in html

    client.post('/admin/settings/test-line-pretend', data={'stop': '1'})
    assert ai.get_pretend(store.db) is None
