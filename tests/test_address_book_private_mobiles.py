"""Peter's private mobiles reach Tina's address book UNLISTED: found by
number (so extension 9000 and the caller's name work) but never shown."""
from types import SimpleNamespace

from rinq.services.address_book_sync import PeterAddressBookSource, sync_address_book

LISTED = {'id': 1, 'name': 'Clare', 'phone_mobile': '0400 000 001', 'work_email': 'clare@x'}
PRIVATE = {'id': 2, 'name': 'Jacinta', 'mobile': '0400000002', 'email': 'jacinta@x'}


def _source(private):
    return PeterAddressBookSource(SimpleNamespace(
        get_active_staff=lambda: [LISTED], get_private_mobiles=lambda: private))


def test_private_mobile_is_recognised_but_not_listed(make_store):
    db = make_store().db
    sync_address_book(db, _source([PRIVATE]))

    assert db.get_address_book_by_mobile('+61400000002')['name'] == 'Jacinta'
    assert [e['name'] for e in db.get_address_book()] == ['Clare']
    assert [e['name'] for e in db.get_address_book(search='Jac')] == []


def test_private_mobile_opens_the_test_line(make_store):
    from rinq.api.ai_test_routes import is_staff_mobile
    db = make_store().db
    sync_address_book(db, _source([PRIVATE]))
    assert is_staff_mobile(db, '+61400000002')


def test_a_leaver_drops_off(make_store):
    db = make_store().db
    sync_address_book(db, _source([PRIVATE]))
    sync_address_book(db, _source([]))   # Peter no longer lists her
    assert db.get_address_book_by_mobile('+61400000002') is None


def test_peter_not_answering_keeps_private_mobiles(make_store):
    db = make_store().db
    sync_address_book(db, _source([PRIVATE]))
    sync_address_book(db, _source(None))  # Peter's private-mobile call failed
    assert db.get_address_book_by_mobile('+61400000002')['name'] == 'Jacinta'


def test_listing_never_carries_a_private_mobile(make_store):
    db = make_store().db
    sync_address_book(db, _source([PRIVATE]))
    assert all(e.get('unlisted') == 0 for e in db.get_address_book())
