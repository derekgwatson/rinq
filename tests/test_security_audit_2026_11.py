"""Holes from the 2026-11-12 security audit, pinned shut.

1. Twilio signatures were only LOGGED (default 'log' mode), and any X-API-Key
   value at all skipped the check — so anyone on the internet could forge a
   webhook: place calls on our bill, or post a fake "recording ready" whose
   RecordingUrl pointed at their own server, which Rinq then fetched with the
   tenant's Twilio account SID + auth token attached.
2. A '../' RecordingSid wrote files outside the recordings folder.
3. Genuine Twilio callbacks with encoded characters in the URL (%40, %2B)
   failed validation because request.url re-quotes the query string.
4. A caller number broke out of the Call Back button's inline onclick.
"""
from pathlib import Path

import pytest
from twilio.request_validator import RequestValidator

TOKEN = 'test-auth-token-0123456789'
XFF = {'X-Forwarded-For': '203.0.113.9'}   # nginx always adds this
ACCOUNT = 'AC' + 'a' * 32


@pytest.fixture
def store(make_store, monkeypatch):
    monkeypatch.setenv('RINQ_TWILIO_SIGNATURE_MODE', 'enforce')
    monkeypatch.setenv('BOT_API_KEY', 'the-real-bot-key')
    s = make_store()
    from rinq.database.master import get_master_db
    master = get_master_db()
    import sqlite3
    from rinq.config import config
    with sqlite3.connect(config.master_db_path) as c:
        c.execute("UPDATE tenants SET twilio_auth_token = ?, twilio_account_sid = ? WHERE id = 'watson'",
                  (TOKEN, ACCOUNT))
    from flask import g
    g.tenant = master.get_tenant('watson')   # the store fixture's context
    return s


def _outbound_form(store):
    return {'To': '+88213012345', 'From': 'client:nobody', 'CallerId': store.number,
            'CallSid': 'CAforged', 'Called': store.number}


@pytest.mark.parametrize('extra', [{}, {'X-API-Key': 'anything'}, {'X-Twilio-Signature': 'bogus'}])
def test_forged_outbound_call_is_refused(client, store, outside, extra):
    resp = client.post('/api/voice/outbound', data=_outbound_form(store), headers={**XFF, **extra})
    assert resp.status_code == 403
    assert outside.calls_created == []


def test_default_mode_is_enforce(client, store, outside, monkeypatch):
    monkeypatch.delenv('RINQ_TWILIO_SIGNATURE_MODE', raising=False)
    resp = client.post('/api/voice/outbound', data=_outbound_form(store), headers=XFF)
    assert resp.status_code == 403


def test_real_bot_key_still_passes_the_signature_gate(client, store):
    resp = client.post('/api/voice/outbound', data=_outbound_form(store),
                       headers={**XFF, 'X-API-Key': 'the-real-bot-key'})
    assert resp.status_code != 403


def test_genuine_signature_with_encoded_query_is_accepted(client, store):
    """Twilio signs the URL exactly as requested — %40 and %2B included."""
    path = '/api/voice/outbound?agent_email=a%40b.test&n=%2B61400000000'
    url = 'https://tina.test' + path
    form = _outbound_form(store)
    sig = RequestValidator(TOKEN).compute_signature(url, form)
    resp = client.post(path, data=form, base_url='https://tina.test',
                       headers={**XFF, 'X-Twilio-Signature': sig},
                       environ_overrides={'RAW_URI': path})
    assert resp.status_code != 403


def test_recording_fetch_only_ever_goes_to_twilio(app, store, monkeypatch):
    import requests
    from rinq.services.recording_service import recording_service
    seen = []

    class R:
        status_code = 200
        content = b'ID3'
        def raise_for_status(self): pass
    monkeypatch.setattr(requests, 'get', lambda url, **kw: seen.append((url, kw.get('auth'))) or R())
    monkeypatch.setattr(recording_service, '_save_recording_locally', lambda *a: 'x.mp3')
    monkeypatch.setattr(recording_service, '_upload_to_drive', lambda *a, **k: None, raising=False)
    sid = 'RE' + 'b' * 32
    recording_service.process_completed_recording(
        recording_sid=sid, call_sid='CA1', recording_url='https://attacker.invalid/x',
        duration=3, from_number='+61400000000', to_number=store.number, call_type='inbound')
    assert seen, 'no download attempted'
    for url, _auth in seen:
        assert url == f'https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Recordings/{sid}.mp3'


@pytest.mark.parametrize('bad', ['../escaped', '../../audio/hold', 'REnot-hex', ''])
def test_bad_recording_sid_never_downloads_or_writes(app, store, monkeypatch, bad):
    import requests
    from rinq.services.recording_service import recording_service
    calls = []
    monkeypatch.setattr(requests, 'get', lambda *a, **k: calls.append(a))
    out = recording_service.process_completed_recording(
        recording_sid=bad, call_sid='CA1', recording_url='https://attacker.invalid/x',
        duration=3, from_number='+61400000000', to_number=store.number, call_type='inbound')
    assert calls == []
    assert not out.get('success')
    with pytest.raises(ValueError):
        recording_service._save_recording_locally(bad, b'x')


def test_call_back_button_cannot_carry_script():
    page = (Path(__file__).resolve().parents[1] / 'rinq/web/templates/phone.html').read_text()
    assert "'${cb.customer_phone}'" not in page
    assert "dialSafe(cb.customer_phone)" in page
    assert "dialSafe(otherNumber)" in page
