from __future__ import annotations

import json
import os
import time
from urllib import error, parse, request

from .errors import OuraError


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checked_url(url):
    p = parse.urlsplit(url)
    if p.username or p.password or p.fragment or p.query:
        raise OuraError("invalid_url", "Base URL must not contain credentials, query or fragment")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise OuraError("invalid_url", "Use HTTPS; HTTP is allowed only on loopback")
    if not p.hostname:
        raise OuraError("invalid_url", "URL hostname is required")
    return url.rstrip("/")


def fetch(method, url, headers=None, payload=None, *, form=False, retry_get=True):
    data = None
    h = {"User-Agent": "OuraPersonalCLI/0.1", "Accept": "application/json", **(headers or {})}
    if payload is not None:
        data = parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    opener = request.build_opener(NoRedirect)
    attempts = 3 if method == "GET" and retry_get else 1
    for attempt in range(attempts):
        try:
            req = request.Request(url, data=data, headers=h, method=method)
            with opener.open(req, timeout=25) as response:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise OuraError("response_too_large", "Provider response exceeded the size limit")
                try:
                    value = json.loads(body) if body else {}
                except (ValueError, UnicodeError):
                    raise OuraError("invalid_response", "Provider returned invalid JSON") from None
                return response.status, value
        except error.HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After", "")
            exc.close()
            if status in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                try:
                    wait = max(0.0, float(retry_after))
                except ValueError:
                    wait = 2**attempt
                time.sleep(min(wait, float(os.environ.get("OURA_RETRY_WAIT_MAX", "60"))))
                continue
            return status, {}
        except (error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, float(os.environ.get("OURA_RETRY_WAIT_MAX", "60"))))
                continue
            raise OuraError(
                "network_error", "Network request failed; saved data and credentials were preserved", 3
            ) from None
    raise OuraError("network_error", "Request attempts exhausted", 3)


def expect(status, value):
    if 200 <= status < 300:
        return value
    code = {
        400: "invalid_request",
        401: "login_required",
        403: "permission_or_membership",
        404: "not_found",
        409: "conflict",
        410: "expired",
        413: "too_large",
        422: "invalid_request",
        429: "rate_limited",
    }.get(status, "provider_error")
    raise OuraError(code, f"HTTP {status}: {code.replace('_', ' ')}", 3)
