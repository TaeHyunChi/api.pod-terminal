"""노드 셸 — 호스트에 붙는 터미널.

Pod 터미널(`kube_exec`)과 목적이 다르다. 저쪽은 컨테이너 **안**에 셸을 띄우지만,
여기는 노드 **자체**(호스트 OS)에 붙는다. kubelet 로그를 보거나 디스크·네트워크를
진단하는 일은 컨테이너 안에서 할 수 없다.

## 어떻게 붙나

k8s 에는 "노드에 exec" 같은 API 가 없다. `kubectl debug node/` 가 하는 방식을
그대로 쓴다.

    1. 그 노드에 **특권 Pod** 를 하나 띄운다 (hostPID/hostNetwork, privileged)
    2. Running 이 되면 그 Pod 에 exec 로 `nsenter -t 1 -m -u -i -n -p -- sh` 를 실행
       → PID 1(호스트 init)의 네임스페이스로 들어가므로 사실상 호스트 셸이다
    3. 세션이 끝나면 Pod 를 지운다

이미지는 노드에 이미 있는 busybox 를 쓴다(k3s 가 함께 배포한다). nsenter 애플릿이
들어 있고, 새로 받을 것이 없어 외부망이 막힌 환경에서도 뜬다.

## 이것은 노드 root 권한이다

그래서 다른 기능보다 좁게 잠근다.

- **관리자 역할만.** JWT 의 `roleIds` 에 관리자 역할이 있어야 한다. Pod 터미널은
  로그인한 사용자면 열 수 있지만 이것은 아니다.
- **누가 언제 어느 노드에 열었는지 로그로 남긴다.** 호스트 셸은 흔적이 남아야 한다.
- **전용 네임스페이스에만** 특권 Pod 를 만든다. 앱 네임스페이스에 섞이면 그 이름으로
  다른 워크로드를 흉내 낼 수 있고, RBAC 을 좁게 주기도 어렵다.
- 세션이 끊기면 **반드시 지운다**(정상 종료든 오류든). 남으면 그 노드에 특권 Pod 가
  방치된다.
- Pod 는 `activeDeadlineSeconds` 로도 스스로 죽는다 — 서버가 지우지 못하고 죽는
  경우(파드 재시작 등)의 마지막 안전장치다.
"""

import json
import logging
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

from .kube_exec import CA_PATH, ExecError, _token

log = logging.getLogger(__name__)

#: 호스트 네임스페이스로 들어가는 명령. PID 1 은 호스트의 init 이다.
NSENTER_COMMAND = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh"]


def _api() -> str:
    return current_app.config["K8S_API"].rstrip("/")


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{_api()}{path}", data=body, method=method)  # noqa: S310
    request.add_header("Authorization", f"Bearer {_token()}")
    if body:
        request.add_header("Content-Type", "application/json")
    context = ssl.create_default_context(cafile=CA_PATH)
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=current_app.config["K8S_CONNECT_TIMEOUT"], context=context
        ) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (json.loads(exc.read() or b"{}") or {}).get("message") or ""
        except (ValueError, OSError):
            pass
        if exc.code in (401, 403):
            raise ExecError(f"권한이 없습니다: {detail or method + ' ' + path}") from None
        if exc.code == 404:
            raise ExecError("대상을 찾을 수 없습니다.") from None
        raise ExecError(f"k8s API 오류({exc.code}) {detail}".strip()) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("k8s API 호출 실패: %s %s %s", method, path, exc)
        raise ExecError("k8s API 에 연결할 수 없습니다.") from None


def node_exists(node: str) -> bool:
    """노드가 실제로 있는지. 없는 이름으로 특권 Pod 를 만들면 Pending 으로 매달린다."""
    try:
        _request("GET", f"/api/v1/nodes/{urllib.parse.quote(node)}")
    except ExecError:
        return False
    return True


def debug_pod_manifest(node: str, name: str, user_id: str) -> dict:
    """노드에 붙기 위한 특권 Pod.

    `tolerations: Exists` 가 필요하다 — 컨트롤 플레인 노드에는 taint 가 걸려 있어
    그것 없이는 스케줄되지 않는다. 진단이 가장 필요한 노드가 대개 그쪽이다.
    """
    config = current_app.config
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": config["NODE_SHELL_NAMESPACE"],
            # 플랫폼 공통 라벨 규약 — 생성 사용자 id 와 API 이름을 반드시 붙인다.
            "labels": {
                "app.kubernetes.io/name": "node-shell",
                "app.kubernetes.io/managed-by": "pod-terminal-service",
                "oncloud-ai/created-by-api": "pod-terminal-service",
                "oncloud-ai/created-by": user_id,
                "pod-terminal-service.oncloud-ai/node": node,
            },
        },
        "spec": {
            "nodeName": node,
            "hostPID": True,
            "hostNetwork": True,
            "hostIPC": True,
            "restartPolicy": "Never",
            # 서버가 지우지 못하고 죽어도 Pod 가 스스로 끝나게 하는 마지막 안전장치.
            "activeDeadlineSeconds": config["NODE_SHELL_MAX_SECONDS"],
            "tolerations": [{"operator": "Exists"}],
            "containers": [
                {
                    "name": "shell",
                    "image": config["NODE_SHELL_IMAGE"],
                    # exec 로 붙을 때까지 살아 있기만 하면 된다.
                    "command": ["sleep", str(config["NODE_SHELL_MAX_SECONDS"])],
                    "securityContext": {"privileged": True},
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "200m", "memory": "128Mi"},
                    },
                }
            ],
        },
    }


def create_debug_pod(node: str, user_id: str) -> str:
    """특권 Pod 를 만들고 이름을 돌려준다. 실패는 `ExecError`."""
    namespace = current_app.config["NODE_SHELL_NAMESPACE"]
    # 이름에 난수를 붙인다 — 같은 노드에 두 사람이 동시에 열 수 있어야 하고,
    # 지우기 전에 재사용하면 이름 충돌로 실패한다.
    name = f"node-shell-{_safe(node)}-{secrets.token_hex(4)}"
    _request(
        "POST",
        f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods",
        debug_pod_manifest(node, name, user_id),
    )
    log.info("노드 셸 Pod 생성: node=%s pod=%s user=%s", node, name, user_id)
    return name


def delete_debug_pod(name: str) -> None:
    """세션이 끝나면 지운다. **실패해도 예외를 올리지 않는다** — 정리 실패로 사용자에게
    오류를 보여 봐야 할 일이 없고, 남은 Pod 는 activeDeadlineSeconds 가 끝낸다."""
    namespace = current_app.config["NODE_SHELL_NAMESPACE"]
    try:
        _request(
            "DELETE",
            f"/api/v1/namespaces/{urllib.parse.quote(namespace)}"
            f"/pods/{urllib.parse.quote(name)}?gracePeriodSeconds=0",
        )
        log.info("노드 셸 Pod 삭제: %s", name)
    except ExecError as exc:
        log.warning("노드 셸 Pod 를 지우지 못했습니다(%s): %s", name, exc)


def wait_running(name: str, timeout_seconds: int) -> None:
    """Pod 가 Running 이 될 때까지 기다린다. 못 되면 `ExecError`."""
    import time

    namespace = current_app.config["NODE_SHELL_NAMESPACE"]
    path = (
        f"/api/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/pods/{urllib.parse.quote(name)}"
    )
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        body = _request("GET", path)
        status = body.get("status") or {}
        phase = status.get("phase") or ""
        if phase == "Running":
            return
        if phase in ("Failed", "Succeeded"):
            raise ExecError(f"노드 셸 Pod 가 {phase} 로 끝났습니다.")
        # 왜 안 뜨는지(이미지 없음 등)를 사용자에게 그대로 전한다.
        for cs in status.get("containerStatuses") or []:
            waiting = ((cs or {}).get("state") or {}).get("waiting") or {}
            last = waiting.get("reason") or last
        time.sleep(0.5)
    raise ExecError(f"노드 셸 Pod 가 시간 안에 뜨지 않았습니다{f' ({last})' if last else ''}.")


def _safe(value: str) -> str:
    """Pod 이름 규칙(소문자 영숫자와 -)으로 줄인다."""
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in value.lower())
    return cleaned.strip("-")[:40] or "node"
