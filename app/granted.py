"""볼륨 권한 설정에서 '터미널' 권한이 켜진 네임스페이스 (volume-service 0.4.0).

관리자 메뉴 '볼륨 권한 설정'이 네임스페이스에 터미널 권한을 켜면 volume-service 가 이 서비스 어카운트에
exec 권한(RoleBinding)을 연결한다. 여기서는 그 목록을 읽어 설정 `ALLOWED_NAMESPACES` 에 더한다 —
호출자의 토큰으로 읽고(헤더 또는 WebSocket `?token=`), 30초 동안 기억한다. 읽지 못하면 설정만 쓴다.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from flask import current_app, has_request_context, request

log = logging.getLogger(__name__)

_CACHE: dict = {"at": 0.0, "items": []}
TTL_SECONDS = 30.0


def _token() -> str:
    if not has_request_context():
        return ""
    header = (request.headers.get("Authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.args.get("token") or "").strip()


def granted() -> list[str]:
    base = (current_app.config.get("VOLUME_SERVICE_URL") or "").rstrip("/")
    if not base:
        return []
    now = time.monotonic()
    if now - _CACHE["at"] < TTL_SECONDS:
        return list(_CACHE["items"])
    token = _token()
    if not token:
        return list(_CACHE["items"])
    req = urllib.request.Request(f"{base}/volumes/namespaces?grant=terminal")  # noqa: S310 - 내부 URL
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as res:  # noqa: S310
            body = json.loads(res.read() or b"{}")
        items = [i["name"] for i in body.get("items") or [] if isinstance(i, dict) and i.get("name")]
        _CACHE.update(at=now, items=items)
        return items
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("볼륨 권한 설정(터미널) 목록을 읽지 못했습니다 — 설정만 씁니다: %s", exc)
        return list(_CACHE["items"])


def allowed_namespaces() -> list[str]:
    """설정 ALLOWED_NAMESPACES + 볼륨 권한 설정의 터미널 네임스페이스(중복 없이, 설정이 앞)."""
    out = list(current_app.config["ALLOWED_NAMESPACES"])
    for name in granted():
        if name not in out:
            out.append(name)
    return out


def is_allowed(namespace: str) -> bool:
    if namespace in current_app.config["ALLOWED_NAMESPACES"]:
        return True
    return namespace in granted()
