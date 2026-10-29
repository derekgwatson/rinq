"""Audrey (bot-team call analysis) reads every recording through a bot-only
feed, and fetches audio without caching it here (Derek 2026-10-29)."""

import pytest

KEY = 'test-bot-key'
PROXIED = {'X-Forwarded-For': '1.2.3.4'}


@pytest.fixture
def recordings(app, make_store):
    store = make_store()
    store.tenant_id = 'watson'
    with store.db._get_conn() as conn:
        for sid, kind, drive in [('RE1', 'inbound', 'D1'), ('RE2', 'voicemail', 'D2'),
                                 ('RE3', 'outbound', None)]:
            conn.execute("INSERT INTO recording_log (recording_sid, call_sid, call_type, "
                         "duration_seconds, drive_file_id) VALUES (?, ?, ?, 60, ?)",
                         (sid, 'CA' + sid, kind, drive))
        conn.execute("INSERT INTO call_log (call_sid, call_type, queue_name, direction, "
                     "from_number, to_number, status) VALUES ('CARE1', 'ai_receptionist', "
                     "'Shoalhaven', 'inbound', '+61400111222', '+61244181100', 'ai_answered')")
        conn.commit()
    return store


def test_feed_leaves_out_voicemail_and_says_whose_store(client, recordings, monkeypatch):
    monkeypatch.setenv('BOT_API_KEY', KEY)
    r = client.get(f'/api/recordings/feed?tenant={recordings.tenant_id}',
                   headers={**PROXIED, 'X-API-Key': KEY})
    assert r.status_code == 200, r.get_data(as_text=True)
    rows = r.get_json()['recordings']
    assert [x['recording_sid'] for x in rows] == ['RE1', 'RE3']
    assert rows[0]['queue_name'] == 'Shoalhaven' and rows[0]['is_stuart'] == 1
    assert rows[0]['has_audio'] == 1 and rows[1]['has_audio'] == 0

    after = client.get(f'/api/recordings/feed?tenant={recordings.tenant_id}&after_id={rows[0]["id"]}',
                       headers={**PROXIED, 'X-API-Key': KEY}).get_json()
    assert [x['recording_sid'] for x in after['recordings']] == ['RE3']


def test_feed_is_never_open_to_a_logged_in_user(client, recordings):
    with client.session_transaction() as s:
        s['user_id'] = 1
        s['user_email'] = 'someone@watsonblinds.com.au'
    r = client.get(f'/api/recordings/feed?tenant={recordings.tenant_id}', headers=PROXIED)
    assert r.status_code == 403
    r = client.get(f'/api/recordings/RE1/export-audio?tenant={recordings.tenant_id}', headers=PROXIED)
    assert r.status_code == 403


def test_unknown_tenant_is_refused(client, recordings):
    assert client.get('/api/recordings/feed?tenant=nope').status_code == 404


def test_drive_audio_is_streamed_not_cached(client, recordings, monkeypatch):
    from rinq.services.recording_service import recording_service
    monkeypatch.setattr(recording_service, 'fetch_from_drive', lambda fid: b'ID3audio')
    saved = []
    monkeypatch.setattr(recording_service, '_save_recording_locally',
                        lambda *a: saved.append(a) or '/x')
    r = client.get(f'/api/recordings/RE1/export-audio?tenant={recordings.tenant_id}')
    assert r.status_code == 200 and r.data == b'ID3audio'
    assert saved == []
    assert not recording_service.get_recording_file_path('RE1')
