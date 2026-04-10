"""
Text-to-Speech service for Rinq.

Supports:
- ElevenLabs: Premium quality with Australian accent options
- Google Cloud TTS: Good quality Australian voices (uses service account auth)
"""

import logging
import os
from pathlib import Path
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

# Google Cloud TTS scope
GOOGLE_TTS_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'


class TTSService:
    """Generate audio from text using multiple TTS providers."""

    # ElevenLabs voices - DEFAULT voices available to all accounts (no need to add from library)
    ELEVENLABS_VOICES = {
        # Australian voices (default)
        'cjVigY5qzO86Huf0OWal': {'name': 'Stuart', 'accent': 'Australian', 'gender': 'Male', 'age': 'Middle-aged'},
        # American voices (default)
        '21m00Tcm4TlvDq8ikWAM': {'name': 'Rachel', 'accent': 'American', 'gender': 'Female', 'age': 'Young'},
        'EXAVITQu4vr4xnSDxMaL': {'name': 'Bella', 'accent': 'American', 'gender': 'Female', 'age': 'Young'},
        'ErXwobaYiN019PkySvjV': {'name': 'Antoni', 'accent': 'American', 'gender': 'Male', 'age': 'Young'},
        'pNInz6obpgDQGcFmaJgB': {'name': 'Adam', 'accent': 'American', 'gender': 'Male', 'age': 'Middle-aged'},
        'onwK4e9ZLuTAKqWW03F9': {'name': 'Daniel', 'accent': 'British', 'gender': 'Male', 'age': 'Middle-aged'},
        'XrExE9yKIg1WjnnlVkGX': {'name': 'Matilda', 'accent': 'American', 'gender': 'Female', 'age': 'Young'},
        'pqHfZKP75CvOlQylNhV4': {'name': 'Bill', 'accent': 'American', 'gender': 'Male', 'age': 'Old'},
        'nPczCjzI2devNBz1zQrb': {'name': 'Brian', 'accent': 'American', 'gender': 'Male', 'age': 'Middle-aged'},
    }

    # Google Cloud TTS Australian voices
    GOOGLE_VOICES = {
        # Neural2 voices (highest quality)
        'en-AU-Neural2-A': {'name': 'Neural2-A', 'gender': 'Female', 'quality': 'Neural2'},
        'en-AU-Neural2-B': {'name': 'Neural2-B', 'gender': 'Male', 'quality': 'Neural2'},
        'en-AU-Neural2-C': {'name': 'Neural2-C', 'gender': 'Female', 'quality': 'Neural2'},
        'en-AU-Neural2-D': {'name': 'Neural2-D', 'gender': 'Male', 'quality': 'Neural2'},
        # Wavenet voices (very good quality)
        'en-AU-Wavenet-A': {'name': 'Wavenet-A', 'gender': 'Female', 'quality': 'Wavenet'},
        'en-AU-Wavenet-B': {'name': 'Wavenet-B', 'gender': 'Male', 'quality': 'Wavenet'},
        'en-AU-Wavenet-C': {'name': 'Wavenet-C', 'gender': 'Female', 'quality': 'Wavenet'},
        'en-AU-Wavenet-D': {'name': 'Wavenet-D', 'gender': 'Male', 'quality': 'Wavenet'},
        # Standard voices (good quality, cheapest)
        'en-AU-Standard-A': {'name': 'Standard-A', 'gender': 'Female', 'quality': 'Standard'},
        'en-AU-Standard-B': {'name': 'Standard-B', 'gender': 'Male', 'quality': 'Standard'},
        'en-AU-Standard-C': {'name': 'Standard-C', 'gender': 'Female', 'quality': 'Standard'},
        'en-AU-Standard-D': {'name': 'Standard-D', 'gender': 'Male', 'quality': 'Standard'},
    }

    # Cartesia API settings
    CARTESIA_API_VERSION = '2025-04-16'
    CARTESIA_MODEL = 'sonic-3'

    def __init__(self, config):
        self.config = config
        self.elevenlabs_api_key = getattr(config, 'elevenlabs_api_key', None) or os.environ.get('ELEVENLABS_API_KEY')
        self.cartesia_api_key = getattr(config, 'cartesia_api_key', None) or os.environ.get('CARTESIA_API_KEY')

        # Google Cloud TTS uses service account credentials (shared with other bots)
        self._google_credentials = None
        self._google_credentials_path = os.environ.get(
            'GOOGLE_CREDENTIALS_FILE',
            str(Path(__file__).parent.parent.parent / '.secrets' / 'google' / 'credentials.json')
        )

        # Cache for Cartesia voices (fetched from API)
        self._cartesia_voices_cache = None

    @property
    def elevenlabs_available(self) -> bool:
        return bool(self.elevenlabs_api_key)

    @property
    def cartesia_available(self) -> bool:
        return bool(self.cartesia_api_key)

    @property
    def google_available(self) -> bool:
        """Check if Google TTS is available (service account credentials exist)."""
        return Path(self._google_credentials_path).exists()

    def _get_google_access_token(self) -> str:
        """Get an access token for Google Cloud TTS using service account."""
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        if self._google_credentials is None:
            self._google_credentials = service_account.Credentials.from_service_account_file(
                self._google_credentials_path,
                scopes=[GOOGLE_TTS_SCOPE]
            )

        # Refresh token if expired
        if not self._google_credentials.valid:
            self._google_credentials.refresh(Request())

        return self._google_credentials.token

    def generate_elevenlabs(
        self,
        text: str,
        voice_id: str,
        stability: float = 0.5,
        similarity_boost: float = 0.75,
    ) -> bytes:
        """Generate audio using ElevenLabs TTS."""
        if not self.elevenlabs_api_key:
            raise ValueError("ElevenLabs API key not configured")

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
                headers={
                    'xi-api-key': self.elevenlabs_api_key,
                    'Content-Type': 'application/json',
                    'Accept': 'audio/mpeg',
                },
                json={
                    'text': text,
                    'model_id': 'eleven_multilingual_v2',
                    'voice_settings': {
                        'stability': stability,
                        'similarity_boost': similarity_boost,
                    }
                }
            )
            response.raise_for_status()
            return response.content

    def generate_cartesia(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
    ) -> bytes:
        """Generate audio using Cartesia Sonic 3 TTS."""
        if not self.cartesia_api_key:
            raise ValueError("Cartesia API key not configured")

        with httpx.Client(timeout=60.0) as client:
            body = {
                'model_id': self.CARTESIA_MODEL,
                'transcript': text,
                'voice': {
                    'mode': 'id',
                    'id': voice_id,
                },
                'output_format': {
                    'container': 'mp3',
                    'sample_rate': 44100,
                    'bit_rate': 128000,
                },
                'language': 'en',
            }
            if speed != 1.0:
                body['generation_config'] = {'speed': speed}

            response = client.post(
                'https://api.cartesia.ai/tts/bytes',
                headers={
                    'Cartesia-Version': self.CARTESIA_API_VERSION,
                    'Authorization': f'Bearer {self.cartesia_api_key}',
                    'Content-Type': 'application/json',
                },
                json=body,
            )
            response.raise_for_status()
            return response.content

    def generate_google(
        self,
        text: str,
        voice_name: str = 'en-AU-Neural2-A',
        speaking_rate: float = 1.0,
    ) -> bytes:
        """Generate audio using Google Cloud TTS (via REST API with service account)."""
        if not self.google_available:
            raise ValueError("Google TTS not available - service account credentials not found")

        access_token = self._get_google_access_token()

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                'https://texttospeech.googleapis.com/v1/text:synthesize',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json',
                },
                json={
                    'input': {'text': text},
                    'voice': {
                        'languageCode': 'en-AU',
                        'name': voice_name,
                    },
                    'audioConfig': {
                        'audioEncoding': 'MP3',
                        'speakingRate': speaking_rate,
                    }
                }
            )
            response.raise_for_status()
            data = response.json()

            # Google returns base64-encoded audio
            import base64
            return base64.b64decode(data['audioContent'])

    def get_elevenlabs_voices(self) -> dict:
        """Get ElevenLabs voices - combines hardcoded with API voices."""
        voices = dict(self.ELEVENLABS_VOICES)

        if self.elevenlabs_api_key:
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(
                        'https://api.elevenlabs.io/v1/voices',
                        headers={'xi-api-key': self.elevenlabs_api_key}
                    )
                    response.raise_for_status()
                    data = response.json()

                    for v in data.get('voices', []):
                        voice_id = v['voice_id']
                        labels = v.get('labels', {})
                        voices[voice_id] = {
                            'name': v['name'],
                            'accent': labels.get('accent', 'Unknown'),
                            'gender': labels.get('gender', ''),
                            'age': labels.get('age', ''),
                        }
            except Exception as e:
                logger.warning(f"Failed to fetch ElevenLabs voices: {e}")

        return voices

    def get_elevenlabs_voices_grouped(self) -> dict:
        """Get ElevenLabs voices grouped by accent."""
        voices = self.get_elevenlabs_voices()
        grouped = {}

        for voice_id, info in voices.items():
            accent = info.get('accent', 'Other')
            if accent not in grouped:
                grouped[accent] = []
            grouped[accent].append((voice_id, info))

        # Sort: Australian first, then British, then others
        accent_order = ['Australian', 'British', 'American']
        sorted_grouped = {}

        for accent in accent_order:
            if accent in grouped:
                sorted_grouped[accent] = sorted(grouped[accent], key=lambda x: x[1]['name'])

        for accent in sorted(grouped.keys()):
            if accent not in sorted_grouped:
                sorted_grouped[accent] = sorted(grouped[accent], key=lambda x: x[1]['name'])

        return sorted_grouped

    def get_cartesia_voices(self) -> dict:
        """Get Cartesia voices from API (cached)."""
        if self._cartesia_voices_cache is not None:
            return self._cartesia_voices_cache

        voices = {}
        if not self.cartesia_api_key:
            return voices

        try:
            with httpx.Client(timeout=30.0) as client:
                # Paginate through all voices
                starting_after = None
                while True:
                    params = {'limit': 100}
                    if starting_after:
                        params['starting_after'] = starting_after

                    response = client.get(
                        'https://api.cartesia.ai/voices',
                        headers={
                            'Cartesia-Version': self.CARTESIA_API_VERSION,
                            'Authorization': f'Bearer {self.cartesia_api_key}',
                        },
                        params=params,
                    )
                    response.raise_for_status()
                    data = response.json()

                    for v in data.get('data', []):
                        voices[v['id']] = {
                            'name': v['name'],
                            'description': v.get('description', ''),
                            'gender': v.get('gender', ''),
                            'language': v.get('language', ''),
                        }

                    if not data.get('has_more'):
                        break
                    # Use the last voice ID for pagination
                    voice_list = data.get('data', [])
                    if voice_list:
                        starting_after = voice_list[-1]['id']
                    else:
                        break

            self._cartesia_voices_cache = voices
        except Exception as e:
            logger.warning(f"Failed to fetch Cartesia voices: {e}")

        return voices

    def get_cartesia_voices_grouped(self) -> dict:
        """Get Cartesia voices grouped by language/gender."""
        voices = self.get_cartesia_voices()
        grouped = {}

        for voice_id, info in voices.items():
            gender = info.get('gender', '').capitalize() or 'Other'
            if gender not in grouped:
                grouped[gender] = []
            grouped[gender].append((voice_id, info))

        # Sort voices by name within each group
        for key in grouped:
            grouped[key] = sorted(grouped[key], key=lambda x: x[1]['name'])

        return grouped

    def get_google_voices_grouped(self) -> dict:
        """Get Google voices grouped by quality tier."""
        grouped = {'Neural2 (Best)': [], 'Wavenet': [], 'Standard': []}

        for voice_id, info in self.GOOGLE_VOICES.items():
            quality = info['quality']
            if quality == 'Neural2':
                grouped['Neural2 (Best)'].append((voice_id, info))
            elif quality == 'Wavenet':
                grouped['Wavenet'].append((voice_id, info))
            else:
                grouped['Standard'].append((voice_id, info))

        # Sort by name within each group
        for key in grouped:
            grouped[key] = sorted(grouped[key], key=lambda x: x[1]['name'])

        return grouped


# Singleton instance
_tts_service: Optional[TTSService] = None


def get_tts_service() -> TTSService:
    """Get TTS service singleton."""
    global _tts_service
    if _tts_service is None:
        from rinq.config import config
        _tts_service = TTSService(config)
    return _tts_service


def generate_staff_name_audio(email: str, name: str, extension: str, actor: str) -> dict:
    """Generate a TTS audio clip of a staff member's name.

    Uses the default TTS provider/voice configured in Rinq's settings.
    Saves to a deterministic filename so regeneration overwrites cleanly.

    Returns {'success': True} or {'success': False, 'error': '...'}.
    """
    from rinq.database.db import get_db

    tts = get_tts_service()
    db = get_db()

    # Read default TTS settings
    settings = db.get_tts_settings()
    provider = settings.get('default_provider', 'elevenlabs')
    voice = settings.get('default_voice', '')

    if not voice:
        return {'success': False, 'error': 'No default TTS voice configured'}

    # Build deterministic output path
    from rinq.config import config
    audio_folder = config.base_dir / 'audio'
    audio_folder.mkdir(exist_ok=True)
    filename = f"name_ext_{extension}.mp3"
    output_path = audio_folder / filename

    try:
        # Generate audio of just the name
        if provider == 'elevenlabs':
            if not tts.elevenlabs_available:
                return {'success': False, 'error': 'ElevenLabs API key not configured'}
            audio_bytes = tts.generate_elevenlabs(name, voice_id=voice, stability=0.7)
        elif provider == 'cartesia':
            if not tts.cartesia_available:
                return {'success': False, 'error': 'Cartesia API key not configured'}
            audio_bytes = tts.generate_cartesia(name, voice_id=voice)
        elif provider == 'google':
            if not tts.google_available:
                return {'success': False, 'error': 'Google TTS not available'}
            audio_bytes = tts.generate_google(name, voice_name=voice)
        else:
            return {'success': False, 'error': f'Unknown provider: {provider}'}

        # Write the audio file
        with open(output_path, 'wb') as f:
            f.write(audio_bytes)

        # Update the database
        relative_path = f"/audio/{filename}"
        db.update_staff_name_audio(email, relative_path, name, actor)

        logger.info(f"Generated name audio for {email} ({name}): {filename}")
        return {'success': True}

    except Exception as e:
        logger.warning(f"Failed to generate name audio for {email} ({name}): {e}")
        return {'success': False, 'error': str(e)}
