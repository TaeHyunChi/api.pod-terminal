"""WebSocket 인증 — `?token=` 쿼리의 JWT 를 검증한다.

브라우저의 WebSocket API 는 헤더를 붙일 수 없어 Authorization 을 쓸 수 없다.
그래서 쿼리로 받는다. 대가가 있다 — 토큰이 URL 에 실려 접근 로그나 프록시 로그에
남을 수 있다. 그래서 이 서비스의 접근 로그는 쿼리를 남기지 않고(gunicorn 기본
포맷은 경로만 찍는다), 토큰 자체는 어디에도 기록하지 않는다.
"""

import logging

import jwt
from flask import current_app, request

from .errors import Forbidden

log = logging.getLogger(__name__)


def subject_from_query() -> str | None:
    """접속한 사용자 id. 검증에 실패하면 None."""
    return _subject((request.args.get("token") or "").strip())


def subject_from_request() -> str | None:
    """HTTP 엔드포인트용 — Authorization 헤더를 먼저 보고 `?token=` 으로 물러난다.

    WebSocket 은 헤더를 못 붙여 쿼리로 받지만, 보통의 GET 은 헤더를 쓸 수 있고
    그쪽이 로그에 토큰이 남지 않아 낫다.
    """
    header = (request.headers.get("Authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return _subject(header[7:].strip())
    return subject_from_query()


def claims_from_query() -> dict | None:
    """토큰의 claim 전체 — 역할 판정처럼 `sub` 만으로 부족할 때 쓴다.

    노드 셸이 이것을 쓴다: 노드 root 권한이라 "로그인했는가" 로는 부족하고
    `roleIds` 를 봐야 한다.
    """
    token = (request.args.get("token") or "").strip()
    if not token:
        header = (request.headers.get("Authorization") or "").strip()
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    return _claims(token)


def _claims(token: str) -> dict | None:
    if current_app.config.get("AUTH_DISABLED"):
        return {"sub": (request.args.get("userId") or "anonymous").strip() or "anonymous"}
    secret = current_app.config.get("JWT_SECRET")
    if not token or not secret:
        return None
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[current_app.config.get("JWT_ALGORITHM", "HS256")],
            options={"require": ["exp"], "verify_aud": False},
        )
    except jwt.InvalidTokenError as exc:
        log.info("ws 토큰 거절: %s", exc)
        return None


def _subject(token: str) -> str | None:
    if current_app.config.get("AUTH_DISABLED"):
        return (request.args.get("userId") or "anonymous").strip() or "anonymous"

    secret = current_app.config.get("JWT_SECRET")
    if not token or not secret:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[current_app.config.get("JWT_ALGORITHM", "HS256")],
            options={"require": ["exp"], "verify_aud": False},
        )
    except jwt.InvalidTokenError as exc:
        log.info("ws 토큰 거절: %s", exc)
        return None

    subject = claims.get("sub")
    return subject if isinstance(subject, str) and subject else None


def has_permission(key: str, claims: dict | None) -> bool:
    """권한 키 검사 (api.role docs/permission-catalog.md 1절 공용 규칙).

    AUTH_DISABLED → True, 관리자(`ADMIN_ROLE_ID`) → True(관리자는 늘 모든 권한),
    아니면 JWT `permissions` 클레임(문자열 배열)에 key 가 있어야 한다.
    이 서비스는 WebSocket 이라 `g` 대신 검증한 claims 를 직접 받는다.
    """
    if current_app.config.get("AUTH_DISABLED"):
        return True
    claims = claims or {}
    role_ids = claims.get("roleIds") or []
    if isinstance(role_ids, str):
        role_ids = [role_ids]
    if isinstance(role_ids, list) and current_app.config["ADMIN_ROLE_ID"] in role_ids:
        return True
    perms = claims.get("permissions")
    return isinstance(perms, list) and key in perms


def require_permission(key: str, claims: dict | None) -> None:
    """HTTP 핸들러용 — 권한이 없으면 403 PERMISSION_REQUIRED 를 던진다."""
    if not has_permission(key, claims):
        raise Forbidden(f"이 작업에는 '{key}' 권한이 필요합니다.", code="PERMISSION_REQUIRED")
