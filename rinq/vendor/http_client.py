import os
from urllib.parse import urljoin

import requests

BOT_API_KEY = os.getenv("BOT_API_KEY", "")


class BotHttpClient:
    """
    Simple HTTP client for talking to other bots.

    Always sends X-API-Key header.
    """

    def __init__(self, base_url: str, timeout: int = 10):
        if not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
        self.timeout = timeout

    def _headers(self) -> dict:
        headers: dict = {}

        # Look up the key at call time, not import time
        api_key = os.getenv("BOT_API_KEY", "")
        if api_key:
            headers["X-API-Key"] = api_key

        return headers

    def get(self, path: str, **kwargs):
        url = urljoin(self.base_url, path.lstrip("/"))

        # Use per-call timeout if provided, otherwise default
        timeout = kwargs.pop("timeout", self.timeout)

        return requests.get(
            url,
            headers=self._headers(),
            timeout=timeout,
            **kwargs,
        )

    def post(self, path: str, json=None, **kwargs):
        url = urljoin(self.base_url, path.lstrip("/"))

        # Use per-call timeout if provided, otherwise default
        timeout = kwargs.pop("timeout", self.timeout)

        return requests.post(
            url,
            headers=self._headers(),
            json=json,
            timeout=timeout,
            **kwargs,
        )

    def patch(self, path: str, json=None, **kwargs):
        url = urljoin(self.base_url, path.lstrip("/"))

        # Use per-call timeout if provided, otherwise default
        timeout = kwargs.pop("timeout", self.timeout)

        return requests.patch(
            url,
            headers=self._headers(),
            json=json,
            timeout=timeout,
            **kwargs,
        )

    def put(self, path: str, json=None, **kwargs):
        url = urljoin(self.base_url, path.lstrip("/"))

        # Use per-call timeout if provided, otherwise default
        timeout = kwargs.pop("timeout", self.timeout)

        return requests.put(
            url,
            headers=self._headers(),
            json=json,
            timeout=timeout,
            **kwargs,
        )

    def delete(self, path: str, **kwargs):
        url = urljoin(self.base_url, path.lstrip("/"))

        # Use per-call timeout if provided, otherwise default
        timeout = kwargs.pop("timeout", self.timeout)

        return requests.delete(
            url,
            headers=self._headers(),
            timeout=timeout,
            **kwargs,
        )
