"""
Thin FRED API client.

Verified against the official FRED API docs (fred.stlouisfed.org/docs/api):
  - fred/series                returns {"seriess": [{id, title, frequency,
                               frequency_short, units, seasonal_adjustment_short,
                               last_updated, ...}]}
  - fred/series/observations   returns {"count", "offset", "limit",
                               "observations": [{realtime_start, realtime_end,
                               date, value}]}; limit max 100000
  - realtime_start / realtime_end default to today's date
  - errors: JSON {"error_code", "error_message"} with an HTTP status code
  - rate limit: 120 requests per minute, otherwise HTTP 429

Design choices
  - Returns the raw response bytes alongside the parsed JSON, so the caller
    can snapshot exactly what was received before parsing.
  - The API key is added at send time and redacted from every URL, params
    dict and error message this module returns.
  - Throttles below the documented limit and retries only on 429/5xx.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

import requests

BASE_URL = "https://api.stlouisfed.org/fred/"
MIN_INTERVAL_S = 0.55          # ~109 requests/min, under the 120/min limit
RETRY_STATUSES = {429, 500, 502, 503, 504}
REDACTED = "***REDACTED***"


class FredError(Exception):
    def __init__(self, message: str, status: int | None = None, body: bytes = b""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class FredResponse:
    endpoint: str
    params: dict          # API key redacted
    status: int
    body: bytes           # exact bytes received
    payload: dict         # parsed JSON


class FredClient:
    def __init__(self, api_key: str, *, session: requests.Session | None = None,
                 min_interval: float = MIN_INTERVAL_S, max_retries: int = 3,
                 timeout: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        if not api_key:
            raise ValueError("FRED_API_KEY is not set (see .env.example)")
        self._key = api_key
        self._session = session or requests.Session()
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def _redact(self, text: str) -> str:
        return text.replace(self._key, REDACTED)

    def _throttle(self) -> None:
        if self._last_call is not None:
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def get(self, endpoint: str, params: dict) -> FredResponse:
        public = {k: v for k, v in params.items() if k != "api_key"}
        public["file_type"] = "json"
        send = {**public, "api_key": self._key}
        url = BASE_URL + endpoint

        last_error: FredError | None = None
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(url, params=send, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = FredError(f"network error on {endpoint}: {self._redact(str(exc))}")
                self._sleep(2 ** attempt)
                continue

            body = resp.content
            if resp.status_code == 200:
                try:
                    payload = json.loads(body)
                except ValueError as exc:
                    raise FredError(f"{endpoint}: response is not JSON ({exc})",
                                    status=200, body=body) from None
                return FredResponse(endpoint, public, 200, body, payload)

            message = self._error_message(body) or resp.reason or "unknown error"
            last_error = FredError(f"{endpoint} HTTP {resp.status_code}: {message}",
                                   status=resp.status_code, body=body)
            if resp.status_code not in RETRY_STATUSES:
                raise last_error
            self._sleep(2 ** attempt * (5 if resp.status_code == 429 else 1))

        raise last_error  # retries exhausted

    def _error_message(self, body: bytes) -> str | None:
        try:
            msg = json.loads(body).get("error_message")
        except (ValueError, AttributeError):
            return None
        return self._redact(msg) if msg else None

    # Convenience wrappers -------------------------------------------------

    def series_info(self, series_id: str) -> FredResponse:
        return self.get("series", {"series_id": series_id})

    def observations(self, series_id: str, *, offset: int = 0, limit: int = 100000,
                     realtime_start: str | None = None, realtime_end: str | None = None,
                     observation_start: str | None = None) -> FredResponse:
        params = {"series_id": series_id, "offset": offset, "limit": limit}
        if realtime_start:
            params["realtime_start"] = realtime_start
        if realtime_end:
            params["realtime_end"] = realtime_end
        if observation_start:
            params["observation_start"] = observation_start
        return self.get("series/observations", params)
