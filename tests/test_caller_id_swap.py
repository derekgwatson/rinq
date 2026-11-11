"""The ⇄ swap button on the phone page must name both numbers the same way.

Britt (2026-11-11): Canberra active, Wagga as the swap. Wagga's phone_numbers
row is named with its own digits (Twilio's default); the office name lives in
verified_caller_ids. The button read "⇄ 61269316440" while the active number
read "Canberra", so after a press the only word on screen was still
"Canberra" (now on the button) and it looked like it bounced back.
"""
from rinq.database.master import get_master_db

BRITT = 'brittany.moore@watsonblinds.com.au'
CANBERRA = '+61262804443'
WAGGA = '+61269316440'


def _britt_logged_in(client, store):
    db = store.db
    db.upsert_phone_number({'sid': 'PNcbr', 'phone_number': CANBERRA, 'friendly_name': '61262804443',
                            'forward_to': None, 'is_active': 1, 'synced_at': None})
    db.upsert_phone_number({'sid': 'PNwag', 'phone_number': WAGGA, 'friendly_name': '61269316440',
                            'forward_to': None, 'is_active': 1, 'synced_at': None})
    db.add_verified_caller_id(CANBERRA, 'Canberra', created_by='test')
    db.add_verified_caller_id(WAGGA, 'Wagga Wagga', created_by='test')
    db.create_staff_extension(BRITT, 'test')
    db.update_staff_extension_caller_id(BRITT, CANBERRA, 'test')
    db.update_staff_extension_alternate_caller_id(BRITT, WAGGA, 'test')

    master = get_master_db()
    user = master.get_or_create_user(BRITT, 'Britt')
    user_id = user['id'] if isinstance(user, dict) else user
    master.add_user_to_tenant('watson', user_id, role='user')
    with client.session_transaction() as s:
        s['user_id'], s['user_email'], s['tenant_id'] = user_id, BRITT, 'watson'


def test_swap_button_names_wagga_not_its_digits(client, make_store):
    from rinq.services.caller_id import caller_id_label
    store = make_store()
    _britt_logged_in(client, store)
    assert caller_id_label(WAGGA, store.db) == 'Wagga Wagga'
    assert caller_id_label(CANBERRA, store.db) == 'Canberra'


def test_swap_returns_names_for_the_page_to_show(client, make_store):
    store = make_store()
    _britt_logged_in(client, store)

    data = client.post('/my-desk-phone/swap-caller-id').get_json()
    assert data['active_caller_id'] == WAGGA and data['active_display'] == 'Wagga Wagga'
    assert data['toggle_target'] == CANBERRA and data['toggle_display'] == 'Canberra'

    data = client.post('/my-desk-phone/swap-caller-id').get_json()
    assert data['active_display'] == 'Canberra' and data['toggle_display'] == 'Wagga Wagga'
