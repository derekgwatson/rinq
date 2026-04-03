"""
Standalone bot URL resolver for Watson integrations.

In bot-team, get_bot_url() looks up ports via Chester's registry.
Here, URLs are configured via environment variables:
  WATSON_PETER_URL=https://peter.watsonblinds.com.au
  WATSON_SADIE_URL=https://sadie.watsonblinds.com.au
  etc.
"""

import os


def get_bot_url(bot_name: str) -> str:
    """Get the URL for a Watson bot by name.

    Reads from WATSON_{NAME}_URL env var.
    Falls back to localhost with a conventional port (will fail gracefully).
    """
    env_key = f"WATSON_{bot_name.upper()}_URL"
    url = os.environ.get(env_key, '')
    if url:
        return url.rstrip('/')
    # Fallback — will fail gracefully in try/except
    return f"http://localhost:0/{bot_name}"
