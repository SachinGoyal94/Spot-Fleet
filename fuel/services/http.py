"""Shared HTTP plumbing: a single session (proper User-Agent for OSM services)
and a minimal thread-safe rate limiter for the free public geocoders."""

import threading
import time

import requests

from django.conf import settings

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': settings.HTTP_USER_AGENT})

DEFAULT_TIMEOUT = 20


class RateLimiter:
    """Allows at most `qps` calls per second across the whole process."""

    def __init__(self, qps):
        self.min_interval = 1.0 / qps
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._last = now


def get_json(url, params=None, timeout=DEFAULT_TIMEOUT):
    resp = SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
