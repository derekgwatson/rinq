"""Personal voicemail lines (services/personal_voicemail.py, migration 088).

Rohan's mobile has no visual voicemail, so its unanswered calls divert to a
Tina number that only takes messages for him. The promise under test: the
messages reach him (page + email) and nobody else - no call log, report,
recording list, ticket or live-calls view - and an admin can't quietly
redirect them.
"""

from types import SimpleNamespace

import pytest

from conftest import ring

ROHAN = 'rohan.watson@watsonblinds.com.au'
CLARE = 'clare.weinhonig@watsonblinds.com.au'
SID = 'RE11111111111111111111111111111111'


def _login(client, email, role='user'):
    from rinq.database.master import get_master_db
    master = get_master_db()
    user = master.get_or_create_user(email, email.split('@')[0])
    master.add_user_to_tenant('watson', user['id'], role=role)
    with client.session_transaction() as s:
        s['user_id'], s['user_email'], s['tenant_id'] = user['id'], email, 'watson'


@pytest.fixture
def outside_world(monkeypatch):
    """Twilio's audio, Whisper, the caller lookup and email - all stubbed."""
    import requests
    import rinq.integrations as integrations
    import rinq.integrations.openai.whisper as whisper

    sent, tickets = [], []

    class FakeEmail:
        def send_email(self, **kw):
            sent.append(kw)
            return 'msg-1'

    class FakeTickets:
        def create_ticket(self, **kw):
            tickets.append(kw)
            return {'id': 1}

    monkeypatch.setattr(integrations, 'get_email_service', lambda: FakeEmail())
    monkeypatch.setattr(integrations, 'get_ticket_service', lambda: FakeTickets())
    monkeypatch.setattr(integrations, 'get_customer_lookup', lambda: SimpleNamespace(
        find_by_phone=lambda n: {'name': 'Jo Bloggs'} if n == '+61400111222' else None))
    monkeypatch.setattr(whisper, 'get_whisper_service', lambda: SimpleNamespace(
        is_configured=True,
        transcribe=lambda audio, filename: SimpleNamespace(
            ok=True, text='Hi Rohan, call me about the order', error=None, fault=False)))
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(
        status_code=200, content=b'ID3fake-mp3', raise_for_status=lambda: None))
    return SimpleNamespace(emails=sent, tickets=tickets)


@pytest.fixture(autouse=True)
def background_inline(monkeypatch):
    """The download/transcribe/email thread runs inline, so a test sees its
    result as soon as the webhook returns."""
    import rinq.api.personal_voicemail_routes as pvr
    monkeypatch.setattr(pvr, '_spawn', lambda fn: fn())
    monkeypatch.setattr(pvr.time, 'sleep', lambda s: None)
    return pvr


@pytest.fixture
def line(make_store):
    """The store number turned into Rohan's personal line."""
    from rinq.services import personal_voicemail as pv
    store = make_store()
    line_id = pv.create_line(store.db, store.number, ROHAN, 'test')
    return SimpleNamespace(store=store, db=store.db, number=store.number, id=line_id, pv=pv)


def _leave_message(client, number, sid=SID, caller='+61400111222'):
    r = client.post('/api/voice/personal-voicemail/recorded', data={
        'RecordingSid': sid, 'RecordingUrl': f'https://api.twilio.com/x/{sid}',
        'RecordingDuration': '14', 'From': caller, 'To': number, 'CallSid': 'CAvm',
        'ForwardedFrom': '+61438608301', 'AccountSid': 'ACwatson'})
    assert r.status_code == 200, r.get_data(as_text=True)


def _count(db, table):
    with db._get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- the call

def test_a_call_gets_the_greeting_and_the_tone_and_is_never_logged(client, line):
    twiml = ring(client, line.store)
    assert '<Record' in twiml and '/api/voice/personal-voicemail/recorded' in twiml
    assert 'not available' in twiml          # no greeting recorded yet
    assert '<Enqueue' not in twiml
    assert _count(line.db, 'call_log') == 0


def test_the_owners_greeting_is_played(client, line):
    line.pv.save_greeting(line.db, 'watson', line.pv.get_line(line.db, line.id),
                          b'RIFF....WAVEfmt ', 'wav', 'test')
    twiml = ring(client, line.store)
    assert '/personal-greeting/watson/' in twiml and '.wav</Play>' in twiml
    assert 'not available' not in twiml
    url = twiml.split('<Play>')[1].split('</Play>')[0]
    path = url.split('localhost', 1)[-1] if 'localhost' in url else '/' + url.split('/', 3)[3]
    r = client.get(path)
    assert r.status_code == 200 and r.data.startswith(b'RIFF')


def test_a_guessed_greeting_name_is_refused(client, line):
    assert client.get('/personal-greeting/watson/' + 'a' * 32 + '.wav').status_code == 404
    assert client.get('/personal-greeting/watson/../rinq.db.wav').status_code == 404


def test_the_line_takes_the_number_off_its_call_flow(line):
    assert line.db.get_phone_number_by_number(line.number).get('call_flow_id') is None


# ---------------------------------------------------------------- the message

def test_a_message_is_kept_for_the_owner_and_emailed_only_to_them(client, line, outside_world):
    _leave_message(client, line.number)
    msgs = line.pv.messages(line.db, line.id)
    assert len(msgs) == 1
    m = msgs[0]
    assert m['transcription'] == 'Hi Rohan, call me about the order'
    assert m['caller_name'] == 'Jo Bloggs' and m['forwarded_from'] == '+61438608301'
    assert line.pv.message_audio_path('watson', m)
    assert [e['to'] for e in outside_world.emails] == [ROHAN]
    e = outside_world.emails[0]
    assert e['subject'] == 'Voicemail from Jo Bloggs (0400111222)'
    assert 'Hi Rohan, call me about the order' in e['text_body']
    assert e['attachments'][0]['content_type'] == 'audio/mpeg'
    # ...and nowhere else
    assert outside_world.tickets == []
    assert _count(line.db, 'recording_log') == 0
    assert _count(line.db, 'call_log') == 0


def test_twilio_retrying_keeps_one_message_and_one_email(client, line, outside_world):
    _leave_message(client, line.number)
    _leave_message(client, line.number)
    assert len(line.pv.messages(line.db, line.id)) == 1
    assert len(outside_world.emails) == 1


def test_email_copies_off_means_no_email(client, line, outside_world):
    line.pv.update_line(line.db, line.id, 'test', email_copies=0)
    _leave_message(client, line.number)
    assert outside_world.emails == []
    assert len(line.pv.messages(line.db, line.id)) == 1


def test_a_withheld_caller(client, line, outside_world):
    _leave_message(client, line.number, caller='anonymous')
    assert outside_world.emails[0]['subject'] == 'Voicemail from a withheld number'


# ------------------------------------------- answered at once, finished later

def _status_callback(client, line_id, sid=SID, status='completed'):
    """Twilio's recordingStatusCallback: no To/From, the line is in the URL."""
    r = client.post(f'/api/voice/personal-voicemail/recorded?line={line_id}', data={
        'RecordingSid': sid, 'RecordingStatus': status, 'RecordingDuration': '14',
        'CallSid': 'CAvm', 'AccountSid': 'ACwatson'})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_the_tone_also_asks_twilio_for_a_second_callback(client, line):
    twiml = ring(client, line.store)
    assert f'recordingStatusCallback="http' in twiml
    assert f'/api/voice/personal-voicemail/recorded?line={line.id}"' in twiml


def test_twilio_is_answered_before_the_slow_work(client, line, outside_world,
                                                 background_inline, monkeypatch):
    """Review 2026-11-26 (F4): the download + Whisper + email used to run
    inside Twilio's 15 s callback. Now the reply comes first."""
    jobs = []
    monkeypatch.setattr(background_inline, '_spawn', jobs.append)
    r = client.post('/api/voice/personal-voicemail/recorded', data={
        'RecordingSid': SID, 'RecordingDuration': '14', 'From': '+61400111222',
        'To': line.number, 'CallSid': 'CAvm', 'AccountSid': 'ACwatson'})
    assert '<Hangup/>' in r.get_data(as_text=True)
    assert outside_world.emails == [] and len(jobs) == 1
    jobs[0]()   # the thread - no request context needed
    m = line.pv.messages(line.db, line.id)[0]
    assert m['transcription'] == 'Hi Rohan, call me about the order'
    assert [e['to'] for e in outside_world.emails] == [ROHAN]


def test_a_recording_not_ready_is_finished_by_the_status_callback(client, line, outside_world,
                                                                   monkeypatch):
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(status_code=404, content=b''))
    _leave_message(client, line.number)
    assert outside_world.emails == []          # not given up on: no "couldn't fetch" email
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(
        status_code=200, content=b'ID3fake-mp3'))
    _status_callback(client, line.id)
    m = line.pv.messages(line.db, line.id)[0]
    assert m['transcription'] == 'Hi Rohan, call me about the order'
    assert line.pv.message_audio_path('watson', m)
    assert [e['subject'] for e in outside_world.emails] == ['Voicemail from Jo Bloggs (0400111222)']


def test_a_worker_that_died_is_taken_over_by_a_later_callback(client, line, outside_world,
                                                              background_inline, monkeypatch):
    monkeypatch.setattr(background_inline, '_spawn', lambda fn: None)   # killed mid-message
    _leave_message(client, line.number)
    assert outside_world.emails == []
    # Too soon: the first worker might still be going
    monkeypatch.setattr(background_inline, '_spawn', lambda fn: fn())
    _status_callback(client, line.id)
    assert outside_world.emails == []
    with line.db._get_conn() as conn:
        conn.execute("UPDATE personal_voicemails SET processing_started_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
    _leave_message(client, line.number)        # Twilio's next delivery
    assert [e['to'] for e in outside_world.emails] == [ROHAN]
    assert len(line.pv.messages(line.db, line.id)) == 1


def test_status_callback_first_then_the_action_sends_one_email(client, line, outside_world):
    _status_callback(client, line.id)
    _leave_message(client, line.number)
    assert len(line.pv.messages(line.db, line.id)) == 1
    assert len(outside_world.emails) == 1


def test_no_recording_at_twilio_still_tells_the_owner(client, line, outside_world, monkeypatch):
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(status_code=404, content=b''))
    _leave_message(client, line.number)
    _status_callback(client, line.id, status='absent')
    assert len(outside_world.emails) == 1
    assert 'Please play the recording' in outside_world.emails[0]['text_body']


def test_a_deleted_message_is_not_brought_back_by_a_late_callback(client, line, outside_world):
    _leave_message(client, line.number)
    m = line.pv.messages(line.db, line.id)[0]
    line.pv.delete_message(line.db, 'watson', m)
    _status_callback(client, line.id)
    assert line.pv.messages(line.db, line.id) == []
    assert len(outside_world.emails) == 1


# ---------------------------------------------------------------- who sees it

@pytest.fixture
def twilio_ready(monkeypatch):
    # My Numbers shows a "set up Twilio" page unless the account is configured
    import rinq.web.routes as web_routes
    monkeypatch.setattr(web_routes, 'get_twilio_service',
                        lambda: SimpleNamespace(is_configured=True))


def test_the_owner_sees_their_messages_on_my_numbers(client, line, outside_world, twilio_ready):
    _leave_message(client, line.number)
    _login(client, ROHAN)
    html = client.get('/').get_data(as_text=True)
    assert 'My voicemail' in html and 'Hi Rohan, call me about the order' in html
    assert 'Jo Bloggs (0400111222)' in html
    msg = line.pv.messages(line.db, line.id)[0]
    r = client.get(f'/my-voicemail/message/{msg["id"]}/audio')
    assert r.status_code == 200 and r.data == b'ID3fake-mp3'


def test_nobody_else_sees_or_plays_them_not_even_an_admin(client, line, outside_world, twilio_ready):
    _leave_message(client, line.number)
    msg = line.pv.messages(line.db, line.id)[0]
    _login(client, CLARE, role='admin')
    home = client.get('/').get_data(as_text=True)
    assert 'My Queues' in home and 'My voicemail' not in home
    assert client.get(f'/my-voicemail/message/{msg["id"]}/audio').status_code == 404
    assert client.post(f'/my-voicemail/message/{msg["id"]}/delete').status_code == 404
    admin_page = client.get('/admin/personal-voicemail').get_data(as_text=True)
    assert ROHAN in admin_page or 'rohan.watson' in admin_page
    assert 'call me about the order' not in admin_page


def test_the_owner_can_delete_a_message_for_good(client, line, outside_world):
    _leave_message(client, line.number)
    msg = line.pv.messages(line.db, line.id)[0]
    path = line.pv.message_audio_path('watson', msg)
    _login(client, ROHAN)
    assert client.post(f'/my-voicemail/message/{msg["id"]}/delete').status_code == 200
    assert line.pv.messages(line.db, line.id) == []
    import os
    assert not os.path.exists(path)
    # A late Twilio retry of the same recording doesn't bring it back
    _leave_message(client, line.number)
    assert line.pv.messages(line.db, line.id) == []


def test_the_owner_records_a_greeting(client, line):
    import io
    _login(client, ROHAN)
    wav = b'RIFF\x24\x00\x00\x00WAVEfmt ' + b'\x00' * 40
    r = client.post('/my-voicemail/greeting',
                    data={'greeting': (io.BytesIO(wav), 'greeting.wav')},
                    content_type='multipart/form-data')
    assert r.status_code == 200 and r.get_json()['success']
    assert '/personal-greeting/watson/' in ring(client, line.store)


def test_a_non_audio_greeting_is_refused(client, line):
    import io
    _login(client, ROHAN)
    r = client.post('/my-voicemail/greeting',
                    data={'greeting': (io.BytesIO(b'<html>nope</html>'), 'x.wav')},
                    content_type='multipart/form-data')
    assert r.status_code == 400


def test_someone_without_a_line_cannot_use_the_owner_routes(client, line):
    _login(client, CLARE, role='admin')
    assert client.post('/my-voicemail/greeting/clear').status_code == 404
    assert client.post('/my-voicemail/email-copies', data={'on': '0'}).status_code == 404


# ---------------------------------------------------------------- admin

def test_an_admin_change_emails_the_owner(client, line, outside_world):
    _login(client, CLARE, role='admin')
    r = client.post(f'/admin/personal-voicemail/{line.id}/update',
                    data={'phone_number': line.number, 'owner_email': CLARE})
    assert r.status_code == 302
    assert {e['to'] for e in outside_world.emails} == {ROHAN, CLARE}
    assert all('changed your personal voicemail' in e['text_body'] for e in outside_world.emails)


def test_removing_a_line_needs_the_number_typed(client, line, outside_world):
    _login(client, CLARE, role='admin')
    client.post(f'/admin/personal-voicemail/{line.id}/delete', data={'confirm': 'yes'})
    assert line.pv.get_line(line.db, line.id)
    client.post(f'/admin/personal-voicemail/{line.id}/delete', data={'confirm': line.number})
    assert line.pv.get_line(line.db, line.id) is None
    assert [e['to'] for e in outside_world.emails] == [ROHAN]


def test_a_manager_cannot_open_the_admin_page(client, line):
    _login(client, 'chris.savage@watsonblinds.com.au', role='manager')
    assert client.get('/admin/personal-voicemail').status_code in (302, 403)


# ---------------------------------------------------------------- Tina-wide checks

def test_the_monday_cross_check_leaves_the_line_out(line):
    from datetime import date, datetime, timezone
    from rinq.services import cross_check
    call = SimpleNamespace(sid='CA' + 'f' * 32, direction='inbound', to=line.number,
                           _from='+61400111222', status='completed',
                           start_time=datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc),
                           duration='20', parent_call_sid=None)
    fake = SimpleNamespace(calls=SimpleNamespace(list=lambda **k: [call], stream=lambda **k: iter([call])))
    import rinq.services.twilio_service as ts
    orig = ts.twilio_list
    ts.twilio_list = lambda resource, **k: [call] if resource is fake.calls else []
    try:
        result = cross_check.run(line.db, fake, date(2026, 11, 23), date(2026, 11, 29))
    finally:
        ts.twilio_list = orig
    assert result['incoming_calls'] == 0


def test_live_calls_hide_a_caller_leaving_a_message(line, monkeypatch):
    from rinq.services import live_calls
    leg = SimpleNamespace(sid='CA' + 'e' * 32, status='in-progress', to=line.number,
                          _from='+61400111222', direction='inbound', start_time=None,
                          parent_call_sid=None)
    monkeypatch.setattr(live_calls, '_live_twilio_legs', lambda client: [leg])
    monkeypatch.setattr(live_calls, '_held_conferences', lambda client: set())
    snap = live_calls.build_snapshot(line.db, SimpleNamespace())
    assert snap['twilio_live'] == 0   # not even counted
    assert line.number not in repr(snap) and '0400111222' not in repr(snap)


# ------------------------------- the number's call flow (review 2026-11-26, F3)

def _flashes(client):
    with client.session_transaction() as s:
        return ' | '.join(m for _, m in s.get('_flashes', []))


def _flow_of(db, number):
    return (db.get_phone_number_by_number(number) or {}).get('call_flow_id')


def test_removing_a_line_puts_its_number_back_on_its_call_flow(client, line, outside_world):
    _login(client, CLARE, role='admin')
    client.post(f'/admin/personal-voicemail/{line.id}/delete', data={'confirm': line.number})
    assert _flow_of(line.db, line.number) == line.store.flow_id
    assert 'back on call flow "Shoalhaven"' in _flashes(client)


def test_adding_a_line_on_a_live_store_number_warns(client, make_store, outside_world):
    store = make_store()
    _login(client, CLARE, role='admin')
    page = client.get('/admin/personal-voicemail').get_data(as_text=True)
    assert '(in use: call flow &#34;Shoalhaven&#34;)' in page or '(in use: call flow "Shoalhaven")' in page
    client.post('/admin/personal-voicemail/create',
                data={'phone_number': store.number, 'owner_email': ROHAN})
    assert 'was on call flow "Shoalhaven"' in _flashes(client)


def test_moving_a_line_to_a_store_number_warns_and_gives_the_old_one_back(client, line, outside_world):
    from types import SimpleNamespace as NS
    db = line.db
    flow_id = db.create_call_flow({'name': 'Wagga', 'open_action': 'queue',
                                   'open_queue_id': line.store.queue_id}, 'test')
    db.upsert_phone_number({'sid': 'PNwagga', 'phone_number': '+61269310000', 'friendly_name': 'Wagga',
                            'forward_to': None, 'is_active': 1, 'synced_at': None})
    db.set_phone_number_call_flow('PNwagga', flow_id, 'test')
    wagga = NS(number='+61269310000', flow_id=flow_id)
    _login(client, CLARE, role='admin')
    client.post(f'/admin/personal-voicemail/{line.id}/update',
                data={'phone_number': wagga.number, 'owner_email': ROHAN})
    flashes = _flashes(client)
    assert f'{wagga.number} was on call flow "Wagga"' in flashes
    assert _flow_of(line.db, wagga.number) is None
    assert _flow_of(line.db, line.number) == line.store.flow_id      # Shoalhaven back
    # ...and removing it now gives Wagga back too
    client.post(f'/admin/personal-voicemail/{line.id}/delete', data={'confirm': wagga.number})
    assert _flow_of(line.db, wagga.number) == wagga.flow_id


def test_with_no_flow_recorded_nothing_is_guessed(client, line, outside_world):
    """A line made before the previous flow was remembered: the number is
    left with no call flow and the admin is told, rather than Tina picking one."""
    with line.db._get_conn() as conn:
        conn.execute("UPDATE personal_voicemail_lines SET previous_call_flow_id = NULL, "
                     "previous_voicemail_destination_id = NULL")
        conn.commit()
    _login(client, CLARE, role='admin')
    client.post(f'/admin/personal-voicemail/{line.id}/delete', data={'confirm': line.number})
    assert _flow_of(line.db, line.number) is None
    assert 'nothing recorded what it was on before' in _flashes(client)
