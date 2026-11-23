"""Where a recorded voicemail is filed (POST /api/voice/voicemail).

The call flow's voicemail box decides it; a number with no call flow (a
direct-ring line) uses its own box. Nothing leaves the machine: the Twilio
audio download, Whisper and the ticket service are all stubbed.
"""

from types import SimpleNamespace

import pytest


@pytest.fixture
def tickets(monkeypatch):
    """The ticket service, recording every ticket Tina tried to create."""
    import requests
    import rinq.integrations as integrations
    import rinq.integrations.openai.whisper as whisper

    made = []

    class FakeTickets:
        def create_ticket(self, **kwargs):
            made.append(kwargs)
            return {'id': 700000 + len(made)}

    monkeypatch.setattr(integrations, 'get_ticket_service', lambda: FakeTickets())
    monkeypatch.setattr(whisper, 'get_whisper_service',
                        lambda: SimpleNamespace(is_configured=False))
    monkeypatch.setattr(requests, 'get', lambda *a, **k: SimpleNamespace(
        status_code=200, content=b'ID3fake-mp3', raise_for_status=lambda: None))
    return made


# Real Twilio recording SIDs are 'RE' + 32 hex — Rinq refuses anything else
# since the 2026-11-12 security audit.
def _voicemail(client, to, sid='RE11111111111111111111111111111111', text='Hi, ringing about my blinds'):
    r = client.post('/api/voice/voicemail', data={
        'RecordingUrl': f'https://api.twilio.com/Recordings/{sid}', 'RecordingSid': sid,
        'RecordingDuration': '12', 'From': '+61400111222', 'To': to, 'CallSid': f'CA{sid}',
        'TranscriptionText': text, 'AccountSid': 'ACwatson'})
    assert r.status_code == 200, r.get_data(as_text=True)


def _set_box(store, vm_id, **fields):
    sets = ', '.join(f'{k} = ?' for k in fields)
    with store.db._get_conn() as conn:
        conn.execute(f"UPDATE voicemail_destinations SET {sets} WHERE id = ?",
                     (*fields.values(), vm_id))
        conn.commit()


def test_voicemail_becomes_a_ticket_for_the_flows_team(client, make_store, tickets):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='ticket', ticket_group_id=42)
    _voicemail(client, store.number)
    assert len(tickets) == 1
    t = tickets[0]
    assert t['group_id'] == 42
    assert 'Hi, ringing about my blinds' in t['description']
    assert t['attachments'][0]['filename'] == 'voicemail_RE11111111111111111111111111111111.mp3'


def test_the_ticket_number_is_kept_on_the_recording(client, make_store, tickets):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='ticket')
    _voicemail(client, store.number)
    with store.db._get_conn() as conn:
        row = conn.execute("SELECT * FROM recording_log WHERE recording_sid = 'RE11111111111111111111111111111111'").fetchone()
    assert dict(row).get('ticket_id') in (700001, '700001')


def test_twilio_retrying_the_webhook_makes_one_ticket(client, make_store, tickets):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='ticket')
    _voicemail(client, store.number)
    _voicemail(client, store.number)
    assert len(tickets) == 1


def test_direct_ring_number_uses_its_own_box(client, make_store, tickets):
    store = make_store()
    direct_box = store.db.create_voicemail_destination(
        {'name': 'Derek direct', 'routing_type': 'ticket', 'ticket_group_id': 7}, 'test')
    store.db.upsert_phone_number({'sid': 'PNdirect', 'phone_number': '+61261926884',
                                  'friendly_name': 'Direct', 'forward_to': None,
                                  'is_active': 1, 'synced_at': None})
    store.db.update_phone_number_voicemail_destination('PNdirect', direct_box, 'test')
    _voicemail(client, '+61261926884')
    assert [t['group_id'] for t in tickets] == [7]


def test_no_box_files_no_ticket_but_keeps_the_recording(client, make_store, tickets):
    store = make_store()
    store.db.upsert_phone_number({'sid': 'PNbare', 'phone_number': '+61261000000',
                                  'friendly_name': 'Bare', 'forward_to': None,
                                  'is_active': 1, 'synced_at': None})
    _voicemail(client, '+61261000000', sid='REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')
    assert tickets == []
    with store.db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recording_log WHERE recording_sid = 'REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'"
                            ).fetchone()[0] == 1


def test_no_transcription_says_so_on_the_ticket(client, make_store, tickets):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='ticket')
    _voicemail(client, store.number, text='')
    assert 'Transcription pending or unavailable' in tickets[0]['description']


def test_a_box_with_a_person_assigns_the_ticket_to_them(client, make_store, tickets):
    # A person's own line (Brittany's 02 6192 6884): her voicemails must reach
    # HER, not sit in a group nobody watches for them.
    store = make_store()
    box = store.db.create_voicemail_destination(
        {'name': 'Brittany Moore', 'routing_type': 'ticket', 'ticket_group_id': 18,
         'ticket_assignee_email': 'brittany.moore@watsonblinds.com.au'}, 'test')
    store.db.upsert_phone_number({'sid': 'PNbritt', 'phone_number': '+61261926884',
                                  'friendly_name': 'Britt', 'forward_to': None,
                                  'is_active': 1, 'synced_at': None})
    store.db.update_phone_number_voicemail_destination('PNbritt', box, 'test')
    _voicemail(client, '+61261926884', sid='REcccccccccccccccccccccccccccccccc')
    assert [(t['group_id'], t['assignee_email']) for t in tickets] == [
        (18, 'brittany.moore@watsonblinds.com.au')]


def test_a_box_without_a_person_assigns_nobody(client, make_store, tickets):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='ticket', ticket_group_id=42)
    _voicemail(client, store.number)
    assert tickets[0]['assignee_email'] is None


def test_zelda_is_sent_the_assignee(monkeypatch):
    from rinq.integrations.zelda.tickets import ZeldaTicketService
    sent = {}

    class FakeClient:
        def post(self, path, json=None, timeout=None):
            sent.update(json)
            return type('R', (), {'status_code': 201, 'json': lambda self: {'ticket': {'id': 5}}})()

    svc = ZeldaTicketService()
    svc._client = FakeClient()
    svc.create_ticket(subject='s', description='d', assignee_email='a@b.com')
    assert sent['assignee_email'] == 'a@b.com'


def test_the_assignee_can_be_changed_and_cleared(make_store):
    store = make_store()
    store.db.update_voicemail_destination(
        store.vm_id, {'ticket_assignee_email': 'brittany.moore@watsonblinds.com.au'}, 'test')
    assert store.db.get_voicemail_destination(store.vm_id)['ticket_assignee_email'] == \
        'brittany.moore@watsonblinds.com.au'
    store.db.update_voicemail_destination(store.vm_id, {'ticket_assignee_email': None}, 'test')
    assert store.db.get_voicemail_destination(store.vm_id)['ticket_assignee_email'] is None


def test_an_email_box_with_no_address_still_files_a_ticket(client, make_store, tickets, emails):
    # A box carrying the email type but no address must never lose a message.
    store = make_store()
    _set_box(store, store.vm_id, routing_type='email', email=None, ticket_group_id=42)
    _voicemail(client, store.number)
    assert [t['group_id'] for t in tickets] == [42]
    assert emails == []


# --- "Email to one person" boxes (Rohan's mobile, 2026-11-23) ---------------

ROHAN = 'rohan.watson@watsonblinds.com.au'


@pytest.fixture
def emails(monkeypatch):
    """The email service, recording every email Tina sent. Caller lookup is
    stubbed: one known number."""
    import rinq.integrations as integrations

    class Sent(list):
        pass

    sent = Sent()

    class FakeEmail:
        ok = True

        def send_email(self, **kwargs):
            sent.append(kwargs)
            return 'msg-1' if FakeEmail.ok else None

    class FakeLookup:
        def find_by_phone(self, number):
            return {'name': 'Jo Bloggs'} if number == '+61400111222' else None

    sent.fake = FakeEmail
    monkeypatch.setattr(integrations, 'get_email_service', lambda: FakeEmail())
    monkeypatch.setattr(integrations, 'get_customer_lookup', lambda: FakeLookup())
    return sent


def _activity(store, action):
    with store.db._get_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT details FROM activity_log WHERE action = ?", (action,)).fetchall()]


def test_an_email_box_emails_only_its_person_and_files_no_ticket(client, make_store, tickets, emails):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='email', email=ROHAN)
    _voicemail(client, store.number)
    assert tickets == []
    assert len(emails) == 1
    e = emails[0]
    assert e['to'] == ROHAN
    assert e['subject'] == 'Voicemail from Jo Bloggs (0400111222)'
    assert 'Hi, ringing about my blinds' in e['text_body']
    assert 'Length: 12 seconds' in e['text_body']
    assert e['attachments'][0]['filename'] == 'voicemail_RE11111111111111111111111111111111.mp3'
    assert e['attachments'][0]['content_type'] == 'audio/mpeg'
    assert _activity(store, 'voicemail_emailed')


def test_an_email_box_says_when_there_is_no_written_message(client, make_store, tickets, emails):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='email', email=ROHAN)
    _voicemail(client, store.number, text='')
    assert "Couldn't write this one out" in emails[0]['text_body']


def test_a_withheld_caller_is_named_as_withheld(client, make_store, tickets, emails):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='email', email=ROHAN)
    r = client.post('/api/voice/voicemail', data={
        'RecordingUrl': 'https://api.twilio.com/Recordings/RE22222222222222222222222222222222',
        'RecordingSid': 'RE22222222222222222222222222222222', 'RecordingDuration': '5',
        'From': 'anonymous', 'To': store.number, 'CallSid': 'CAanon', 'TranscriptionText': 'hi',
        'AccountSid': 'ACwatson'})
    assert r.status_code == 200
    assert emails[0]['subject'] == 'Voicemail from a withheld number'


def test_a_failed_email_is_logged_and_still_files_no_ticket(client, make_store, tickets, emails):
    # Rohan's messages must never land in a shared queue, even when the email fails.
    store = make_store()
    _set_box(store, store.vm_id, routing_type='email', email=ROHAN)
    emails.fake.ok = False
    _voicemail(client, store.number)
    assert tickets == []
    assert _activity(store, 'voicemail_email_failed')


def test_twilio_retrying_the_webhook_sends_one_email(client, make_store, tickets, emails):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='email', email=ROHAN)
    _voicemail(client, store.number)
    _voicemail(client, store.number)
    assert len(emails) == 1


def _render_boxes(app, boxes):
    from flask import render_template_string
    src = open('rinq/web/templates/admin_voicemail_destinations.html').read()
    src = src.replace('{% extends "base.html" %}', '')
    with app.test_request_context():
        return render_template_string(
            src, voicemail_destinations=boxes,
            ticket_groups=[{'id': 18, 'name': 'Showroom'}],
            staff=[{'email': 'b@x.com', 'name_audio_text': 'Brittany Moore', 'extension': '2700'},
                   {'email': ROHAN, 'name_audio_text': 'Rohan Watson', 'extension': '6947'}])


def test_the_admin_page_shows_ticket_and_email_boxes(app):
    html = _render_boxes(app, [
        {'id': 1, 'name': 'B', 'routing_type': 'ticket', 'email': None, 'ticket_group_id': 18,
         'ticket_assignee_email': 'b@x.com', 'description': None},
        {'id': 2, 'name': 'Rohan', 'routing_type': 'email', 'email': ROHAN,
         'ticket_group_id': None, 'ticket_assignee_email': None, 'description': None}])
    assert 'Showroom' in html and 'assigned to b@x.com' in html
    assert f'only to {ROHAN}' in html
    assert 'An email to one person only' in html


def test_an_old_ticket_box_with_an_address_still_reads_as_a_ticket(app):
    # Batemans Bay's box carries bb@ from before tickets; it files tickets.
    html = _render_boxes(app, [
        {'id': 1, 'name': 'BB', 'routing_type': 'ticket', 'email': 'bb@x.com', 'ticket_group_id': 18,
         'ticket_assignee_email': None, 'description': None}])
    assert 'only to bb@x.com' not in html


def test_box_form_email_needs_a_person_and_clears_the_ticket_fields(app):
    from rinq.web.admin_flow_routes import _voicemail_box_fields
    with app.test_request_context(method='POST', data={'routing_type': 'email', 'email': ''}):
        assert _voicemail_box_fields('Rohan') == (None, 'Pick the person this box emails')
    with app.test_request_context(method='POST', data={
            'routing_type': 'email', 'email': ' Rohan.Watson@watsonblinds.com.au ',
            'ticket_group_id': '18'}):
        fields, err = _voicemail_box_fields('Rohan')
    assert err is None
    assert fields['email'] == ROHAN and fields['ticket_group_id'] is None


def test_box_form_ticket_needs_a_group_and_leaves_the_address_alone(app):
    from rinq.web.admin_flow_routes import _voicemail_box_fields
    with app.test_request_context(method='POST', data={'routing_type': 'ticket'}):
        assert _voicemail_box_fields('B')[1] == "Pick the group that gets this box's voicemail tickets"
    with app.test_request_context(method='POST', data={'ticket_group_id': '18'}):
        fields, err = _voicemail_box_fields('B')
    assert fields['routing_type'] == 'ticket' and 'email' not in fields
