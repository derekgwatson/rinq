"""TwiML building helpers — audio URLs, say/play, voicemail, closed messages.

Extracted from routes.py to reduce that file's size and make these
helpers testable and reusable.
"""

import logging
from xml.sax.saxutils import escape as xml_escape

from flask import Response

from rinq.config import config
from rinq.database.db import get_db

logger = logging.getLogger(__name__)


def twiml_response(*parts) -> Response:
    """Build a TwiML Response from XML parts.

    Usage:
        return twiml_response('<Say>Hello</Say>')
        return twiml_response(say_or_play('greeting', 'Hello'), '<Hangup/>')
    """
    body = '\n'.join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Response>',
        *parts,
        '</Response>',
    ])
    return Response(body, mimetype='application/xml')


def get_full_audio_url(file_url: str) -> str:
    """Convert a file_url (path or full URL) to a full URL for Twilio."""
    if not file_url:
        return None
    if file_url.startswith('http://') or file_url.startswith('https://'):
        return file_url
    return f"{config.webhook_base_url}{file_url}"


def get_audio_url_by_type(file_type: str) -> str | None:
    """Look up the first audio file of a given type and return its full URL."""
    db = get_db()
    files = db.get_audio_files(file_type=file_type)
    if files and files[0].get('file_url'):
        return get_full_audio_url(files[0]['file_url'])
    return None


def say_or_play(audio_type: str, fallback_text: str, indent: str = '    ') -> str:
    """Return a <Play> tag if custom audio exists for the type, else <Say>."""
    audio_url = get_audio_url_by_type(audio_type)
    if audio_url:
        return f'{indent}<Play>{xml_escape(audio_url)}</Play>'
    return f'{indent}<Say voice="Polly.Nicole">{fallback_text}</Say>'


def build_reopen_twiml(next_open: dict, indent: str = '    ') -> list[str]:
    """Build TwiML snippets announcing when the business reopens.

    Uses recorded audio snippets if available (reopen_prefix, reopen_day_*,
    reopen_time_*), falling back to Polly.Nicole TTS for any missing pieces.
    """
    if not next_open:
        return []

    day_label = next_open['day_label']
    time_spoken = next_open['time']
    time_raw = next_open.get('time_raw', '')

    day_audio_type = f"reopen_day_{day_label.lower().replace(' ', '_')}"
    time_audio_type = f"reopen_time_{time_raw.replace(':', '')}" if time_raw else None

    parts = []

    prefix_url = get_audio_url_by_type('reopen_prefix')
    day_url = get_audio_url_by_type(day_audio_type)
    time_url = get_audio_url_by_type(time_audio_type) if time_audio_type else None

    if prefix_url and day_url and time_url:
        parts.append(f'{indent}<Play>{xml_escape(prefix_url)}</Play>')
        parts.append(f'{indent}<Play>{xml_escape(day_url)}</Play>')
        parts.append(f'{indent}<Play>{xml_escape(time_url)}</Play>')
    else:
        parts.append(f'{indent}<Say voice="Polly.Nicole">We reopen {day_label} at {time_spoken}.</Say>')

    return parts


def build_closed_message_twiml(message_parts: list, next_open: dict | None,
                                db, indent: str = '    ') -> list[str]:
    """Build TwiML from an ordered list of closed message segments.

    Each segment is a dict with a 'type' key:
      - {"type": "audio", "audio_id": 5}  -> <Play> the audio file
      - {"type": "opentime"}              -> opening time (recorded audio or TTS)
      - {"type": "openday"}               -> opening day (recorded audio or TTS)
    """
    if not message_parts:
        return []

    day_label = None
    time_spoken = None
    day_audio_type = None
    time_audio_type = None
    if next_open:
        day_label = next_open['day_label']
        time_spoken = next_open['time']
        time_raw = next_open.get('time_raw', '')
        day_audio_type = f"reopen_day_{day_label.lower().replace(' ', '_')}"
        time_audio_type = f"reopen_time_{time_raw.replace(':', '')}" if time_raw else None

    twiml_parts = []
    for part in message_parts:
        part_type = part.get('type')

        if part_type == 'audio':
            audio_id = part.get('audio_id')
            if audio_id:
                audio = db.get_audio_file(audio_id)
                if audio and audio.get('file_url'):
                    audio_url = get_full_audio_url(audio['file_url'])
                    twiml_parts.append(f'{indent}<Play>{xml_escape(audio_url)}</Play>')

        elif part_type == 'opentime' and next_open:
            time_url = get_audio_url_by_type(time_audio_type) if time_audio_type else None
            if time_url:
                twiml_parts.append(f'{indent}<Play>{xml_escape(time_url)}</Play>')
            elif time_spoken:
                twiml_parts.append(f'{indent}<Say voice="Polly.Nicole">{time_spoken}</Say>')

        elif part_type == 'openday' and next_open:
            if day_label == 'later today':
                continue
            day_url = get_audio_url_by_type(day_audio_type) if day_audio_type else None
            if day_url:
                twiml_parts.append(f'{indent}<Play>{xml_escape(day_url)}</Play>')
            elif day_label:
                twiml_parts.append(f'{indent}<Say voice="Polly.Nicole">{day_label}</Say>')

    return twiml_parts
