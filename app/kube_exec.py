"""k8s `exec` 로 컨테이너 안에 셸을 띄우고 그 스트림을 다룬다.

k8s 의 exec 는 WebSocket 하나 위에 여러 채널을 얹는다(`v4.channel.k8s.io`).
프레임의 **첫 바이트가 채널 번호**이고 나머지가 내용이다.

    0  stdin    (우리 → 컨테이너)
    1  stdout   (컨테이너 → 우리)
    2  stderr   (컨테이너 → 우리)
    3  error    (끝났을 때 상태가 JSON 으로 온다)
    4  resize   (우리 → 컨테이너, `{"Width":80,"Height":24}`)

그래서 브라우저에서 온 글자를 그대로 흘려보내면 안 되고, 채널 번호를 앞에 붙여야
한다. 반대로 컨테이너 출력도 첫 바이트를 떼고 화면에 보내야 한다.

인증은 in-cluster ServiceAccount 토큰이다. 권한은 `pods/exec` 의 **create 와 get
둘 다** 필요하다 — kubectl 이 쓰는 SPDY 방식은 POST 지만 우리가 쓰는 WebSocket
방식은 업그레이드 요청이 GET 이다(deploy/k3s/30-rbac.yaml).
"""

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

log = logging.getLogger(__name__)

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH = f"{_SA_DIR}/token"
CA_PATH = f"{_SA_DIR}/ca.crt"

#: 채널 번호. k8s 의 v4 스트림 규약이다.
CH_STDIN = 0
CH_STDOUT = 1
CH_STDERR = 2
CH_ERROR = 3
CH_RESIZE = 4

SUBPROTOCOL = "v4.channel.k8s.io"


class ExecError(Exception):
    """붙지 못했다. 호출자가 사용자에게 보여 줄 사유로 바꾼다."""


def in_cluster() -> bool:
    return os.path.exists(TOKEN_PATH)


def _token() -> str:
    # 토큰은 회전한다 — 붙을 때마다 다시 읽는다.
    with open(TOKEN_PATH, encoding="utf-8") as handle:
        return handle.read().strip()


def _containers(body: dict) -> list[dict]:
    """Pod 의 컨테이너 목록 — 여러 개면 화면이 고르게 한다.

    init 컨테이너도 포함한다(로그가 남고, 실패 원인이 거기 있을 때가 많다).
    `ready`/`state` 는 상태 배지용이고, 순서는 spec 순서 그대로다.
    """
    spec = body.get("spec") or {}
    status = body.get("status") or {}
    state_of = {}
    for key in ("containerStatuses", "initContainerStatuses"):
        for cs in status.get(key) or []:
            if not isinstance(cs, dict):
                continue
            state = cs.get("state") or {}
            # 값이 아니라 **키 존재**로 판정한다 — running 이 빈 객체로 올 수 있어
            # 참/거짓으로 보면 실행 중인 컨테이너를 waiting 으로 오판한다.
            phase = "running" if "running" in state else (
                "terminated" if "terminated" in state else "waiting"
            )
            state_of[cs.get("name")] = {
                "ready": bool(cs.get("ready")),
                "state": phase,
                "restarts": int(cs.get("restartCount") or 0),
            }

    containers = []
    for key, is_init in (("initContainers", True), ("containers", False)):
        for c in spec.get(key) or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            meta = state_of.get(c["name"], {"ready": False, "state": "waiting", "restarts": 0})
            containers.append({
                "name": c["name"],
                "image": c.get("image") or "",
                "init": is_init,
                **meta,
            })
    return containers


def pod_status(namespace: str, pod: str) -> dict | None:
    """Pod 한 개의 현재 상태. 없으면(삭제됐으면) None.

    터미널을 열기 전의 **상태 확인**에 쓴다 — 이미 사라진 Pod 에 exec 를 열려고
    하면 사용자에게는 원인 없는 연결 실패로만 보인다. 먼저 물어보고, 접속할 수
    없으면 화면이 상태 정보만 그리게 한다. (log-stream 서비스와 같은 모양)
    """
    path = (
        f"/api/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/pods/{urllib.parse.quote(pod)}"
    )
    api = current_app.config["K8S_API"].rstrip("/")
    request = urllib.request.Request(f"{api}{path}", method="GET")  # noqa: S310
    request.add_header("Authorization", f"Bearer {_token()}")
    context = ssl.create_default_context(cafile=CA_PATH)
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=current_app.config["K8S_CONNECT_TIMEOUT"], context=context
        ) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            raise ExecError("이 Pod 를 조회할 권한이 없습니다.") from None
        raise ExecError(f"Pod 상태를 가져오지 못했습니다({exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("Pod 상태 조회 실패: %s/%s %s", namespace, pod, exc)
        raise ExecError("k8s API 에 연결할 수 없습니다.") from None

    status = body.get("status") or {}
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in status.get("conditions") or []
        if isinstance(c, dict)
    )
    # 왜 안 뜨는지(CrashLoopBackOff 같은 것)는 컨테이너 상태에 들어 있다.
    reason = ""
    for cs in status.get("containerStatuses") or []:
        state = (cs or {}).get("state") or {}
        reason = (
            (state.get("waiting") or {}).get("reason")
            or (state.get("terminated") or {}).get("reason")
            or reason
        )

    return {
        "phase": status.get("phase") or "",
        "ready": ready,
        "startedAt": status.get("startTime") or "",
        "reason": reason,
        "containers": _containers(body),
    }


def exec_url(namespace: str, pod: str, *, container: str, command: list[str]) -> str:
    """exec 스트림 주소. 셸을 tty 로 띄운다."""
    query = [("stdin", "true"), ("stdout", "true"), ("stderr", "true"), ("tty", "true")]
    if container:
        query.append(("container", container))
    # command 는 인자마다 따로 실어야 한다 — 하나로 합치면 그 전체가 실행 파일 이름이 된다.
    query.extend(("command", part) for part in command)
    path = (
        f"/api/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/pods/{urllib.parse.quote(pod)}/exec"
    )
    return f"{path}?{urllib.parse.urlencode(query)}"


def connect(namespace: str, pod: str, *, container: str = "", command: list[str] | None = None):
    """컨테이너에 붙어 `websocket.WebSocket` 을 돌려준다.

    호출측이 다 쓰면 `close()` 한다. 붙지 못하면 `ExecError` 를 던진다.
    """
    try:
        import websocket  # 지연 import — 테스트는 이 의존성 없이도 돈다
    except ImportError as exc:  # pragma: no cover - 운영 이미지에는 항상 있다
        raise ExecError("터미널 의존성이 설치되지 않았습니다.") from exc

    api = current_app.config["K8S_API"].rstrip("/")
    url = api.replace("https://", "wss://").replace("http://", "ws://")
    url += exec_url(namespace, pod, container=container, command=command or default_command())

    try:
        conn = websocket.create_connection(
            url,
            header=[f"Authorization: Bearer {_token()}"],
            subprotocols=[SUBPROTOCOL],
            sslopt={"ca_certs": CA_PATH, "cert_reqs": ssl.CERT_REQUIRED},
            timeout=current_app.config["K8S_CONNECT_TIMEOUT"],
            enable_multithread=True,
        )
        # 붙는 데는 제한 시간을 두되 **붙은 뒤에는 푼다.** 이 timeout 은 읽기에도
        # 걸려서 그대로 두면 사용자가 아무것도 치지 않는 동안 셸이 그 시간마다
        # 끊긴다 — 터미널은 대개 조용히 열려 있으므로 바로 티가 난다(실제로 겪었다).
        conn.settimeout(None)
        return conn
    except Exception as exc:  # noqa: BLE001 — websocket 라이브러리가 던지는 종류가 다양하다
        log.warning("exec 접속 실패: %s/%s %s", namespace, pod, exc)
        message = str(exc)
        if "403" in message:
            raise ExecError("이 Pod 에 접속할 권한이 없습니다.") from None
        if "404" in message:
            raise ExecError("Pod 를 찾을 수 없습니다.") from None
        raise ExecError("Pod 에 접속하지 못했습니다.") from None


def default_command() -> list[str]:
    """띄울 셸.

    세 가지를 챙긴다.

    1. **대화형(-i)으로 띄운다.** 안 그러면 프롬프트가 나오지 않아, 명령을 쳐도
       화면이 그대로라 사용자에게는 "실행이 안 된다" 로 보인다(실제로 그렇게 겪었다).
    2. **stderr 를 버리지 않는다.** 셸의 프롬프트는 stdout 이 아니라 stderr 로 나간다 —
       예전에 `2>/dev/null` 로 bash 없음 오류를 감추려다 프롬프트까지 같이 지웠다.
       그래서 오류를 감추는 대신 `command -v` 로 bash 가 있는지 먼저 확인한다.
    3. **TERM 을 정해 준다.** k8s exec 는 TERM 을 넘겨주지 않아서, 비워 두면
       vim·top 같은 전체화면 프로그램이 "terminal not fully functional" 로 뜬다.
    """
    return [
        "/bin/sh",
        "-c",
        "export TERM=${TERM:-xterm-256color}; "
        "if command -v bash >/dev/null 2>&1; then exec bash -i; fi; "
        "exec sh -i",
    ]


def stdin_frame(text: str) -> bytes:
    """사용자가 친 글자 → stdin 채널 프레임."""
    return bytes([CH_STDIN]) + text.encode("utf-8")


def resize_frame(cols: int, rows: int) -> bytes:
    """터미널 크기 변경 → resize 채널 프레임.

    크기를 알려 주지 않으면 컨테이너는 기본값(80x24)으로 알고 있어서, 넓은 창에서
    vim 같은 전체화면 프로그램이 화면 오른쪽을 쓰지 못한다.
    """
    payload = json.dumps({"Width": max(1, int(cols)), "Height": max(1, int(rows))})
    return bytes([CH_RESIZE]) + payload.encode("utf-8")


def read_frame(data: bytes | str) -> tuple[int, str]:
    """컨테이너에서 온 프레임 → (채널, 내용).

    빈 프레임은 채널만 있고 내용이 없다 — 그때는 (채널, "") 이다.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data:
        return -1, ""
    return data[0], data[1:].decode("utf-8", errors="replace")


def exit_message(payload: str) -> str:
    """error 채널(3)의 JSON → 사람이 읽을 한 줄.

    정상 종료면 빈 문자열이다. k8s 는 성공도 이 채널로 알려 준다(`status: Success`).
    """
    try:
        status = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return payload.strip()
    if status.get("status") == "Success":
        return ""
    return status.get("message") or payload.strip()
