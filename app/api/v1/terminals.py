"""Pod 원격 터미널 — 브라우저 WebSocket 을 k8s exec 로 중계한다.

    브라우저 ── WS  /api/v1/terminals/exec?namespace=&pod=&container=&token=
                     │
                 k8s  WS  .../pods/{pod}/exec (v4.channel.k8s.io)

Guacamole 과 같은 결이다 — 사용자는 아무것도 설치하지 않고, 게이트웨이가 프로토콜을
바꿔 준다. 다만 화면에 그리는 일은 브라우저의 터미널 에뮬레이터가 한다.

브라우저와 주고받는 프레임은 **JSON 한 줄**이다. 채널 번호가 붙은 k8s 의 이진
프레임을 그대로 넘기면 화면이 그 규약까지 알아야 한다.

    받는 것 {"type":"input","data":"ls\\n"}
            {"type":"resize","cols":120,"rows":30}
    보내는 것 {"type":"ready"}
            {"type":"output","data":"..."}      stdout/stderr 를 합쳐서 보낸다
            {"type":"ping"}                     유휴 연결 유지
            {"type":"exit","message":""}        빈 문자열이면 정상 종료
            {"type":"error","message":"..."}

stdout 과 stderr 를 합치는 이유 — tty 로 띄우면 컨테이너 쪽에서 이미 하나로 합쳐져
나오고, 화면도 한 줄기로 그린다.
"""

import json
import logging
import threading

from flask import Blueprint, current_app, request

from ... import kube_exec
from ...auth import subject_from_query

log = logging.getLogger(__name__)

bp = Blueprint("terminals", __name__, url_prefix="/terminals")


@bp.get("/namespaces")
def allowed_namespaces():
    """터미널을 열 수 있는 네임스페이스 목록 — 화면이 고를 수 있게."""
    return {"items": list(current_app.config["ALLOWED_NAMESPACES"])}


def _send(ws, payload: dict) -> None:
    ws.send(json.dumps(payload, ensure_ascii=False))


def _params() -> tuple[dict | None, str]:
    """요청 파라미터 검증. (값, 오류 메시지)."""
    namespace = (request.args.get("namespace") or "").strip()
    pod = (request.args.get("pod") or request.args.get("podId") or "").strip()
    if not namespace or not pod:
        return None, "namespace 와 pod 는 필수입니다."

    if namespace not in current_app.config["ALLOWED_NAMESPACES"]:
        # 어떤 네임스페이스가 있는지는 알려 주지 않는다.
        return None, "이 네임스페이스에는 접속할 수 없습니다."

    return {
        "namespace": namespace,
        "pod": pod,
        "container": (request.args.get("container") or "").strip(),
    }, ""


def _pump_to_browser(app, upstream, ws, done: threading.Event) -> None:
    """컨테이너 출력을 브라우저로 옮긴다.

    별도 스레드인 이유 — 양쪽 다 "올 때까지 기다리는" 소켓이라 한 스레드로는
    한쪽만 볼 수 있다.
    """
    try:
        with app.app_context():
            while not done.is_set():
                channel, text = kube_exec.read_frame(upstream.recv())
                if channel in (kube_exec.CH_STDOUT, kube_exec.CH_STDERR):
                    if text:
                        _send(ws, {"type": "output", "data": text})
                elif channel == kube_exec.CH_ERROR:
                    _send(ws, {"type": "exit", "message": kube_exec.exit_message(text)})
                    break
                elif channel == -1:
                    break  # 빈 프레임 = 상대가 닫았다
    except Exception:  # noqa: BLE001 — 정상 종료도 예외로 온다
        pass
    finally:
        done.set()
        # 브라우저 쪽 읽기를 깨워 연결을 정리하게 한다.
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def stream(ws) -> None:
    """터미널 WebSocket. 연결이 끊길 때까지 살아 있는다."""
    if not subject_from_query():
        # 1008 = policy violation. 브라우저 콘솔에 이유가 남는다.
        ws.close(1008, "unauthorized")
        return

    params, error = _params()
    if params is None:
        _send(ws, {"type": "error", "message": error})
        ws.close(1008, "bad-request")
        return

    if not kube_exec.in_cluster():
        _send(ws, {"type": "error", "message": "클러스터 밖에서는 터미널을 열 수 없습니다."})
        ws.close(1011, "no-cluster")
        return

    try:
        upstream = kube_exec.connect(
            params["namespace"], params["pod"], container=params["container"]
        )
    except kube_exec.ExecError as exc:
        _send(ws, {"type": "error", "message": str(exc)})
        ws.close(1011, "exec-failed")
        return

    _send(ws, {"type": "ready"})
    log.info("터미널 열림: %s/%s", params["namespace"], params["pod"])

    done = threading.Event()
    reader = threading.Thread(
        target=_pump_to_browser,
        args=(current_app._get_current_object(), upstream, ws, done),
        daemon=True,
        name=f"term-{params['pod'][:40]}",
    )
    reader.start()

    ping_seconds = current_app.config["WS_PING_SECONDS"]
    try:
        while not done.is_set():
            # 타임아웃까지 기다렸다가 아무것도 없으면 ping 을 보낸다. 터미널은 사용자가
            # 치지 않는 동안 조용해서, 알리지 않으면 프록시가 유휴 연결을 끊는다.
            message = ws.receive(timeout=ping_seconds)
            if message is None:
                _send(ws, {"type": "ping"})
                continue
            _handle(upstream, message)
    except Exception:  # noqa: BLE001 — 정상 종료도 예외로 온다
        pass
    finally:
        done.set()
        try:
            upstream.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("터미널 닫힘: %s/%s", params["namespace"], params["pod"])


def _handle(upstream, message: str) -> None:
    """브라우저가 보낸 한 줄을 k8s 채널 프레임으로 바꿔 보낸다."""
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return  # 해석 못 하는 프레임은 버린다
    if not isinstance(payload, dict):
        return

    kind = payload.get("type")
    if kind == "input":
        data = payload.get("data")
        if isinstance(data, str) and data:
            upstream.send_binary(kube_exec.stdin_frame(data))
    elif kind == "resize":
        upstream.send_binary(
            kube_exec.resize_frame(payload.get("cols") or 80, payload.get("rows") or 24)
        )
