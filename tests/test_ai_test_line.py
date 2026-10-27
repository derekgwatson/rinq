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
