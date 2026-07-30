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
import urllib.parse

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
