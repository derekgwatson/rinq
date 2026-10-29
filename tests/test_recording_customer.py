"""The recordings list shows the customer as well as the staff member
(Derek 2026-10-30: "currently I only see the agent").

Outbound recordings were logged with only the customer's number, so the name
is looked up (Clara, then Tina's address book) when the recording arrives.
"""

import rinq.integrations as integrations
from rinq.services import recording_service as rs

PHIL = 'phil@watsonblinds.com.au'


class _Clara:
    def __init__(self, known):
        self.known, self.asked = known, []

    def find_by_phone(self, number):
        self.asked.append(number)
        name = self.known.get(number)
        return {'name': name} if name else None


def _webhook(client, monkeypatch, store, ctype, customer, name=None):
    captured = {}
    monkeypatch.setattr(rs.recording_service, 'process_completed_recording',
                        lambda **kw: captured.update(kw) or {'success': True})
    conf = f'call_CA{ctype}'
    store.db.add_participant(conf, 'CAagent', 'agent', name='Phil', email=PHIL)
    store.db.add_participant(conf, 'CAcust', 'customer', name=name, phone_number=customer)
    r = client.post(f'/api/voice/recording-status?conf={conf}&ctype={ctype}',
                    data={'RecordingSid': f'RE{ctype}', 'RecordingStatus': 'completed',
                          'RecordingDuration': '60', 'RecordingUrl': 'https://x.test/r',
                          'AccountSid': 'ACtest', 'To': store.number})
    assert r.status_code == 200
    return captured


def test_an_outbound_recording_gets_the_customers_name(client, make_store, monkeypatch):
    store = make_store()
    clara = _Clara({'+61400111222': 'Kay Burman'})
    monkeypatch.setattr(integrations, '_customer_lookup', clara)
    got = _webhook(client, monkeypatch, store, 'outbound', '+61400111222', name='+61400111222')
    assert got['caller_name'] == 'Kay Burman'
    assert got['to_number'] == '+61400111222'


def test_an_inbound_name_from_the_queue_is_kept_without_asking_again(client, make_store, monkeypatch):
    store = make_store()
    clara = _Clara({'+61400111222': 'Someone Else'})
    monkeypatch.setattr(integrations, '_customer_lookup', clara)
    got = _webhook(client, monkeypatch, store, 'inbound', '+61400111222', name='Jenny Ryan')
    assert got['caller_name'] == 'Jenny Ryan'
    assert clara.asked == []


def test_nobody_knows_them_keeps_what_there_was(client, make_store, monkeypatch):
    store = make_store()
    monkeypatch.setattr(integrations, '_customer_lookup', _Clara({}))
    got = _webhook(client, monkeypatch, store, 'outbound', '+61400111222', name='+61400111222')
    assert got['caller_name'] == '+61400111222'


def test_tinas_address_book_is_the_fallback(make_store, monkeypatch):
    store = make_store()
    monkeypatch.setattr(integrations, '_customer_lookup', _Clara({}))
    monkeypatch.setattr(store.db, 'get_address_book_by_mobile',
                        lambda m: {'name': 'Bob Supplier'} if m == '+61400111222' else None)
    assert rs.customer_name_for_number(store.db, '0400 111 222') == 'Bob Supplier'


def test_a_withheld_number_is_never_looked_up(make_store, monkeypatch):
    clara = _Clara({})
    monkeypatch.setattr(integrations, '_customer_lookup', clara)
    assert rs.customer_name_for_number(make_store().db, 'anonymous') is None
    assert clara.asked == []


def test_the_list_shows_the_customer_end_of_each_call():
    out = rs.customer_display({'call_type': 'outbound', 'from_number': PHIL,
                               'to_number': '+61400111222', 'caller_name': '+61400111222'})
    assert out == {'customer_number': '+61400111222', 'customer_name': None}
    inbound = rs.customer_display({'call_type': 'inbound', 'from_number': '+61400111222',
                                   'caller_name': 'Ryan, Jenny'})
    assert inbound == {'customer_number': '+61400111222', 'customer_name': 'Ryan, Jenny'}


def test_a_manager_sees_customer_and_staff_on_the_page(client, make_store):
    from rinq.database.master import get_master_db
    store = make_store()
    store.db.log_recording({'recording_sid': 'REx', 'call_sid': 'CAx', 'call_type': 'outbound',
                            'from_number': PHIL, 'to_number': '+61400111222',
                            'caller_name': 'Kay Burman', 'staff_email': PHIL,
                            'staff_name': 'Phil Rivera', 'duration_seconds': 61,
                            'created_at': '2026-10-29T05:00:00+00:00'})
    master = get_master_db()
    user = master.get_or_create_user('derek@watsonblinds.com.au', 'Derek')
    user_id = user['id'] if isinstance(user, dict) else user
    master.add_user_to_tenant('watson', user_id, role='admin')
    with client.session_transaction() as s:
        s['user_id'], s['user_email'], s['tenant_id'] = user_id, 'derek@watsonblinds.com.au', 'watson'

    html = client.get('/recordings').get_data(as_text=True)
    assert '<th>Customer</th>' in html and '<th>Staff</th>' in html
    row = html[html.index('class="rec-row"'):html.index('class="detail-row"')]
    assert 'Kay Burman' in row and '+61400111222' in row and 'Phil Rivera' in row
