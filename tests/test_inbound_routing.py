"""Where an incoming store call goes - queue, Stuart (the AI receptionist)
or voicemail.

These are the decisions the Shoalhaven pilot rests on (2026-10-28): Stuart
answers only where a call flow explicitly says AI Receptionist, only in open
hours, and never when Rosie is down. Everything else is voicemail, as it was.
"""

from conftest import ROSIE_ANSWER_URL, ring

PHIL = 'phil@watsonblinds.com.au'
BEN = 'ben@watsonblinds.com.au'


def test_staff_free_caller_waits_in_the_queue(client, make_store):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    twiml = ring(client, store)
    assert '<Enqueue' in twiml
    assert ROSIE_ANSWER_URL not in twiml


def test_everyone_on_a_call_goes_straight_to_stuart(client, make_store, outside):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL, BEN)
    outside.busy = {PHIL, BEN}
    twiml = ring(client, store)
    assert ROSIE_ANSWER_URL in twiml
    assert '<Enqueue' not in twiml


def test_one_person_free_still_rings(client, make_store, outside):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL, BEN)
    outside.busy = {PHIL}
    assert '<Enqueue' in ring(client, store)


def test_voicemail_store_keeps_the_hold_when_everyone_is_busy(client, make_store, outside):
    """Canberra answers ~9 callers a month after a 1min+ wait - the skip is
    for Stuart stores only (Derek, 2026-10-28)."""
    store = make_store(no_answer='voicemail').with_staff(PHIL)
    outside.busy = {PHIL}
    twiml = ring(client, store)
    assert '<Enqueue' in twiml
    assert ROSIE_ANSWER_URL not in twiml


def test_cant_tell_who_is_busy_rings_as_normal(client, make_store, outside):
    """A Twilio hiccup must never send a call Phil could answer to Stuart."""
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    outside.busy_lookup_fails = True
    assert '<Enqueue' in ring(client, store)


def test_nobody_on_duty_stuart_store_goes_to_stuart(client, make_store):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL, active=False)
    assert ROSIE_ANSWER_URL in ring(client, store)


def test_nobody_on_duty_voicemail_store_goes_to_voicemail(client, make_store):
    store = make_store(no_answer='voicemail').with_staff(PHIL, active=False)
    twiml = ring(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Enqueue' not in twiml
    assert '<Record' in twiml


def test_rosie_down_means_voicemail_not_an_error(client, make_store, outside):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL, active=False)
    outside.rosie_up = False
    twiml = ring(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def test_closed_hours_never_reach_stuart(client, make_store, outside):
    """Stuart doesn't know the time of day yet - after hours stays voicemail."""
    store = make_store(no_answer='ai_receptionist', open=False).with_staff(PHIL)
    outside.busy = {PHIL}
    twiml = ring(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Enqueue' not in twiml


def test_a_handover_to_stuart_is_logged_and_recorded(client, make_store, outside, monkeypatch):
    from rinq.services import recording_service as rec
    recorded = []
    monkeypatch.setattr(rec.recording_service, 'start_recording', recorded.append)
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    outside.busy = {PHIL}
    ring(client, store, sid='CArecorded')
    assert recorded == ['CArecorded']
    assert store.db.get_call_log_field('CArecorded', 'call_type') == 'ai_receptionist'


def test_routing_reads_the_call_flows_voicemail_box_not_the_numbers(make_store):
    """phone_numbers grew its own voicemail_destination_id (migration 074) and
    the routing query's pn.* shadowed the call flow's - stores fell through to
    "Please try again later. Goodbye." for 72 calls before this was caught."""
    store = make_store()
    routing = store.db.get_call_routing(store.number)
    assert routing['call_flow']['voicemail_destination_id'] is not None


def test_a_fresh_install_builds_both_databases(make_store):
    """Master migration 002 re-added a column 001 already creates, so a new
    server could never start (found 2026-10-28)."""
    from rinq.database.master import get_master_db
    store = make_store()
    assert get_master_db().get_tenant('watson')
    assert store.db.get_call_routing(store.number)


def _starts_waiting(client, store, sid='CAcustomer1', caller='+61400111222'):
    """First hold-music cycle - this is where Tina records the waiting caller."""
    r = client.post(f'/api/voice/queue/{store.queue_id}/wait',
                    data={'CallSid': sid, 'QueueTime': '0', 'QueuePosition': '1',
                          'From': caller, 'To': store.number})
    assert r.status_code == 200, r.get_data(as_text=True)


def _hold_runs_out(client, store, sid='CAcustomer1', caller='+61400111222'):
    """Staff phones rang out and the max wait passed - Twilio's queue action."""
    r = client.post(f'/api/voice/queue/{store.queue_id}/leave',
                    data={'CallSid': sid, 'QueueResult': 'redirected', 'QueueTime': '68',
                          'From': caller, 'To': store.number})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_hold_running_out_at_a_stuart_store_goes_to_stuart(client, make_store):
    """The usual open-hours miss - staff on duty, nobody picks up. Shoalhaven's
    first two pilot callers went to voicemail this way (2026-10-28)."""
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    assert '<Enqueue' in ring(client, store)
    _starts_waiting(client, store)
    twiml = _hold_runs_out(client, store)
    assert ROSIE_ANSWER_URL in twiml
    assert '<Record' not in twiml


def test_hold_running_out_at_a_voicemail_store_stays_voicemail(client, make_store):
    store = make_store(no_answer='voicemail').with_staff(PHIL)
    ring(client, store)
    _starts_waiting(client, store)
    twiml = _hold_runs_out(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def test_caller_who_asked_for_voicemail_gets_voicemail(client, make_store):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store)
    _starts_waiting(client, store)
    store.db.update_queued_call_status('CAcustomer1', 'voicemail')
    twiml = _hold_runs_out(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def test_hold_running_out_with_rosie_down_is_voicemail(client, make_store, outside):
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store)
    _starts_waiting(client, store)
    outside.rosie_up = False
    twiml = _hold_runs_out(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def _whole_team_rang_out(client, store, sid='CAcustomer1', caller='+61400111222'):
    """Every phone rang out (queue set to give up on no answer) - Tina redirects
    the caller here."""
    r = client.post(f'/api/voice/queue/{store.queue_id}/rejected-voicemail',
                    data={'CallSid': sid, 'From': caller, 'To': store.number})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_team_ringing_out_at_a_stuart_store_goes_to_stuart(client, make_store):
    """Shoalhaven's queue gives up when every phone rings out - that sent 318
    callers to voicemail before the pilot, and would have kept doing it."""
    store = make_store(no_answer='ai_receptionist').with_staff(PHIL)
    ring(client, store)
    _starts_waiting(client, store)
    twiml = _whole_team_rang_out(client, store)
    assert ROSIE_ANSWER_URL in twiml
    assert '<Record' not in twiml


def test_team_ringing_out_at_a_voicemail_store_stays_voicemail(client, make_store):
    store = make_store(no_answer='voicemail').with_staff(PHIL)
    ring(client, store)
    _starts_waiting(client, store)
    twiml = _whole_team_rang_out(client, store)
    assert ROSIE_ANSWER_URL not in twiml
    assert '<Record' in twiml


def test_stuart_queue_says_it_hands_over_to_stuart(make_store):
    """Drives the Queues admin labels - 'Send to Stuart' vs 'Send to voicemail'."""
    from rinq.api.routes import _ai_call_flow_for_queue
    store = make_store(no_answer='ai_receptionist')
    assert _ai_call_flow_for_queue(store.db, store.queue_id) is not None


def test_voicemail_queue_says_voicemail(make_store):
    from rinq.api.routes import _ai_call_flow_for_queue
    store = make_store(no_answer='voicemail')
    assert _ai_call_flow_for_queue(store.db, store.queue_id) is None
