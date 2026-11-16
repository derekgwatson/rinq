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
