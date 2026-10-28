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


def _voicemail(client, to, sid='REvm1', text='Hi, ringing about my blinds'):
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
    assert t['attachments'][0]['filename'] == 'voicemail_REvm1.mp3'


def test_the_ticket_number_is_kept_on_the_recording(client, make_store, tickets):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='ticket')
    _voicemail(client, store.number)
    with store.db._get_conn() as conn:
        row = conn.execute("SELECT * FROM recording_log WHERE recording_sid = 'REvm1'").fetchone()
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
    _voicemail(client, '+61261000000', sid='REbare')
    assert tickets == []
    with store.db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recording_log WHERE recording_sid = 'REbare'"
                            ).fetchone()[0] == 1


def test_no_transcription_says_so_on_the_ticket(client, make_store, tickets):
    store = make_store()
    _set_box(store, store.vm_id, routing_type='ticket')
    _voicemail(client, store.number, text='')
    assert 'Transcription pending or unavailable' in tickets[0]['description']
