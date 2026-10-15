"""HTTP helpers shared by the Atlassian Cloud exporters."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Optional, Tuple

Transport = Callable[[str], Tuple[int, Optional[dict]]]
Post = Callable[[str, dict], Tuple[int, Optional[dict]]]
Download = Callable[[str], Tuple[int, bytes]]


class TransportError(Exception):
    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail
        if detail and not status:
            message = detail
        elif detail:
            message = f"HTTP {status}: {detail[:180]}"
        elif status:
            message = f"HTTP {status}"
        else:
            message = "request failed"
        super().__init__(message)


class _Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class _CaptureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            fp.read()
        finally:
            fp.close()
        raise _Redirect(newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


def basic_auth(email: str, token: str) -> str:
    return base64.b64encode(f"{email}:{token}".encode()).decode("ascii")


def same_host(left: str, right: str) -> bool:
    left_host = urllib.parse.urlsplit(left).netloc.lower()
    right_host = urllib.parse.urlsplit(right).netloc.lower()
    return left_host == right_host


def parse_json(raw: bytes) -> dict | None:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def retry_seconds(header: str | None, fallback: float) -> float:
    if header and header.strip().isdigit():
        return min(float(header.strip()), 60.0)
    return fallback


def request_with_retries(
    opener,
    url: str,
    auth: str,
    timeout: float,
    accept: str,
    attempts: int,
    error_cls: type[TransportError],
    data: bytes | None = None,
):
    delay = 1.0
    last_error: TransportError | None = None
    for attempt in range(attempts):
        try:
            return read_response(opener, url, auth, timeout, accept, error_cls, data)
        except urllib.error.HTTPError as error:
            payload = error.read()
            if error.code == 429 and attempt + 1 < attempts:
                time.sleep(retry_seconds(error.headers.get("Retry-After"), delay))
                delay = min(delay * 2, 30)
                continue
            return error.code, payload
        except urllib.error.URLError as error:
            last_error = error_cls(0, str(error.reason))
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
        except error_cls as error:
            last_error = error
            break
    raise last_error or error_cls(0, "request failed")


def read_response(
    opener,
    url: str,
    auth: str,
    timeout: float,
    accept: str,
    error_cls: type[TransportError],
    data: bytes | None = None,
) -> tuple[int, bytes]:
    current = url
    send_auth = True
    for _hop in range(5):
        headers = {"Accept": accept, "User-Agent": "supreme-octo-doodle"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if send_auth:
            headers["Authorization"] = f"Basic {auth}"
        request = urllib.request.Request(current, data=data, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except _Redirect as redirect:
            if not redirect.url.startswith("https://"):
                raise error_cls(0, "redirect was not https") from redirect
            if data is not None and not same_host(current, redirect.url):
                raise error_cls(0, "redirect left the Atlassian host") from redirect
            send_auth = same_host(current, redirect.url)
            current = redirect.url
    raise error_cls(0, "redirect did not finish")


def _opener():
    return urllib.request.build_opener(_CaptureRedirect)


def json_transport(
    email: str,
    token: str,
    error_cls: type[TransportError],
    timeout: float = 30.0,
    attempts: int = 5,
) -> Transport:
    auth = basic_auth(email, token)
    opener = _opener()

    def transport(url: str) -> tuple[int, dict | None]:
        status, body = request_with_retries(
            opener, url, auth, timeout, "application/json", attempts, error_cls
        )
        return status, parse_json(body)

    return transport


def json_post(
    email: str,
    token: str,
    error_cls: type[TransportError],
    timeout: float = 30.0,
    attempts: int = 5,
) -> Post:
    auth = basic_auth(email, token)
    opener = _opener()

    def post(url: str, payload: dict) -> tuple[int, dict | None]:
        data = json.dumps(payload).encode("utf-8")
        status, body = request_with_retries(
            opener, url, auth, timeout, "application/json", attempts, error_cls, data
        )
        return status, parse_json(body)

    return post


def bytes_download(
    email: str,
    token: str,
    error_cls: type[TransportError],
    timeout: float = 60.0,
    attempts: int = 5,
) -> Download:
    auth = basic_auth(email, token)
    opener = _opener()

    def download(url: str) -> tuple[int, bytes]:
        return request_with_retries(opener, url, auth, timeout, "*/*", attempts, error_cls)

    return download
