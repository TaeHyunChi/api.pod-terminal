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

from ... import kube_exec, node_shell
from ...auth import claims_from_query, subject_from_query, subject_from_request

log = logging.getLogger(__name__)

bp = Blueprint("terminals", __name__, url_prefix="/terminals")


@bp.get("/namespaces")
def allowed_namespaces():
    """터미널을 열 수 있는 네임스페이스 목록 — 화면이 고를 수 있게."""
    return {"items": list(current_app.config["ALLOWED_NAMESPACES"])}


@bp.get("/node-status")
def get_node_status():
    """노드 셸을 열기 전 확인 — 있는 노드인지, 이 사람이 열 수 있는지.

    화면이 버튼을 미리 잠글 수 있어야 한다. 붙고 나서 거절하면 사용자는 무엇이
    문제인지 모른 채 검은 화면만 본다.
    """
    claims = claims_from_query()
    if not claims:
        return {"code": "UNAUTHORIZED", "message": "인증이 필요합니다."}, 401

    node = (request.args.get("node") or "").strip()
    if not node:
        return {"code": "INVALID_PARAMETER", "message": "node 파라미터가 필요합니다."}, 400

    allowed = _is_node_shell_admin(claims)
    if not kube_exec.in_cluster():
        return {
            "node": node, "exists": False, "allowed": allowed, "available": False,
            "message": "클러스터 밖에서는 노드 셸을 열 수 없습니다.",
        }

    exists = node_shell.node_exists(node)
    return {
        "node": node,
        "exists": exists,
        # 이 사람이 노드 셸을 열 자격이 있는가(관리자 역할).
        "allowed": allowed,
        "available": bool(exists and allowed),
        "message": (
            "" if exists and allowed
            else ("노드를 찾을 수 없습니다." if not exists else "노드 셸은 관리자만 열 수 있습니다.")
        ),
    }


@bp.get("/pod-status")
def get_pod_status():
    """터미널을 열기 전의 Pod 상태 확인.

    "접속해도 되는가" 의 판단(`available`)을 **여기서** 내린다 — 화면마다 제각기
    판단하면 MFE 가 늘 때마다 규칙이 갈라진다. 터미널은 셸 프로세스를 새로 띄우는
    일이라 **Running 인 Pod 에만** 열 수 있다 — 로그와 달리 Succeeded/Failed 는
    오브젝트가 남아 있어도 exec 가 안 된다.

        { "name", "namespace", "exists", "phase", "ready",
          "startedAt", "message", "available" }
    """
    if not subject_from_request():
        return {"code": "UNAUTHORIZED", "message": "인증이 필요합니다."}, 401

    namespace = (request.args.get("namespace") or "").strip()
    pod = (request.args.get("pod") or request.args.get("podId") or "").strip()
    if not namespace or not pod:
        return {"code": "BAD_REQUEST", "message": "namespace 와 pod 는 필수입니다."}, 400
    if namespace not in current_app.config["ALLOWED_NAMESPACES"]:
        # 어떤 네임스페이스가 있는지는 알려 주지 않는다.
        return {"code": "FORBIDDEN", "message": "이 네임스페이스는 조회할 수 없습니다."}, 403
    if not kube_exec.in_cluster():
        return {"code": "K8S_UNAVAILABLE", "message": "클러스터 밖에서는 조회할 수 없습니다."}, 502

    try:
        status = kube_exec.pod_status(namespace, pod)
    except kube_exec.ExecError as exc:
        return {"code": "K8S_UNAVAILABLE", "message": str(exc)}, 502

    if status is None:
        return {
            "name": pod,
            "namespace": namespace,
            "exists": False,
            "phase": "",
            "ready": False,
            "startedAt": "",
            "message": "Pod 를 찾을 수 없습니다. 이미 종료된 배포의 Pod 는 클러스터에 남아 있지 않습니다.",
            "available": False,
        }

    running = status["phase"] == "Running"
    return {
        "name": pod,
        "namespace": namespace,
        "exists": True,
        "phase": status["phase"],
        "ready": status["ready"],
        "startedAt": status["startedAt"],
        "message": status["reason"]
        or ("" if running else "실행 중인 Pod 에만 터미널을 열 수 있습니다."),
        "available": running,
    }


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


# --------------------------------------------------------------------------- #
# 노드 셸 — 호스트에 붙는 터미널
# --------------------------------------------------------------------------- #
def _is_node_shell_admin(claims: dict) -> bool:
    """노드 셸을 열 자격 — 관리자 역할이 있어야 한다.

    Pod 터미널은 로그인한 사용자면 열 수 있지만 이것은 **노드 root 권한**이라
    같은 기준을 쓸 수 없다. 역할 id 는 auth-service 가 토큰에 실어 준다.
    """
    if current_app.config.get("AUTH_DISABLED"):
        return True
    required = set(current_app.config["NODE_SHELL_ROLE_IDS"])
    if not required:
        # 목록이 비었으면 **아무도** 열 수 없다. 열어 두는 쪽이 위험하다.
        return False
    role_ids = claims.get("roleIds")
    return bool(required & set(role_ids)) if isinstance(role_ids, list) else False


def node_stream(ws) -> None:
    """노드 셸 WebSocket.

    Pod 터미널(`stream`)과 다른 점은 **붙기 전에 Pod 를 만들고 끝나면 지운다**는
    것뿐이다. 붙은 뒤의 프레임 규약은 완전히 같아서 화면은 같은 컴포넌트를 쓴다.
    """
    claims = claims_from_query()
    if not claims:
        ws.close(1008, "unauthorized")
        return
    if not _is_node_shell_admin(claims):
        _send(ws, {"type": "error", "message": "노드 셸은 관리자만 열 수 있습니다."})
        ws.close(1008, "forbidden")
        return

    node = (request.args.get("node") or "").strip()
    if not node:
        _send(ws, {"type": "error", "message": "node 파라미터가 필요합니다."})
        ws.close(1008, "bad-request")
        return

    if not kube_exec.in_cluster():
        _send(ws, {"type": "error", "message": "클러스터 밖에서는 노드 셸을 열 수 없습니다."})
        ws.close(1011, "no-cluster")
        return

    user_id = claims.get("sub") or "unknown"
    pod_name = ""
    try:
        if not node_shell.node_exists(node):
            raise kube_exec.ExecError(f"노드 '{node}' 를 찾을 수 없습니다.")
        # 특권 Pod 를 만드는 데 몇 초 걸린다 — 화면이 멈춘 것처럼 보이지 않게 알린다.
        _send(ws, {"type": "output", "data": f"[{node}] 노드 셸을 준비하는 중...\r\n"})
        pod_name = node_shell.create_debug_pod(node, user_id)
        node_shell.wait_running(pod_name, current_app.config["NODE_SHELL_START_TIMEOUT"])
        upstream = kube_exec.connect(
            current_app.config["NODE_SHELL_NAMESPACE"],
            pod_name,
            command=node_shell.NSENTER_COMMAND,
        )
    except kube_exec.ExecError as exc:
        if pod_name:
            node_shell.delete_debug_pod(pod_name)
        _send(ws, {"type": "error", "message": str(exc)})
        ws.close(1011, "node-shell-failed")
        return

    _send(ws, {"type": "ready"})
    # 호스트 셸은 흔적이 남아야 한다 — 누가 언제 어느 노드에 열었는지.
    log.warning("노드 셸 열림: node=%s user=%s pod=%s", node, user_id, pod_name)

    done = threading.Event()
    reader = threading.Thread(
        target=_pump_to_browser,
        args=(current_app._get_current_object(), upstream, ws, done),
        daemon=True,
        name=f"node-{node[:36]}",
    )
    reader.start()

    ping_seconds = current_app.config["WS_PING_SECONDS"]
    try:
        while not done.is_set():
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
        # 특권 Pod 를 반드시 지운다. 남으면 그 노드에 root 컨테이너가 방치된다.
        node_shell.delete_debug_pod(pod_name)
        log.warning("노드 셸 닫힘: node=%s user=%s pod=%s", node, user_id, pod_name)
