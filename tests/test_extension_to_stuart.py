"""Unanswered extension -> Stuart takes a message (Admin -> Settings switch,
off by default - Derek 2026-10-28: "have it ready, just not switched on").

Who counts as staff is the security line: only a call placed from a logged-in
Tina browser or SIP login carries staff_email to Rosie. A caller coming in
through the extension line is a customer whatever number they show.
"""

from urllib.parse import parse_qs, urlparse
import re

from conftest import ROSIE_ANSWER_URL

BRITT = 'brittany@watsonblinds.com.au'
ANNA = 'anna@watsonblinds.com.au'


def _rosie_params(twiml):
    m = re.search(r'<Redirect method="POST">([^<]+)</Redirect>', twiml)
    assert m, twiml
    url = m.group(1).replace('&amp;', '&')
    assert url.startswith(ROSIE_ANSWER_URL)
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _setup(make_store, on):
    store = make_store()
    store.db.create_staff_extension(BRITT, 'test', extension='6801')
    if on:
        store.db.set_bot_setting('extension_no_answer_ai', '1', 'test')
    return store


def _directory_no_answer(client, store):
    r = client.post(f'/api/voice/extension-no-answer?called={store.number}&from=%2B61400111222'
                    f'&flow_id=&ext=6801',
                    data={'CallSid': 'CAcust', 'To': store.number, 'From': '+61400111222'})
    return r.get_data(as_text=True)


def _internal_no_answer(client, caller=ANNA):
    r = client.post('/api/voice/extension-dial-status?ext=6801&caller=' + caller,
                    data={'CallSid': 'CAinternal', 'AccountSid': 'ACwatson'})
    return r.get_data(as_text=True)


def test_switch_is_off_by_default(make_store):
    from rinq.api.ai_test_routes import extension_ai_enabled
    assert extension_ai_enabled(make_store().db) is False


def test_off_customer_on_extension_line_gets_the_old_fallback(client, make_store):
    store = _setup(make_store, on=False)
    assert ROSIE_ANSWER_URL not in _directory_no_answer(client, store)


def test_off_colleague_hears_not_answered(client, make_store):
    _setup(make_store, on=False)
    twiml = _internal_no_answer(client)
    assert 'The call was not answered' in twiml
    assert ROSIE_ANSWER_URL not in twiml


def test_on_customer_gets_stuart_taking_a_message_but_never_as_staff(client, make_store):
    store = _setup(make_store, on=True)
    params = _rosie_params(_directory_no_answer(client, store))
    assert params['for_email'] == BRITT
    assert 'staff_email' not in params


def test_on_colleague_gets_stuart_as_verified_staff(client, make_store):
    _setup(make_store, on=True)
    params = _rosie_params(_internal_no_answer(client))
    assert params['for_email'] == BRITT
    assert params['staff_email'] == ANNA


def test_on_unknown_extension_is_not_sent_to_stuart(client, make_store):
    _setup(make_store, on=True)
    r = client.post('/api/voice/extension-dial-status?ext=9999&caller=' + ANNA,
                    data={'CallSid': 'CAx', 'AccountSid': 'ACwatson'})
    assert ROSIE_ANSWER_URL not in r.get_data(as_text=True)


def test_internal_call_carries_the_logged_in_caller_to_the_dial_action(client, make_store, outside):
    """The staff identity comes from the Tina login (client identity), not
    from anything the far end could set."""
    store = _setup(make_store, on=True)
    store.db.create_staff_extension(ANNA, 'test', extension='6840')
    r = client.post('/api/voice/outbound',
                    data={'To': '6801', 'From': 'client:anna_at_watsonblinds_com_au',
                          'CallSid': 'CAanna', 'AccountSid': 'ACwatson'})
    twiml = r.get_data(as_text=True)
    m = re.search(r'<Dial action="([^"]+)"', twiml)
    assert m, twiml
    action = parse_qs(urlparse(m.group(1).replace('&amp;', '&')).query)
    assert action['ext'] == ['6801']
    assert action['caller'] == [ANNA]
