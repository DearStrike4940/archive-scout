from __future__ import annotations

import random
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable, TypeAlias

import httpx
import urllib3

from ..constants import RETRYABLE_STATUS
from ..downloads.rate_limit import FixedRateLimiter, SharedHostGate
from ..events import Stopped
from ..json_codec import JSONDecodeErrors, loads as json_loads
from ..network.transports import (
    ResilientTransport,
    InvalidRangeResponse,
    TransportExhaustedError,
    is_transport_connection_failure,
    is_transport_read_timeout,
    is_transport_timeout,
)
from ..runtime import ensure_frozen_bundle_available, frozen_bundle_error_from_exception, is_missing_frozen_bundle_error
from ..utils import clean_space, cdx_request_url


class TransientRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        timed_out: bool = False,
        read_timed_out: bool = False,
        connection_failed: bool = False,
        splittable: bool = False,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.read_timed_out = bool(read_timed_out)
        self.connection_failed = bool(connection_failed)
        self.timed_out = bool(timed_out or read_timed_out)
        self.splittable = splittable
        self.endpoint = endpoint


class MalformedCDXResponse(TransientRequestError):
    """A successful CDX response whose body cannot be parsed safely."""


class PermanentRequestError(RuntimeError):
    """A deterministic HTTP rejection that should not enter transient retry loops."""

    def __init__(self, message: str, *, status: int, category: str = "http_client_error") -> None:
        super().__init__(message)
        self.status = int(status)
        self.category = category


class RateLimitDeferred(TransientRequestError):
    """Raised only after an optional server-directed wait budget is exhausted."""

    def __init__(self, message: str, *, status: int = 429, waited: float = 0.0) -> None:
        super().__init__(message, status=status, splittable=False)
        self.waited = float(waited)


CDXRow: TypeAlias = tuple[str, str, str, str, str, str]


@dataclass(slots=True)
class CDXRows:
    """Compact CDX rows in timestamp/original/mimetype/status/digest/length order."""

    rows: list[CDXRow]
    resume_key: str | None = None


def is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TransportExhaustedError):
        return exc.timed_out
    current: BaseException | None = exc
    visited: set[int] = set()
    timeout_types = (
        TimeoutError,
        httpx.TimeoutException,
        urllib3.exceptions.TimeoutError,
        urllib3.exceptions.ReadTimeoutError,
        urllib3.exceptions.ConnectTimeoutError,
    )
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, timeout_types):
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException) and reason is not current:
            current = reason
            continue
        current = current.__cause__ or current.__context__
    return False


class HttpClient:
    """Wayback-aware HTTP client with independent connection fallbacks.

    The retry policy, shared 429 circuit, and fixed user-selected pacing remain in
    this class. Actual I/O is delegated to a persistent transport that can switch
    between httpx, urllib3, and the operating system's curl stack after genuine
    connection failures. An HTTP response never causes a backend switch; it is
    handled here so all workers follow the same Wayback policy.
    """

    def __init__(
        self,
        limiter: FixedRateLimiter,
        retries: int,
        timeout: float,
        user_agent: str,
        stop_event: threading.Event,
        retry_callback: Callable[[int, int, str, float], None] | None = None,
        *,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        pool_size: int = 4,
        host_gate: SharedHostGate | None = None,
        rate_limit_attempts: int = 0,
        rate_limit_max_wait: float = 0.0,
        network_backend: str = "auto",
        trust_environment: bool = True,
        network_callback: Callable[[str], None] | None = None,
        transport: ResilientTransport | None = None,
    ) -> None:
        self.limiter = limiter
        self.retries = max(1, int(retries))
        self.timeout = max(1.0, float(timeout))
        self.connect_timeout = max(1.0, float(connect_timeout if connect_timeout is not None else timeout))
        self.read_timeout = max(1.0, float(read_timeout if read_timeout is not None else timeout))
        self.user_agent = user_agent
        self.stop_event = stop_event
        self.retry_callback = retry_callback
        self.host_gate = host_gate or SharedHostGate()
        self.rate_limit_attempts = max(0, int(rate_limit_attempts))
        self.rate_limit_max_wait = max(0.0, float(rate_limit_max_wait))
        self.endpoint_lock = threading.Lock()
        self.endpoint_last_success: str | None = None
        self.endpoint_cooldown_until: dict[str, float] = {}
        self.transport = transport or ResilientTransport(
            pool_size=max(1, int(pool_size)),
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            mode=network_backend,
            trust_env=trust_environment,
            callback=network_callback,
        )

    def close(self) -> None:
        self.transport.close()

    def get(self, url: str, max_bytes: int, accept: str = "*/*") -> dict:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Accept-Language": "en-US,en;q=0.8",
        }
        ensure_frozen_bundle_available()
        generic_attempt = 0
        rate_attempt = 0
        total_rate_wait = 0.0

        while True:
            ensure_frozen_bundle_available()
            permit = self.host_gate.acquire_request(self.stop_event)
            try:
                with self.limiter.slot(self.stop_event):
                    if not self.host_gate.permit_is_current(permit):
                        self.host_gate.finish_request(permit, recovered=False)
                        continue
                    response = self.transport.request(url, headers, max_bytes, self.stop_event)
                status = int(response.status)

                retry_after_header = response.headers.get("retry-after") or response.headers.get("Retry-After")
                if status in {429, 503}:
                    retry_after = parse_retry_after(retry_after_header)
                    rate_attempt += 1
                    wait_seconds = self.host_gate.pause_for_rate_limit(retry_after, f"HTTP {status}")
                    total_rate_wait += wait_seconds
                    if self.retry_callback:
                        self.retry_callback(
                            rate_attempt,
                            self.rate_limit_attempts,
                            f"HTTP {status}; all Wayback requests paused",
                            wait_seconds,
                        )
                    attempts_exhausted = self.rate_limit_attempts > 0 and rate_attempt >= self.rate_limit_attempts
                    wait_exhausted = self.rate_limit_max_wait > 0 and total_rate_wait > self.rate_limit_max_wait
                    if attempts_exhausted or wait_exhausted:
                        raise RateLimitDeferred(
                            f"Wayback continued returning HTTP {status} after {rate_attempt} coordinated pauses. Progress was saved for resume.",
                            status=status,
                            waited=total_rate_wait,
                        )
                    continue

                self.host_gate.finish_request(permit, recovered=True)

                if status >= 400:
                    if status not in RETRYABLE_STATUS:
                        runtime_error = str(
                            response.headers.get("x-archive-wayback-runtime-error")
                            or response.headers.get("X-Archive-Wayback-Runtime-Error")
                            or ""
                        )
                        try:
                            body_preview = bytes(response.data[:4096]).decode("utf-8", "ignore")
                        except Exception:
                            body_preview = ""
                        combined = (runtime_error + " " + body_preview).casefold()
                        if status == 403 and "robots.txt" in combined:
                            category = "robots_blocked"
                        elif status == 403 and (
                            "blocked site error" in combined
                            or "excluded from the wayback machine" in combined
                        ):
                            category = "wayback_excluded"
                        elif status == 403:
                            category = "wayback_forbidden"
                        elif status == 404:
                            category = "missing_capture"
                        else:
                            category = "http_client_error"
                        detail = clean_space(runtime_error)[:300]
                        suffix = f" — {detail}" if detail else ""
                        raise PermanentRequestError(
                            f"HTTP {status}: {url}{suffix}", status=status, category=category
                        )
                    generic_attempt += 1
                    if generic_attempt >= self.retries:
                        raise TransientRequestError(
                            f"HTTP {status} after {self.retries} attempts: {url}",
                            status=status,
                            splittable=status in {408, 500, 502, 503, 504},
                        )
                    self.retry_wait(generic_attempt - 1, f"HTTP {status}", parse_retry_after(retry_after_header))
                    continue

                return {
                    "data": response.data,
                    "status": status,
                    "headers": response.headers,
                    "final_url": response.final_url,
                    "backend": response.backend,
                    "elapsed": response.elapsed,
                }
            except (RateLimitDeferred, Stopped):
                self.host_gate.finish_request(permit, recovered=False)
                raise
            except RuntimeError as exc:
                self.host_gate.finish_request(permit, recovered=False)
                if isinstance(exc, TransientRequestError):
                    raise
                if is_missing_frozen_bundle_error(exc):
                    raise frozen_bundle_error_from_exception(exc) from exc
                # TransportExhaustedError subclasses RuntimeError so it must be
                # handled before deterministic local RuntimeError failures.
                if isinstance(exc, TransportExhaustedError):
                    timed_out = is_timeout_error(exc)
                    read_timed_out = bool(getattr(exc, "read_timed_out", False))
                    generic_attempt += 1
                    if generic_attempt >= self.retries:
                        raise TransientRequestError(
                            f"network failure for {url}: {exc}",
                            timed_out=timed_out,
                            read_timed_out=read_timed_out,
                            connection_failed=bool(getattr(exc, "connection_failed", False)),
                            splittable=True,
                        ) from exc
                    self.retry_wait(generic_attempt - 1, "read timeout" if timed_out else str(exc))
                    continue
                if str(exc).startswith("response exceeds"):
                    # A valid CDX query can exceed the current response budget when
                    # historical URLs are unusually long. Treat this as recoverable
                    # pressure so the indexer can halve the page and/or split the
                    # saved date window instead of terminating near a 50k boundary.
                    raise TransientRequestError(
                        f"CDX response was larger than the safe in-memory budget for {url}: {exc}",
                        splittable=True,
                    ) from exc
                # Malformed URLs and other deterministic local validation errors
                # remain permanent and should not enter the network retry queue.
                raise
            except (httpx.HTTPError, urllib3.exceptions.HTTPError, TimeoutError, OSError) as exc:
                self.host_gate.finish_request(permit, recovered=False)
                if is_missing_frozen_bundle_error(exc):
                    raise frozen_bundle_error_from_exception(exc) from exc
                timed_out = is_timeout_error(exc)
                read_timed_out = is_transport_read_timeout(exc)
                generic_attempt += 1
                if generic_attempt >= self.retries:
                    raise TransientRequestError(
                        f"network failure for {url}: {exc}",
                        timed_out=timed_out,
                        read_timed_out=read_timed_out,
                        connection_failed=is_transport_connection_failure(exc),
                        splittable=True,
                    ) from exc
                self.retry_wait(generic_attempt - 1, "read timeout" if timed_out else str(exc))
            except Exception:
                # Do not leave a recovery probe marked in-flight when a local
                # parser/validation defect escapes the network categories above.
                self.host_gate.finish_request(permit, recovered=False)
                raise

    def download_to_path(
        self,
        url: str,
        destination: Path,
        max_bytes: int,
        accept: str = "*/*",
        *,
        compute_hash: bool = True,
    ) -> dict:
        """Stream a response to disk while retaining the normal Wayback policy."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Accept-Language": "en-US,en;q=0.8",
        }
        ensure_frozen_bundle_available()
        generic_attempt = 0
        rate_attempt = 0
        total_rate_wait = 0.0
        destination = Path(destination)
        range_restarts = 0

        while True:
            ensure_frozen_bundle_available()
            existing_size = destination.stat().st_size if destination.exists() else 0
            request_headers = dict(headers)
            # Partial offsets are only meaningful for the identity encoding,
            # including the first request before an interruption occurs.
            request_headers["Accept-Encoding"] = "identity"
            if existing_size > 0:
                request_headers["Range"] = f"bytes={existing_size}-"
                # Range offsets apply to the identity representation.
                request_headers["Accept-Encoding"] = "identity"
            permit = self.host_gate.acquire_request(self.stop_event)
            try:
                with self.limiter.slot(self.stop_event):
                    if not self.host_gate.permit_is_current(permit):
                        self.host_gate.finish_request(permit, recovered=False)
                        continue
                    if compute_hash:
                        # Preserve the long-standing transport extension contract for
                        # normal callers; the resource-lean acquisition path opts into
                        # the new no-hash flag explicitly.
                        response = self.transport.download(
                            url, request_headers, destination, max_bytes, self.stop_event
                        )
                    else:
                        response = self.transport.download(
                            url, request_headers, destination, max_bytes, self.stop_event,
                            compute_hash=False,
                        )
                status = int(response.status)
                if status == 416 and existing_size:
                    raise InvalidRangeResponse("server rejected saved replay offset; restarting complete file")
                if status < 200 or status >= 300:
                    if status < 400:
                        raise TransientRequestError(f"unexpected replay HTTP {status}: {url}")
                retry_after_header = response.headers.get("retry-after") or response.headers.get("Retry-After")
                if status in {429, 503}:
                    destination.unlink(missing_ok=True)
                    retry_after = parse_retry_after(retry_after_header)
                    rate_attempt += 1
                    wait_seconds = self.host_gate.pause_for_rate_limit(retry_after, f"HTTP {status}")
                    total_rate_wait += wait_seconds
                    if self.retry_callback:
                        self.retry_callback(
                            rate_attempt, self.rate_limit_attempts,
                            f"HTTP {status}; all Wayback requests paused", wait_seconds,
                        )
                    attempts_exhausted = self.rate_limit_attempts > 0 and rate_attempt >= self.rate_limit_attempts
                    wait_exhausted = self.rate_limit_max_wait > 0 and total_rate_wait > self.rate_limit_max_wait
                    if attempts_exhausted or wait_exhausted:
                        raise RateLimitDeferred(
                            f"Wayback continued returning HTTP {status} after {rate_attempt} coordinated pauses. Progress was saved for resume.",
                            status=status, waited=total_rate_wait,
                        )
                    continue

                self.host_gate.finish_request(permit, recovered=True)
                if status >= 400:
                    destination.unlink(missing_ok=True)
                    if status not in RETRYABLE_STATUS:
                        raise RuntimeError(f"HTTP {status}: {url}")
                    generic_attempt += 1
                    if generic_attempt >= self.retries:
                        raise TransientRequestError(
                            f"HTTP {status} after {self.retries} attempts: {url}",
                            status=status, splittable=False,
                        )
                    self.retry_wait(generic_attempt - 1, f"HTTP {status}", parse_retry_after(retry_after_header))
                    continue

                return {
                    "path": response.path,
                    "bytes": response.bytes_written,
                    "content_hash": response.content_hash,
                    "preview": response.preview,
                    "status": status,
                    "headers": response.headers,
                    "final_url": response.final_url,
                    "backend": response.backend,
                    "elapsed": response.elapsed,
                }
            except InvalidRangeResponse as exc:
                self.host_gate.finish_request(permit, recovered=False)
                destination.unlink(missing_ok=True)
                range_restarts += 1
                if range_restarts > 1:
                    raise TransientRequestError(str(exc), splittable=False) from exc
                if self.retry_callback:
                    self.retry_callback(1, 1, str(exc), 0.0)
                continue
            except RateLimitDeferred:
                destination.unlink(missing_ok=True)
                self.host_gate.finish_request(permit, recovered=False)
                raise
            except Stopped:
                self.host_gate.finish_request(permit, recovered=False)
                raise
            except RuntimeError as exc:
                self.host_gate.finish_request(permit, recovered=False)
                if isinstance(exc, TransientRequestError):
                    raise
                if is_missing_frozen_bundle_error(exc):
                    destination.unlink(missing_ok=True)
                    raise frozen_bundle_error_from_exception(exc) from exc
                if isinstance(exc, TransportExhaustedError):
                    # Preserve any bytes already streamed to the .part file. A
                    # retry (or the next application run) can request the rest.
                    timed_out = is_timeout_error(exc)
                    read_timed_out = bool(getattr(exc, "read_timed_out", False))
                    generic_attempt += 1
                    if generic_attempt >= self.retries:
                        raise TransientRequestError(
                            f"network failure for {url}: {exc}",
                            timed_out=timed_out, read_timed_out=read_timed_out,
                            connection_failed=bool(getattr(exc, "connection_failed", False)),
                            splittable=False,
                        ) from exc
                    self.retry_wait(generic_attempt - 1, "read timeout" if timed_out else str(exc))
                    continue
                # Local validation failures (for example max-size rejection) are
                # not valid resume state.
                destination.unlink(missing_ok=True)
                raise
            except (httpx.HTTPError, urllib3.exceptions.HTTPError, TimeoutError, OSError) as exc:
                self.host_gate.finish_request(permit, recovered=False)
                if is_missing_frozen_bundle_error(exc):
                    destination.unlink(missing_ok=True)
                    raise frozen_bundle_error_from_exception(exc) from exc
                timed_out = is_timeout_error(exc)
                read_timed_out = is_transport_read_timeout(exc)
                generic_attempt += 1
                if generic_attempt >= self.retries:
                    raise TransientRequestError(
                        f"network failure for {url}: {exc}", timed_out=timed_out,
                        read_timed_out=read_timed_out,
                        connection_failed=is_transport_connection_failure(exc),
                        splittable=False,
                    ) from exc
                self.retry_wait(generic_attempt - 1, "read timeout" if timed_out else str(exc))
            except Exception:
                destination.unlink(missing_ok=True)
                self.host_gate.finish_request(permit, recovered=False)
                raise

    def get_json(self, url: str, params: list[tuple[str, str]], max_bytes: int = 64 * 1024 * 1024) -> object:
        return self.get_json_any((url,), params, max_bytes=max_bytes)

    def _ordered_endpoints(self, urls: Iterable[str]) -> list[str]:
        endpoints = list(dict.fromkeys(str(url) for url in urls if str(url).strip()))
        now = time.monotonic()
        with self.endpoint_lock:
            preferred = self.endpoint_last_success
            active = [url for url in endpoints if self.endpoint_cooldown_until.get(url, 0.0) <= now]
        if not active:
            active = endpoints
        if preferred in active:
            active.remove(preferred)
            active.insert(0, preferred)
        return active

    def _remember_endpoint_success(self, endpoint: str) -> None:
        with self.endpoint_lock:
            self.endpoint_last_success = endpoint
            self.endpoint_cooldown_until.pop(endpoint, None)

    def _remember_endpoint_failure(self, endpoint: str) -> None:
        with self.endpoint_lock:
            self.endpoint_cooldown_until[endpoint] = time.monotonic() + 20.0

    @staticmethod
    def _cdx_format_attempts(endpoint: str, prefer_text: bool) -> tuple[str, str]:
        """Try an endpoint's native representation before its fallback.

        Wayback's path-specific Timemap endpoints can ignore ``output=txt`` or
        ``output=json``.  In particular, ``/web/timemap/json`` commonly returns
        JSON even when a caller asked for text.  Trying text first there made a
        valid response look malformed and caused every numbered page to be
        downloaded a second time.  The generic CDX endpoint still honors the
        caller's low-memory text preference.
        """
        path = urllib.parse.urlsplit(endpoint).path.rstrip("/").casefold()
        if path.endswith("/web/timemap/json"):
            return ("json", "text")
        if path.endswith("/web/timemap/cdx"):
            return ("text", "json")
        return ("text", "json") if prefer_text else ("json", "text")

    def get_cdx_any(
        self,
        urls: Iterable[str],
        params: list[tuple[str, str]],
        max_bytes: int = 64 * 1024 * 1024,
        *,
        prefer_text: bool = False,
    ) -> object:
        endpoints = self._ordered_endpoints(urls)
        if not endpoints:
            raise ValueError("at least one endpoint is required")
        failures: list[tuple[str, TransientRequestError]] = []
        text_params = cdx_text_fallback_params(params)

        for endpoint in endpoints:
            attempts = self._cdx_format_attempts(endpoint, prefer_text)
            first_error: BaseException | None = None
            for format_name in attempts:
                request_params = text_params if format_name == "text" else params
                full_url = cdx_request_url(endpoint, request_params)
                try:
                    accept = "text/plain,*/*" if format_name == "text" else "application/json,text/plain,*/*"
                    response = self.get(full_url, max_bytes, accept)
                    payload = parse_cdx_response_data(
                        response["data"], endpoint, request_params, format_name
                    )
                    self._remember_endpoint_success(endpoint)
                    return payload
                except MemoryError as exc:
                    raise TransientRequestError(
                        f"CDX parsing exceeded available memory at {endpoint}; retrying with smaller saved work",
                        splittable=True,
                        endpoint=endpoint,
                    ) from exc
                except MalformedCDXResponse as exc:
                    first_error = first_error or exc
                    if self.retry_callback:
                        other = "JSON" if format_name == "text" else "line-oriented text"
                        self.retry_callback(
                            1, 1,
                            f"CDX {format_name} response was malformed or truncated; retrying as {other}",
                            0.0,
                        )
                    continue
                except TransientRequestError as exc:
                    first_error = first_error or exc
                    # Every configured CDX endpoint uses the same Wayback host.
                    # Once every independent transport fails during connection
                    # setup, trying two more paths on that host only multiplies a
                    # DNS/proxy/TLS failure. Return control to the saved operation
                    # queue immediately so it can retry briefly and pause cleanly.
                    if exc.connection_failed or "safe in-memory budget" in str(exc):
                        self._remember_endpoint_failure(endpoint)
                        exc.endpoint = endpoint
                        raise
                    # A read timeout means Wayback accepted the request but did
                    # not finish the body. Repeating the same expensive query
                    # against every endpoint multiplies the stall; let the page
                    # queue requeue or subdivide it immediately instead.
                    if exc.read_timed_out or exc.timed_out:
                        self._remember_endpoint_failure(endpoint)
                        exc.endpoint = endpoint
                        raise
                    # Other transient failures may be endpoint-specific, so try
                    # the next service without reissuing another representation.
                    break
                except RuntimeError as exc:
                    if str(exc).startswith("HTTP ") or str(exc).startswith("response exceeds"):
                        raise
                    first_error = first_error or exc
                    continue

            self._remember_endpoint_failure(endpoint)
            if isinstance(first_error, TransientRequestError):
                failure = first_error
            else:
                failure = MalformedCDXResponse(
                    f"CDX response was unusable at {endpoint}: {first_error}",
                    splittable=True,
                    endpoint=endpoint,
                )
            failure.endpoint = endpoint
            failures.append((endpoint, failure))
            if self.retry_callback and len(endpoints) > 1:
                self.retry_callback(1, len(endpoints), f"Endpoint unavailable: {endpoint}; trying alternate CDX service", 0.0)

        timed_out = any(exc.timed_out for _, exc in failures)
        splittable = any(exc.splittable for _, exc in failures)
        summary = "; ".join(f"{endpoint}: {exc}" for endpoint, exc in failures)
        raise TransientRequestError(
            f"all CDX endpoints failed: {summary}",
            timed_out=timed_out,
            read_timed_out=any(exc.read_timed_out for _, exc in failures),
            connection_failed=bool(failures) and all(exc.connection_failed for _, exc in failures),
            splittable=splittable or timed_out,
        ) from (failures[-1][1] if failures else None)

    def get_cdx_rows_any(
        self,
        urls: Iterable[str],
        params: list[tuple[str, str]],
        max_bytes: int = 64 * 1024 * 1024,
        *,
        prefer_text: bool = True,
    ) -> CDXRows:
        """Fetch CDX rows without constructing a second list of per-row dicts.

        Large 50,000-row responses previously existed simultaneously as raw
        bytes, one decoded string, a list-of-lists, and a list-of-dicts. That
        multiplication was the main source of the platform-dependent crashes
        near the first large page. This path parses directly into compact tuples.
        """
        # Keep compatibility with callers and tests that replace the public
        # get_cdx_any method. Production uses the compact direct parser below;
        # an overridden legacy method is converted once into compact rows.
        legacy_getter = getattr(type(self), "get_cdx_any")
        if (
            getattr(legacy_getter, "__module__", "") != __name__
            or getattr(legacy_getter, "__name__", "") != "get_cdx_any"
        ):
            payload = self.get_cdx_any(
                urls, params, max_bytes=max_bytes, prefer_text=prefer_text
            )
            return parse_cdx_rows_payload(payload)

        endpoints = self._ordered_endpoints(urls)
        if not endpoints:
            raise ValueError("at least one endpoint is required")
        failures: list[tuple[str, TransientRequestError]] = []
        text_params = cdx_text_fallback_params(params)

        for endpoint in endpoints:
            attempts = self._cdx_format_attempts(endpoint, prefer_text)
            first_error: BaseException | None = None
            for format_name in attempts:
                request_params = text_params if format_name == "text" else params
                full_url = cdx_request_url(endpoint, request_params)
                try:
                    accept = "text/plain,*/*" if format_name == "text" else "application/json,text/plain,*/*"
                    response = self.get(full_url, max_bytes, accept)
                    result = parse_cdx_rows_response_data(
                        response["data"], endpoint, request_params, format_name
                    )
                    self._remember_endpoint_success(endpoint)
                    return result
                except MemoryError as exc:
                    raise TransientRequestError(
                        f"CDX parsing exceeded available memory at {endpoint}; retrying with smaller saved work",
                        splittable=True,
                        endpoint=endpoint,
                    ) from exc
                except MalformedCDXResponse as exc:
                    first_error = first_error or exc
                    if self.retry_callback:
                        other = "JSON" if format_name == "text" else "line-oriented text"
                        self.retry_callback(
                            1, 1,
                            f"CDX {format_name} response was malformed or truncated; retrying as {other}",
                            0.0,
                        )
                    continue
                except TransientRequestError as exc:
                    first_error = first_error or exc
                    if (
                        exc.connection_failed
                        or exc.read_timed_out
                        or exc.timed_out
                        or "safe in-memory budget" in str(exc)
                    ):
                        self._remember_endpoint_failure(endpoint)
                        exc.endpoint = endpoint
                        raise
                    break
                except RuntimeError as exc:
                    if str(exc).startswith("HTTP ") or str(exc).startswith("response exceeds"):
                        raise
                    first_error = first_error or exc
                    continue

            self._remember_endpoint_failure(endpoint)
            failure = first_error if isinstance(first_error, TransientRequestError) else MalformedCDXResponse(
                f"CDX response was unusable at {endpoint}: {first_error}",
                splittable=True,
                endpoint=endpoint,
            )
            failure.endpoint = endpoint
            failures.append((endpoint, failure))
            if self.retry_callback and len(endpoints) > 1:
                self.retry_callback(1, len(endpoints), f"Endpoint unavailable: {endpoint}; trying alternate CDX service", 0.0)

        timed_out = any(exc.timed_out for _, exc in failures)
        summary = "; ".join(f"{endpoint}: {exc}" for endpoint, exc in failures)
        raise TransientRequestError(
            f"all CDX endpoints failed: {summary}",
            timed_out=timed_out,
            read_timed_out=any(exc.read_timed_out for _, exc in failures),
            connection_failed=bool(failures) and all(exc.connection_failed for _, exc in failures),
            splittable=timed_out or any(exc.splittable for _, exc in failures),
        ) from (failures[-1][1] if failures else None)

    def get_json_any(
        self,
        urls: Iterable[str],
        params: list[tuple[str, str]],
        max_bytes: int = 64 * 1024 * 1024,
    ) -> object:
        return self.get_cdx_any(urls, params, max_bytes=max_bytes, prefer_text=False)

    def get_cdx_json_any(
        self,
        urls: Iterable[str],
        params: list[tuple[str, str]],
        max_bytes: int = 64 * 1024 * 1024,
    ) -> object:
        """Fetch one native JSON representation per endpoint.

        Numbered Timemap paging is a JSON protocol.  It must not turn one bad
        page into a second text request for the same page; the page scheduler
        owns retries and durable failed-page state.
        """
        return self._get_cdx_native_json(urls, params, max_bytes, compact=False)

    def get_cdx_json_rows_any(
        self,
        urls: Iterable[str],
        params: list[tuple[str, str]],
        max_bytes: int = 64 * 1024 * 1024,
    ) -> CDXRows:
        """Compact-row native JSON fetch used by numbered Timemap pages."""
        result = self._get_cdx_native_json(urls, params, max_bytes, compact=True)
        if not isinstance(result, CDXRows):
            raise RuntimeError("native CDX JSON row parser returned an unexpected result")
        return result

    def _get_cdx_native_json(
        self,
        urls: Iterable[str],
        params: list[tuple[str, str]],
        max_bytes: int,
        *,
        compact: bool,
    ) -> object | CDXRows:
        endpoints = self._ordered_endpoints(urls)
        if not endpoints:
            raise ValueError("at least one endpoint is required")
        failures: list[tuple[str, TransientRequestError]] = []
        for endpoint in endpoints:
            full_url = cdx_request_url(endpoint, params)
            try:
                response = self.get(full_url, max_bytes, "application/json,*/*")
                payload = parse_json_response(response["data"], endpoint)
                result = parse_cdx_rows_payload(payload) if compact else payload
                self._remember_endpoint_success(endpoint)
                return result
            except MemoryError as exc:
                raise TransientRequestError(
                    f"CDX JSON parsing exceeded available memory at {endpoint}",
                    splittable=True,
                    endpoint=endpoint,
                ) from exc
            except MalformedCDXResponse as exc:
                exc.endpoint = endpoint
                failure = exc
            except TransientRequestError as exc:
                exc.endpoint = endpoint
                if (
                    exc.connection_failed
                    or exc.read_timed_out
                    or exc.timed_out
                    or "safe in-memory budget" in str(exc)
                ):
                    self._remember_endpoint_failure(endpoint)
                    raise
                failure = exc
            except RuntimeError as exc:
                if str(exc).startswith("HTTP ") or str(exc).startswith("response exceeds"):
                    raise
                failure = MalformedCDXResponse(
                    f"CDX JSON response was unusable at {endpoint}: {exc}",
                    splittable=True,
                    endpoint=endpoint,
                )
            self._remember_endpoint_failure(endpoint)
            failures.append((endpoint, failure))
            if self.retry_callback and len(endpoints) > 1:
                self.retry_callback(
                    1,
                    len(endpoints),
                    f"Endpoint unavailable: {endpoint}; trying alternate CDX service",
                    0.0,
                )

        summary = "; ".join(f"{endpoint}: {exc}" for endpoint, exc in failures)
        raise TransientRequestError(
            f"all native JSON CDX endpoints failed: {summary}",
            timed_out=any(exc.timed_out for _, exc in failures),
            read_timed_out=any(exc.read_timed_out for _, exc in failures),
            connection_failed=bool(failures) and all(exc.connection_failed for _, exc in failures),
            splittable=any(exc.splittable for _, exc in failures),
        ) from (failures[-1][1] if failures else None)

    def retry_wait(self, attempt: int, reason: str, retry_after: float | None = None) -> None:
        base = max(float(retry_after or 0), min(120.0, 2**attempt))
        wait_seconds = base * random.uniform(0.85, 1.2)
        if self.retry_callback:
            self.retry_callback(attempt + 2, self.retries, reason, wait_seconds)
        self.stop_event.wait(wait_seconds)
        if self.stop_event.is_set():
            raise Stopped



def request_cdx_rows(
    client: object,
    urls: Iterable[str],
    params: list[tuple[str, str]],
    max_bytes: int = 64 * 1024 * 1024,
    *,
    prefer_text: bool = True,
) -> CDXRows:
    """Use the compact row API while retaining compatibility with clients.

    Third-party integrations and the long-standing test/mocking surface may
    implement only ``get_cdx_any``. Converting that legacy payload here keeps
    those clients working while the built-in HttpClient takes the low-memory
    direct parsing path.
    """
    compact_getter = getattr(client, "get_cdx_rows_any", None)
    if callable(compact_getter):
        return compact_getter(
            urls, params, max_bytes=max_bytes, prefer_text=prefer_text
        )
    legacy_getter = getattr(client, "get_cdx_any")
    payload = legacy_getter(
        urls, params, max_bytes=max_bytes, prefer_text=prefer_text
    )
    return parse_cdx_rows_payload(payload)


def _uses_builtin_cdx_getter(client: object) -> bool:
    getter = getattr(type(client), "get_cdx_any", None)
    return (
        getattr(getter, "__module__", "") == __name__
        and getattr(getter, "__name__", "") == "get_cdx_any"
    )


def request_cdx_json_payload(
    client: object,
    urls: Iterable[str],
    params: list[tuple[str, str]],
    max_bytes: int = 64 * 1024 * 1024,
) -> object:
    """Use native JSON in production while retaining the established mock API."""
    strict_getter = getattr(client, "get_cdx_json_any", None)
    if callable(strict_getter) and _uses_builtin_cdx_getter(client):
        return strict_getter(urls, params, max_bytes=max_bytes)
    return client.get_cdx_any(urls, params, max_bytes=max_bytes, prefer_text=False)


def request_cdx_json_rows(
    client: object,
    urls: Iterable[str],
    params: list[tuple[str, str]],
    max_bytes: int = 64 * 1024 * 1024,
) -> CDXRows:
    """Fetch a numbered page as native JSON without representation fallback."""
    strict_getter = getattr(client, "get_cdx_json_rows_any", None)
    if callable(strict_getter) and _uses_builtin_cdx_getter(client):
        return strict_getter(urls, params, max_bytes=max_bytes)
    payload = client.get_cdx_any(urls, params, max_bytes=max_bytes, prefer_text=False)
    return parse_cdx_rows_payload(payload)


def _looks_like_json_container(data: bytes | bytearray | memoryview) -> bool:
    """Return whether a CDX body starts like a JSON table/error object."""
    prefix = bytes(data[:64]).lstrip(b"\xef\xbb\xbf \t\r\n")
    return prefix.startswith((b"[", b"{"))


def parse_cdx_response_data(
    data: bytes,
    endpoint: str,
    params: list[tuple[str, str]],
    requested_format: str,
) -> object:
    """Parse the representation Wayback returned, not merely the one requested.

    Path-specific Timemap services occasionally ignore the ``output`` query
    parameter.  A valid response in the other representation can therefore be
    consumed locally instead of issuing the same expensive CDX request again.
    Bodies which actually look like truncated JSON still raise and use the
    normal representation/endpoint recovery path.
    """
    looks_json = _looks_like_json_container(data)
    if requested_format == "text":
        if looks_json:
            return parse_json_response(data, endpoint)
        return parse_cdx_text_response(data, endpoint, params)
    try:
        return parse_json_response(data, endpoint)
    except MalformedCDXResponse:
        if looks_json:
            raise
        return parse_cdx_text_response(data, endpoint, cdx_text_fallback_params(params))


def parse_cdx_rows_response_data(
    data: bytes | bytearray,
    endpoint: str,
    params: list[tuple[str, str]],
    requested_format: str,
) -> CDXRows:
    """Compact-row equivalent of :func:`parse_cdx_response_data`."""
    looks_json = _looks_like_json_container(data)
    if requested_format == "text":
        if looks_json:
            return parse_cdx_rows_payload(parse_json_response(bytes(data), endpoint))
        return parse_cdx_text_rows(data, endpoint, params)
    try:
        return parse_cdx_rows_payload(parse_json_response(bytes(data), endpoint))
    except MalformedCDXResponse:
        if looks_json:
            raise
        return parse_cdx_text_rows(data, endpoint, cdx_text_fallback_params(params))

def parse_json_response(data: bytes, endpoint: str = "") -> object:
    raw = data.decode("utf-8", "replace").lstrip("\ufeff").strip()
    if not raw:
        return []
    try:
        return json_loads(raw)
    except JSONDecodeErrors as exc:
        around = raw[max(0, exc.pos - 160): exc.pos + 160]
        preview = clean_space(around or raw[:320])
        raise MalformedCDXResponse(
            f"CDX returned malformed JSON from {endpoint} at line {exc.lineno}, "
            f"column {exc.colno}: {preview}",
            splittable=True,
            endpoint=endpoint,
        ) from exc


def _decode_field(value: bytes | bytearray | memoryview) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value.tobytes().decode("utf-8", "replace")


def _iter_binary_lines(data: bytes | bytearray):
    """Yield lines using the C-level delimiter search, without a full copy."""
    start = 0
    length = len(data)
    while start < length:
        end = data.find(b"\n", start)
        if end < 0:
            yield data[start:]
            return
        yield data[start:end]
        start = end + 1


def parse_cdx_rows_payload(payload: object) -> CDXRows:
    """Convert JSON-style CDX data directly to compact tuples."""
    if payload == []:
        return CDXRows([])
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or payload)
        lowered = message.casefold()
        if "no capture" in lowered or "no result" in lowered or "not found" in lowered:
            return CDXRows([])
        raise RuntimeError(message)
    if not isinstance(payload, list) or not payload:
        raise MalformedCDXResponse("unexpected CDX JSON payload", splittable=True)
    header = payload[0]
    if not isinstance(header, list):
        raise RuntimeError("unexpected CDX response header")
    positions = {str(name): index for index, name in enumerate(header)}
    required = ("timestamp", "original")
    if any(name not in positions for name in required):
        raise RuntimeError("CDX response did not include timestamp and original")
    body = payload[1:]
    resume_key: str | None = None
    if len(body) >= 2 and body[-2] == [] and isinstance(body[-1], list) and len(body[-1]) == 1:
        resume_key = str(body[-1][0])
        body = body[:-2]

    def value(item: list, name: str) -> str:
        index = positions.get(name)
        if index is None or index >= len(item):
            return ""
        return str(item[index] if item[index] is not None else "")

    rows: list[CDXRow] = []
    for item in body:
        if not isinstance(item, list) or len(item) != len(header):
            raise MalformedCDXResponse("incomplete CDX JSON row; page must be retried", splittable=True)
        timestamp = value(item, "timestamp")
        original = value(item, "original")
        if not re.fullmatch(r"\d{14}", timestamp) or not original:
            raise MalformedCDXResponse("invalid CDX timestamp or URL; page must be retried", splittable=True)
        if timestamp and original:
            rows.append(
                (
                    timestamp,
                    original,
                    value(item, "mimetype"),
                    value(item, "statuscode"),
                    value(item, "digest"),
                    value(item, "length"),
                )
            )
    return CDXRows(rows, resume_key)


def parse_cdx_text_rows(
    data: bytes | bytearray,
    endpoint: str = "",
    params: list[tuple[str, str]] | None = None,
) -> CDXRows:
    """Parse line-oriented CDX output in one pass with bounded duplication.

    A normal 200 response with no rows is a valid empty result. This matters for
    sparse sites and date windows; treating it as a broken connection caused some
    projects to keep subdividing and retrying work that was already complete.
    """
    if not data:
        return CDXRows([])
    prefix = bytes(data[:1000]).lstrip(b"\xef\xbb\xbf").lower()
    if any(marker in prefix for marker in (b"<!doctype", b"<html", b"bad gateway", b"temporarily unavailable", b"too many requests")):
        preview = clean_space(bytes(data[:320]).decode("utf-8", "replace"))
        raise MalformedCDXResponse(
            f"CDX plain-text response returned an error page from {endpoint}: {preview}",
            splittable=True,
            endpoint=endpoint,
        )
    params = params or []
    if any(key.casefold() == "shownumpages" and value.casefold() == "true" for key, value in params):
        for raw_line in _iter_binary_lines(data):
            token = raw_line.strip().lstrip(b"\xef\xbb\xbf")
            if not token:
                continue
            if token.isdigit():
                return CDXRows([(token.decode("ascii"), "", "", "", "", "")])
            break
        raise MalformedCDXResponse(
            f"CDX page-count fallback was not numeric at {endpoint}: {clean_space(bytes(data[:320]).decode('utf-8', 'replace'))}",
            splittable=True,
            endpoint=endpoint,
        )

    rows: list[CDXRow] = []
    malformed_count = 0
    malformed_preview = ""
    after_blank = False
    resume_candidate: bytes | None = None
    first_nonempty = True
    fl_value = next((value for key, value in params if key.casefold() == "fl"), "")
    has_urlkey = fl_value.lstrip().casefold().startswith("urlkey,")
    field_offset = 1 if has_urlkey else 0
    expected_parts = 7 if has_urlkey else 6
    maxsplit = expected_parts - 1
    header_token = b"urlkey" if has_urlkey else b"timestamp"
    for raw_line in _iter_binary_lines(data):
        line = raw_line.strip()
        if first_nonempty:
            line = line.lstrip(b"\xef\xbb\xbf")
        if not line:
            after_blank = True
            continue
        first_nonempty = False
        if resume_candidate is not None:
            malformed_count += 1
            if not malformed_preview:
                malformed_preview = _decode_field(resume_candidate)[:320]
            resume_candidate = None
        parts = line.split(None, maxsplit)
        if after_blank and len(parts) == 1:
            resume_candidate = parts[0]
            after_blank = False
            continue
        after_blank = False
        if len(parts) != expected_parts:
            malformed_count += 1
            if not malformed_preview:
                malformed_preview = _decode_field(line)[:320]
            continue
        timestamp = parts[field_offset]
        if not timestamp.isdigit():
            # Header rows are rare. Test for them only after the numeric fast
            # path fails rather than lower-casing the first field of every row.
            if parts[0].lower() == header_token:
                continue
            malformed_count += 1
            if not malformed_preview:
                malformed_preview = _decode_field(line)[:320]
            continue
        mimetype = parts[field_offset + 1]
        statuscode = parts[field_offset + 2]
        digest = parts[field_offset + 3]
        length = parts[field_offset + 4]
        original = parts[field_offset + 5]
        rows.append((
            _decode_field(timestamp), _decode_field(original), _decode_field(mimetype),
            _decode_field(statuscode), _decode_field(digest), _decode_field(length),
        ))
    if malformed_count:
        raise MalformedCDXResponse(
            f"CDX plain-text response contained {malformed_count} malformed row(s) at {endpoint}: "
            f"{clean_space(malformed_preview)}",
            splittable=True,
            endpoint=endpoint,
        )
    resume_key = _decode_field(resume_candidate) if resume_candidate else None
    return CDXRows(rows, resume_key)


def cdx_text_fallback_params(params: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reissue a CDX query in a line-oriented format that survives bad JSON rows."""
    cleaned = [
        (key, value)
        for key, value in params
        if key.casefold() not in {"output", "fl", "gzip"}
    ]
    cleaned.append(("output", "txt"))
    cleaned.append(("gzip", "false"))
    if not any(key.casefold() == "shownumpages" and value.casefold() == "true" for key, value in params):
        # Keeping original last makes split(maxsplit=5) safe even for malformed
        # historical URLs containing literal spaces.
        # urlkey is intentionally retained for robust resumeKey traversal. The
        # Wayback CDX resume token is based on index ordering, and omitting the
        # sort key has historically produced incorrect continuation behavior on
        # some server versions. The parser discards it after continuation is safe.
        cleaned.append(("fl", "urlkey,timestamp,mimetype,statuscode,digest,length,original"))
    return cleaned


def parse_cdx_text_response(
    data: bytes,
    endpoint: str = "",
    params: list[tuple[str, str]] | None = None,
) -> object:
    raw = data.decode("utf-8", "replace").lstrip("\ufeff").replace("\x00", "").strip()
    if not raw:
        params = params or []
        if any(key.casefold() == "shownumpages" and value.casefold() == "true" for key, value in params):
            raise MalformedCDXResponse(
                f"CDX page-count fallback returned an empty body from {endpoint}",
                splittable=True,
                endpoint=endpoint,
            )
        return [["timestamp", "mimetype", "statuscode", "digest", "length", "original"]]
    lowered = raw[:1000].casefold()
    if any(marker in lowered for marker in ("<!doctype", "<html", "bad gateway", "temporarily unavailable", "too many requests")):
        raise MalformedCDXResponse(
            f"CDX plain-text fallback returned an error page from {endpoint}: {clean_space(raw[:320])}",
            splittable=True,
            endpoint=endpoint,
        )
    params = params or []
    if any(key.casefold() == "shownumpages" and value.casefold() == "true" for key, value in params):
        token = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        if token.isdigit():
            return int(token)
        raise MalformedCDXResponse(
            f"CDX page-count fallback was not numeric at {endpoint}: {clean_space(raw[:320])}",
            splittable=True,
            endpoint=endpoint,
        )

    fields = ["timestamp", "mimetype", "statuscode", "digest", "length", "original"]
    blocks = re.split(r"\r?\n[ \t]*\r?\n", raw)
    resume_key: str | None = None
    row_text = raw
    if len(blocks) > 1:
        candidate = blocks[-1].strip()
        if candidate and "\n" not in candidate and len(candidate.split()) == 1:
            resume_key = candidate
            row_text = "\n\n".join(blocks[:-1]).strip()

    rows: list[list[str]] = []
    malformed: list[str] = []
    for line in row_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, len(fields) - 1)
        if len(parts) != len(fields) or not parts[0].isdigit():
            malformed.append(line)
            continue
        rows.append(parts)
    if malformed:
        raise MalformedCDXResponse(
            f"CDX plain-text fallback contained {len(malformed)} malformed row(s) at {endpoint}: "
            f"{clean_space(malformed[0][:320])}",
            splittable=True,
            endpoint=endpoint,
        )
    payload: list[object] = [fields, *rows]
    if resume_key:
        payload.extend([[], [resume_key]])
    return payload

def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None
