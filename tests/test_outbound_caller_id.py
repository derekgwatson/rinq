"""Outbound caller ID (gotcha 38): Tina must never pick a number by chance.

No resolvable caller ID = refuse the call and SAY why (desk phones have no
screen). A set number = dial from exactly that number.
"""

ANNA = 'anna@watsonblinds.com.au'
ANNA_BROWSER = 'client:anna_at_watsonblinds_com_au'
CUSTOMER = '+61400111222'


def _call_out(client, frm=ANNA_BROWSER, to=CUSTOMER):
    r = client.post('/api/voice/outbound',
                    data={'To': to, 'From': frm, 'CallSid': 'CAagentleg',
                          'AccountSid': 'ACwatson'})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def _customer_leg(outside):
    legs = [c for c in outside.calls_created if c.get('to') == CUSTOMER]
    assert len(legs) <= 1
    return legs[0] if legs else None


def test_no_caller_id_anywhere_refuses_out_loud(client, make_store, outside):
    make_store(tenant_default_caller_id=False)
    twiml = _call_out(client)
    assert '<Say' in twiml and 'caller I D' in twiml
    assert '<Hangup/>' in twiml
    assert _customer_leg(outside) is None  # nobody was dialled


def test_staff_own_caller_id_is_used(client, make_store, outside):
    store = make_store(tenant_default_caller_id=False)
    store.db.create_staff_extension(ANNA, 'test')
    store.db.update_staff_extension_caller_id(ANNA, '+61262804443', 'test')
    _call_out(client)
    assert _customer_leg(outside)['from_'] == '+61262804443'


def test_tenant_default_is_the_last_resort(client, make_store, outside):
    store = make_store()  # tenant default = the store number
    _call_out(client)
    assert _customer_leg(outside)['from_'] == store.number


def test_staff_setting_beats_the_tenant_default(client, make_store, outside):
    store = make_store()
    store.db.create_staff_extension(ANNA, 'test')
    store.db.update_staff_extension_caller_id(ANNA, '+61262804443', 'test')
    _call_out(client)
    assert _customer_leg(outside)['from_'] == '+61262804443'


def _shared_desk_phone(store, username='desk_hume_3'):
    store.db.upsert_user({'sid': 'CLdesk', 'username': username, 'friendly_name': 'Hume desk 3',
                          'staff_email': None, 'is_active': 1, 'synced_at': None})
    store.db.mark_shared_desk_phone('CLdesk')
    return f'sip:{username}@watsonblinds.sip.twilio.com'


def test_empty_shared_desk_phone_calls_out_on_the_tenant_default(client, make_store, outside):
    store = make_store()
    _call_out(client, frm=_shared_desk_phone(store))
    assert _customer_leg(outside)['from_'] == store.number


def test_empty_shared_desk_phone_with_no_default_says_how_to_log_in(client, make_store, outside):
    store = make_store(tenant_default_caller_id=False)
    twiml = _call_out(client, frm=_shared_desk_phone(store))
    assert 'star 5 0' in twiml
    assert _customer_leg(outside) is None
