"""Rosie drops out mid-call: the caller hears "trouble on my end" in the
voice they were already talking to - Sophie's clip on a Sophie call,
Stuart's otherwise (Derek 2026-11-27) - then the store's voicemail."""

import pytest

CALLER = '+61400111222'


@pytest.fixture
def rosie_down(monkeypatch):
    import rinq.integrations.watson.ai_receptionist as ai

    class Down:
        @property
        def client(self):
            raise ConnectionError("Rosie restarting")

    monkeypatch.setattr(ai, 'WatsonAIReceptionist', Down)


def _relay_ended(client, store, query=''):
    r = client.post(f'/api/voice/ai-relay-ended{query}',
                    data={'CallSid': 'CAcustomer1', 'From': CALLER, 'To': store.number,
                          'SessionStatus': 'failed'})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


@pytest.mark.parametrize('query, clip', [
    ('?assistant=sophie', 'sophie_lost_connection.mp3'),
    ('?assistant=stuart', 'stuart_lost_connection.mp3'),
    ('', 'stuart_lost_connection.mp3'),                     # Rosie from before this
    ('?assistant=nobody', 'stuart_lost_connection.mp3'),
])
def test_trouble_clip_is_in_the_voice_the_caller_was_hearing(client, make_store, rosie_down,
                                                            query, clip):
    store = make_store(no_answer='ai_receptionist')
    twiml = _relay_ended(client, store, query)
    assert f'/static/{clip}</Play>' in twiml
    assert '<Record' in twiml          # then the voicemail tone
