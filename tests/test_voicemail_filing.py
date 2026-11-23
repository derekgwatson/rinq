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


def test_a_box_left_on_the_old_email_type_still_files_a_ticket(client, make_store, tickets):
    # "Email" routing was never built: it logged and dropped the voicemail.
    # It's gone, so a box still carrying that type must not lose messages.
    store = make_store()
    _set_box(store, store.vm_id, routing_type='email', ticket_group_id=42)
    _voicemail(client, store.number)
    assert [t['group_id'] for t in tickets] == [42]


def test_the_admin_page_offers_no_email_option(app):
    from flask import render_template_string
    src = open('rinq/web/templates/admin_voicemail_destinations.html').read()
    src = src.replace('{% extends "base.html" %}', '')
    with app.test_request_context():
        html = render_template_string(
            src, voicemail_destinations=[{'id': 1, 'name': 'B', 'ticket_group_id': 18,
                                          'ticket_assignee_email': 'b@x.com', 'description': None}],
            ticket_groups=[{'id': 18, 'name': 'Showroom'}],
            staff=[{'email': 'b@x.com', 'name_audio_text': 'Brittany Moore', 'extension': '2700'}])
    assert 'Showroom' in html and 'assigned to b@x.com' in html
    assert 'routing_type' not in html and '>Email<' not in html
