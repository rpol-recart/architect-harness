"""Explicit host-side, read-only HTTP observation. Not exposed as a general MCP tool."""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .core import GraphService, HarnessError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise HarnessError("invalid_url", "Разрешены только абсолютные HTTP(S) URL.")
    if parts.username or parts.password or parts.fragment:
        raise HarnessError("invalid_url", "URL не должен содержать credentials или fragment.")
    try:
        port = parts.port
    except ValueError as error:
        raise HarnessError("invalid_url", "Некорректный порт.") from error
    host = parts.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    default_port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}:{port or default_port}"


def probe_http(service: GraphService, url: str, allowed_origins: list[str],
               related_ids: list[str], timeout: float = 5.0, max_bytes: int = 65536) -> dict:
    """Capture GET metadata. Allowlist is supplied by the host, never a model tool argument.

    Timeout limits individual blocking socket operations, not total wall time.
    No redirects, proxies, credentials, cookies or response body storage. A host with
    unrestricted shell access can bypass this helper; it is not a network sandbox.
    """
    requested_origin = origin(url)
    if requested_origin not in {origin(item) for item in allowed_origins}:
        raise HarnessError("origin_not_allowed", "Origin не разрешён оператором.")
    if not 0 < timeout <= 30 or not 0 < max_bytes <= 1048576:
        raise HarnessError("invalid_probe_limits", "Некорректные ограничения наблюдения.")
    # Reject invalid references before making an actual request.
    snapshot = service.snapshot()
    known = {n["id"] for n in snapshot["nodes"]}
    if not related_ids or any(ref not in known for ref in related_ids):
        raise HarnessError("invalid_reference", "Нужны существующие связанные узлы.")
    parts = urllib.parse.urlsplit(url)
    public_source = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                           "[redacted]" if parts.query else "", ""))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "architect-harness/0.1"})
    started = time.monotonic()
    capture = {"method": "GET", "origin": requested_origin,
               "observed_at": datetime.now(timezone.utc).isoformat(), "socket_timeout_seconds": timeout}
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error  # HTTP errors are still observed responses; redirects are not followed.
        with response:
            content = response.read(max_bytes + 1)
            bounded = content[:max_bytes]
            capture.update(status=response.code, content_type=response.headers.get("Content-Type", ""),
                           bytes_captured=len(bounded), body_truncated=len(content) > max_bytes,
                           sha256=hashlib.sha256(bounded).hexdigest())
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        capture.update(status=None, error_type=type(error).__name__, outcome="request_failed")
    capture["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    evidence = service.record_captured_observation(
        title="HTTP-наблюдение", source=public_source,
        conditions="Один GET из текущего окружения; без повторов и переходов; тело не сохраняется.",
        result=json.dumps(capture, ensure_ascii=False, sort_keys=True), related_ids=related_ids,
        actor="host:http-probe", capture=capture)
    return {"evidence": evidence, "capture": capture,
            "limits": "Подтверждает только этот вызов. Не доказывает контракт, нагрузочную устойчивость или production-поведение."}
