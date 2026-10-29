"""Stuart's calls are recorded - after hours too - and the recordings list says
they were his (Derek 2026-10-29).

After hours no one has answered the call when Tina hands it to Stuart, and
Twilio refuses to record a ringing call ("not eligible for recording"), so
every after-hours Stuart call went unrecorded. Then the caller goes via
/api/voice/stuart-record, which starts the recording once the call is
answered and passes them straight on to Stuart.
"""

import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from conftest import ROSIE_ANSWER_URL, STORE_HOURS, ring

PHIL = 'phil@watsonblinds.com.au'


def _redirect(twiml):
    m = re.search(r'<Redirect method="POST">([^<]+)</Redirect>', twiml)
    assert m, twiml
    return m.group(1).replace('&amp;', '&')


def _after_hours_store(make_store, frozen_clock):
    store = make_store(hours=STORE_HOURS, no_answer='ai_receptionist',
                       closed_action='ai_receptionist').with_staff(PHIL)
    frozen_clock.set(datetime(2026, 10, 28, 17, 15))
    return store


def test_a_recordable_call_goes_straight_to_stuart(client, make_store, frozen_clock, outside):
    store = _after_hours_store(make_store, frozen_clock)
    assert _redirect(ring(client, store)).startswith(ROSIE_ANSWER_URL)
    assert outside.recordings_started == ['CAcustomer1']


def test_a_ringing_call_is_recorded_on_the_way_to_stuart(client, make_store, frozen_clock, outside):
    store = _after_hours_store(make_store, frozen_clock)
    outside.recordable = False
    hop = _redirect(ring(client, store))
    assert urlparse(hop).path == '/api/voice/stuart-record'

    # Twilio has answered by the time it follows the Redirect
    outside.recordable = True
    r = client.post(hop.replace('https://tina.test', ''),
                    data={'CallSid': 'CAcustomer1', 'To': store.number,
                          'From': '+61400111222', 'AccountSid': 'ACtest'})
    assert r.status_code == 200
    on_to_stuart = _redirect(r.get_data(as_text=True))
    assert on_to_stuart.startswith(ROSIE_ANSWER_URL)
    assert outside.recordings_started == ['CAcustomer1', 'CAcustomer1']
    # Stuart gets exactly the details he would have had directly
    assert parse_qs(urlparse(on_to_stuart).query) == parse_qs(urlparse(hop).query)
    assert parse_qs(urlparse(on_to_stuart).query)['reason'] == ['closed']


def test_the_extra_step_only_ever_sends_the_caller_to_stuart(client, make_store, outside):
    store = make_store()
    r = client.post('/api/voice/stuart-record?call_sid=CAx&next=https://evil.test/',
                    data={'CallSid': 'CAx', 'To': store.number, 'AccountSid': 'ACtest'})
    assert _redirect(r.get_data(as_text=True)).startswith(ROSIE_ANSWER_URL + '?')


def test_recordings_list_marks_and_filters_stuarts_calls(make_store):
    store = make_store()
    db = store.db
    for sid, kind in (('CAstuart', 'ai_receptionist'), ('CAstaff', None)):
        db.log_call({'call_sid': sid, 'direction': 'inbound', 'from_number': '+61400111222',
                     'to_number': store.number, 'status': 'answered'})
        if kind:
            db.update_call_log(sid, {'call_type': kind, 'status': 'ai_answered'})
        db.log_recording({'recording_sid': f'RE{sid}', 'call_sid': sid, 'call_type': 'inbound',
                          'from_number': '+61400111222', 'duration_seconds': 60,
                          'created_at': '2026-10-29T05:00:00+00:00'})
    every = {r['call_sid']: r['is_stuart'] for r in db.get_recording_log(exclude_voicemail=True)}
    assert every == {'CAstuart': 1, 'CAstaff': 0}
    assert [r['call_sid'] for r in db.get_recording_log(call_type='stuart')] == ['CAstuart']
