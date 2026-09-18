"""OpenAI Whisper transcription service.

Config via env var:
    OPENAI_API_KEY=sk-xxx

Used for voicemail transcription as a higher-quality alternative to
Twilio's built-in transcribeCallback (which mangles accents, names, and
numbers). Falls back to Twilio transcription if not configured.

⚠️ There is no fallback once a voicemail has been RECORDED. Twilio only
transcribes a recording if `transcribe="true"` was on the <Record> verb, and
`_emit_voicemail_record` leaves it off whenever a key is present here. So a
Whisper failure means that voicemail has no transcription at all, ever —
which is why `transcribe` reports WHY it failed instead of returning a bare
None. Between 2026-09-10 and 2026-09-18 the account sat out of credit and 254
voicemails were filed reading "(Transcription pending or unavailable)", with
nothing raised to anyone.
"""

import logging
import os
from typing import NamedTuple, Optional

import requests

logger = logging.getLogger(__name__)


class TranscriptionResult(NamedTuple):
    """Outcome of one transcription attempt.

    Exactly one of `text` / `error` is set. `error` is written for a staff
    member reading a voicemail ticket, not for a developer reading a log —
    it completes the sentence "No transcription — ...".

    `fault` separates "we are broken" from "there was nothing to transcribe".
    Only a fault is worth waking somebody for; a silent voicemail is normal and
    must never raise an alarm, or the alarm stops meaning anything.
    """

    text: Optional[str] = None
    error: Optional[str] = None
    fault: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.text)


# Maps what OpenAI says to what a person needs to be told. The credit case is
# first because it is the one that stops every voicemail at once and is fixed
# by a human, not by waiting.
def _describe_failure(response) -> str:
    try:
        payload = response.json().get('error', {}) or {}
    except Exception:
        payload = {}
    code = payload.get('code') or ''
    status = response.status_code

    if code in ('credit_balance_exhausted', 'insufficient_quota') or \
            (status == 429 and 'credit' in (payload.get('message') or '').lower()):
        return "the transcription account has run out of credit"
    if status == 429:
        return "the transcription service is rate-limiting us"
    if status in (401, 403):
        return "the transcription service rejected our key"
    if status >= 500:
        return "the transcription service is having an outage"
    message = payload.get('message') or f"HTTP {status}"
    return f"the transcription service refused the request ({message})"


class WhisperService:
    """Audio transcription using OpenAI's Whisper API."""

    API_URL = 'https://api.openai.com/v1/audio/transcriptions'
    MODEL = 'whisper-1'

    def __init__(self):
        self.api_key = os.environ.get('OPENAI_API_KEY', '')

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def transcribe(self, audio_bytes: bytes, filename: str = 'audio.mp3') -> TranscriptionResult:
        """Transcribe audio bytes via Whisper.

        Returns a TranscriptionResult — `text` on success, `error` (a plain
        sentence fragment) on failure. Never raises.
        """
        if not self.is_configured:
            return TranscriptionResult(error="transcription is not switched on")
        try:
            response = requests.post(
                self.API_URL,
                headers={'Authorization': f'Bearer {self.api_key}'},
                files={'file': (filename, audio_bytes, 'audio/mpeg')},
                data={'model': self.MODEL},
                timeout=60,
            )
        except requests.Timeout:
            logger.warning("Whisper transcription failed: timed out")
            return TranscriptionResult(error="the transcription service did not answer in time")
        except Exception as e:
            logger.warning(f"Whisper transcription failed: {e}")
            return TranscriptionResult(error="we could not reach the transcription service")

        if response.status_code != 200:
            reason = _describe_failure(response)
            # The body carries the actual cause; raise_for_status() used to
            # throw it away, which is how "no credit" read as a bare 429.
            logger.error(
                f"Whisper transcription failed: {response.status_code} — {reason} "
                f"(body: {response.text[:300]})"
            )
            return TranscriptionResult(error=reason)

        try:
            text = (response.json().get('text') or '').strip()
        except Exception as e:
            logger.warning(f"Whisper returned an unreadable response: {e}")
            return TranscriptionResult(error="the transcription service sent back something unreadable")

        if not text:
            # A genuinely silent or unintelligible message — not a fault.
            return TranscriptionResult(error="there was no speech to transcribe", fault=False)
        return TranscriptionResult(text=text)


_whisper_service: Optional[WhisperService] = None


def get_whisper_service() -> WhisperService:
    """Get or create the WhisperService singleton."""
    global _whisper_service
    if _whisper_service is None:
        _whisper_service = WhisperService()
    return _whisper_service
