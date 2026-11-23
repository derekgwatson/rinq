"""A caller Stuart can't hear goes back to the team (Derek, 2026-11-23).

Elizabeth is hard of hearing. She waited in the Canberra queue, reached
Stuart, said "Hello?" once and hung up on his check-ins. Rosie now ends such a
call (one that never got going, store open) with a <Redirect> here: back into
the store's queue, ringing the team again - and if nobody answers this time,
voicemail. Never back to Stuart, or the caller goes round in a loop.
"""

from conftest import ROSIE_ANSWER_URL, ring

PHIL = 'phil@watsonblinds.com.au'
CALLER = '+61400111222'


def _wait(client, store, sid='CAcustomer1'):
    r = client.post(f'/api/voice/queue/{store.queue_id}/wait',
                    data={'CallSid': sid, 'QueueTime': '0', 'QueuePosition': '1',
                          'From': CALLER, 'To': store.number})
    assert r.status_code == 200, r.get_data(as_text=True)


def _hold_runs_out(client, store, sid='CAcustomer1'):
    r = client.post(f'/api/voice/queue/{store.queue_id}/leave',
                    data={'CallSid': sid, 'QueueResult': 'redirected', 'QueueTime': '68',
                          'From': CALLER, 'To': store.number})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def _hand_back(client, store, sid='CAcustomer1', queue_id=None):
    r = client.post(f'/api/voice/stuart-handback?queue_id={queue_id or store.queue_id}',
                    data={'CallSid': sid, 'From': CALLER, 'To': store.number})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_handed_back_caller_waits_in_their_queue_and_the_team_is_rung(client, make_store, outside):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store)
    _wait(client, store)
    assert ROSIE_ANSWER_URL in _hold_runs_out(client, store)   # first miss -> Stuart
    rung_before = len(outside.calls_created)

    twiml = _hand_back(client, store)
    assert f'>queue_{store.queue_id}</Enqueue>' in twiml
    assert len(outside.calls_created) > rung_before            # the team rings again
    queued = store.db.get_queued_call_by_sid('CAcustomer1')
    assert queued['status'] == 'waiting' and queued['transfer_type'] == 'stuart'


def test_nobody_answering_the_second_time_is_voicemail_not_stuart(client, make_store):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store)
    _wait(client, store)
    _hold_runs_out(client, store)
    _hand_back(client, store)
    _wait(client, store)
    twiml = _hold_runs_out(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def test_an_ordinary_caller_still_goes_to_stuart_when_the_hold_runs_out(client, make_store):
    # The hand-back marker is per call - it mustn't change anyone else's
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store, sid='CAother')
    _wait(client, store, sid='CAother')
    assert ROSIE_ANSWER_URL in _hold_runs_out(client, store, sid='CAother')


def test_an_unknown_queue_goes_to_voicemail(client, make_store):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    twiml = _hand_back(client, store, queue_id=99999)
    assert '<Enqueue' not in twiml
    assert ROSIE_ANSWER_URL not in twiml
