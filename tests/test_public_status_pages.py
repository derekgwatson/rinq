"""/health and /info show the internet nothing but up/down (security check 2026-11-24).

/info listed every API endpoint and /health the git build to anyone. Signed-in
users, the bot key and local (no X-Forwarded-For) callers still see it all.
"""
XFF = {'X-Forwarded-For': '203.0.113.9'}   # nginx always adds this


def test_outsider_sees_only_status_on_health(client):
    resp = client.get('/health', headers=XFF)
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'healthy'}


def test_outsider_is_refused_info(client):
    resp = client.get('/info', headers=XFF)
    assert resp.status_code == 401
    assert 'endpoints' not in resp.get_data(as_text=True)


def test_wrong_key_is_an_outsider(client, monkeypatch):
    monkeypatch.setenv('BOT_API_KEY', 'the-real-bot-key')
    assert client.get('/info', headers={**XFF, 'X-API-Key': 'guess'}).status_code == 401


def test_bot_key_sees_everything(client, monkeypatch):
    monkeypatch.setenv('BOT_API_KEY', 'the-real-bot-key')
    hdrs = {**XFF, 'X-API-Key': 'the-real-bot-key'}
    assert 'endpoints' in client.get('/info', headers=hdrs).get_json()
    assert 'bot' in client.get('/health', headers=hdrs).get_json()


def test_local_caller_sees_everything(client):
    assert 'endpoints' in client.get('/info').get_json()
    assert 'bot' in client.get('/health').get_json()
