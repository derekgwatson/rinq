"""How a call ends in call_log - Twilio's call-status callback
(rinq/api/routes.py, call_status_callback), and the number setting that makes
Twilio send it at all (TwilioService.sync_phone_numbers).

Canberra, Shoalhaven, Batemans Bay and Wagga never had the callback set (it
was a one-off run before they moved to Tina), so their calls kept whatever
call_log last guessed: a caller who hung up before Stuart answered read
"answered by Stuart" (Helen, 2026-10-31).
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from conftest import STORE_HOURS, ring

PHIL = 'phil@watsonblinds.com.au'


@pytest.fixture
def stuart_told(monkeypatch):
    import rinq.api.routes as routes
    told = []
    monkeypatch.setattr(routes, '_notify_ai_receptionist_call_ended',
                        lambda sid, status: told.append((sid, status)))
    return told


def _after_hours_stuart_call(client, make_store, frozen_clock):
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist',
                       closed_action='ai_receptionist').with_staff(PHIL)
    frozen_clock.set(datetime(2026, 10, 31, 7, 29))   # Saturday morning
    ring(client, store)
    assert store.db.get_call_log_field('CAcustomer1', 'status') == 'ai_answered'
    return store


def _ended_at(store):
    with store.db._get_conn() as conn:
        return conn.execute("SELECT ended_at FROM call_log WHERE call_sid = 'CAcustomer1'").fetchone()[0]


def _ended(client, status, duration='0'):
    r = client.post('/api/voice/call-status',
                    data={'CallSid': 'CAcustomer1', 'CallStatus': status,
                          'CallDuration': duration})
    assert r.status_code == 200


def test_hang_up_before_stuart_answers_is_missed(client, make_store, frozen_clock, stuart_told):
    store = _after_hours_stuart_call(client, make_store, frozen_clock)
    _ended(client, 'no-answer')
    assert store.db.get_call_log_field('CAcustomer1', 'status') == 'missed'
    assert _ended_at(store)


def test_a_call_stuart_took_stays_his(client, make_store, frozen_clock, stuart_told):
    store = _after_hours_stuart_call(client, make_store, frozen_clock)
    _ended(client, 'completed', duration='95')
    assert store.db.get_call_log_field('CAcustomer1', 'status') == 'ai_answered'
    assert _ended_at(store)
    assert stuart_told == [('CAcustomer1', 'completed')]


def test_sync_sets_the_call_status_callback_on_numbers_without_it(make_store, monkeypatch):
    from rinq.services.twilio_service import TwilioService
    make_store()   # a tenant request context to sync into
    updated = {}

    def number(sid, phone, callback):
        n = SimpleNamespace(sid=sid, phone_number=phone, friendly_name=phone,
                            status_callback=callback)
        n.update = lambda **kw: updated.setdefault(phone, kw)
        return n

    good = 'https://tina.test/api/voice/call-status'
    numbers = [number('PN1', '+61244181100', None),     # a store line, never set
               number('PN2', '+61261059636', good)]     # already right
    fake = SimpleNamespace(incoming_phone_numbers=SimpleNamespace(list=lambda **kw: numbers))
    monkeypatch.setattr(TwilioService, 'client', property(lambda self: fake))

    assert TwilioService().sync_phone_numbers()['success']
    assert updated == {'+61244181100': {'status_callback': good,
                                         'status_callback_method': 'POST'}}
