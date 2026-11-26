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


def _whole_team_rang_out(client, store, sid='CAcustomer1'):
    r = client.post(f'/api/voice/queue/{store.queue_id}/rejected-voicemail',
                    data={'CallSid': sid, 'From': CALLER, 'To': store.number})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_whole_team_ringing_out_after_a_hand_back_is_voicemail_not_stuart(client, make_store):
    # The second way out of the queue: every phone rang out or rejected.
    # It sent callers to Stuart too, so a handed-back caller met him again.
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store)
    _wait(client, store)
    _hold_runs_out(client, store)
    _hand_back(client, store)
    twiml = _whole_team_rang_out(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def test_whole_team_ringing_out_on_an_ordinary_call_still_gets_stuart(client, make_store):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store, sid='CAother')
    _wait(client, store, sid='CAother')
    assert ROSIE_ANSWER_URL in _whole_team_rang_out(client, store, sid='CAother')


def test_asked_to_wait_hand_back_also_goes_back_in_the_queue(client, make_store):
    # Meredith (#460): "I'd really rather wait please."
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store)
    _wait(client, store)
    _hold_runs_out(client, store)
    r = client.post(f'/api/voice/stuart-handback?queue_id={store.queue_id}&reason=asked_to_wait',
                    data={'CallSid': 'CAcustomer1', 'From': CALLER, 'To': store.number})
    assert f'>queue_{store.queue_id}</Enqueue>' in r.get_data(as_text=True)
    assert store.db.get_queued_call_by_sid('CAcustomer1')['transfer_type'] == 'stuart'


# ---- handed back after closing time (review 2026-11-26, F6)
# The call started while the store was open (so Stuart could hand back), but
# closing time passed while the caller was with him. Waiting out the whole
# queue for a team that has gone home only delays voicemail - go straight there.

def _rang_before_close_handed_back_after(client, make_store, frozen_clock, **store_kw):
    from datetime import datetime
    from conftest import STORE_HOURS
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist', **store_kw).with_staff(PHIL)
    frozen_clock.set(datetime(2026, 10, 28, 16, 25))     # Wed, open till 4:30
    ring(client, store)
    _wait(client, store)
    _hold_runs_out(client, store)
    frozen_clock.set(datetime(2026, 10, 28, 16, 35))     # closed now
    return store


def test_hand_back_after_closing_goes_straight_to_voicemail(client, make_store, outside,
                                                            frozen_clock):
    store = _rang_before_close_handed_back_after(client, make_store, frozen_clock)
    rung_before = len(outside.calls_created)
    twiml = _hand_back(client, store)
    assert '<Enqueue' not in twiml
    assert '<Record' in twiml and '/api/voice/voicemail' in twiml
    assert 'closed' in twiml.lower()
    assert ROSIE_ANSWER_URL not in twiml
    assert len(outside.calls_created) == rung_before      # nobody rung after hours


def test_hand_back_after_closing_never_returns_to_stuart(client, make_store, frozen_clock):
    # Even where after-hours calls normally go to Stuart - he just handed them back
    store = _rang_before_close_handed_back_after(client, make_store, frozen_clock,
                                                 closed_action='ai_receptionist')
    twiml = _hand_back(client, store)
    assert ROSIE_ANSWER_URL not in twiml and '<Enqueue' not in twiml
    assert '<Record' in twiml


def test_asked_to_wait_after_closing_also_goes_to_voicemail(client, make_store, frozen_clock):
    store = _rang_before_close_handed_back_after(client, make_store, frozen_clock)
    r = client.post(f'/api/voice/stuart-handback?queue_id={store.queue_id}&reason=asked_to_wait',
                    data={'CallSid': 'CAcustomer1', 'From': CALLER, 'To': store.number})
    twiml = r.get_data(as_text=True)
    assert '<Enqueue' not in twiml and '<Record' in twiml


def test_hand_back_just_before_closing_still_waits_for_the_team(client, make_store, frozen_clock):
    from datetime import datetime
    store = _rang_before_close_handed_back_after(client, make_store, frozen_clock)
    frozen_clock.set(datetime(2026, 10, 28, 16, 30))     # the closing minute still counts
    assert f'>queue_{store.queue_id}</Enqueue>' in _hand_back(client, store)
