"""Pod 원격 터미널.

검증하는 것.

1. 허용한 네임스페이스에만 붙는다 — exec 는 컨테이너 안에서 임의의 명령을
   실행하는 권한이라 로그 조회보다 더 좁게 막아야 한다.
2. k8s exec 는 채널 번호가 앞에 붙은 프레임을 쓴다(v4.channel.k8s.io).
   입력·크기 조정은 그 규약대로 감싸고, 출력은 다시 벗겨서 화면에 준다.
3. 화면과는 JSON 으로 주고받는다 — 화면이 채널 번호까지 알 필요는 없다.
"""

import json

import pytest

from app import kube_exec
from app.api.v1 import terminals


def test_allowed_namespaces_are_listed(client):
    body = client.get("/api/v1/terminals/namespaces").get_json()
    assert "oncloud-ai-devops-service" in body["items"]


def test_healthz(client):
    assert client.get("/healthz").get_json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# 파라미터 검증
# --------------------------------------------------------------------------- #
def _params(app, query: str):
    with app.test_request_context(f"/api/v1/terminals/exec?{query}"):
        return terminals._params()


def test_namespace_and_pod_are_required(app):
    value, error = _params(app, "namespace=oncloud-ai-devops-service")
    assert value is None and "필수" in error


def test_namespace_outside_the_allowlist_is_refused(app):
    """RBAC 으로도 막히지만 서버가 먼저 거절해 이유를 분명히 알려 준다."""
    value, error = _params(app, "namespace=kube-system&pod=etcd-0")
    assert value is None
    # 어떤 네임스페이스가 있는지는 알려 주지 않는다.
    assert "kube-system" not in error


def test_container_is_optional(app):
    value, _ = _params(app, "namespace=oncloud-ai-devops-service&pod=p")
    assert value["container"] == ""


# --------------------------------------------------------------------------- #
# k8s exec 규약
# --------------------------------------------------------------------------- #
def test_exec_url_asks_for_a_tty_and_all_streams(app):
    with app.app_context():
        url = kube_exec.exec_url("ns", "p", container="app", command=["/bin/sh"])
    for expected in ("stdin=true", "stdout=true", "stderr=true", "tty=true", "container=app"):
        assert expected in url


def test_each_command_argument_is_sent_separately(app):
    """인자를 하나로 합치면 그 전체가 실행 파일 이름이 되어 exec 가 실패한다."""
    with app.app_context():
        url = kube_exec.exec_url("ns", "p", container="", command=["/bin/sh", "-c", "echo hi"])
    assert url.count("command=") == 3


def test_input_is_wrapped_in_the_stdin_channel():
    frame = kube_exec.stdin_frame("ls\n")
    assert frame[0] == kube_exec.CH_STDIN
    assert frame[1:].decode() == "ls\n"


def test_resize_is_sent_on_its_own_channel():
    """크기를 안 알려 주면 컨테이너는 80x24 로 알고 있어 vim 이 화면을 다 못 쓴다."""
    frame = kube_exec.resize_frame(120, 30)
    assert frame[0] == kube_exec.CH_RESIZE
    assert json.loads(frame[1:]) == {"Width": 120, "Height": 30}


@pytest.mark.parametrize(
    ("raw", "channel", "text"),
    [
        (bytes([kube_exec.CH_STDOUT]) + b"hello", kube_exec.CH_STDOUT, "hello"),
        (bytes([kube_exec.CH_STDERR]) + b"oops", kube_exec.CH_STDERR, "oops"),
        # 채널만 있고 내용이 없는 프레임도 온다.
        (bytes([kube_exec.CH_STDOUT]), kube_exec.CH_STDOUT, ""),
        (b"", -1, ""),
    ],
)
def test_output_channel_is_stripped(raw, channel, text):
    assert kube_exec.read_frame(raw) == (channel, text)


def test_successful_exit_has_no_message():
    """k8s 는 성공도 error 채널로 알려 준다 — 그걸 오류로 보이면 안 된다."""
    assert kube_exec.exit_message(json.dumps({"status": "Success"})) == ""


def test_failed_exit_keeps_the_reason():
    payload = json.dumps({"status": "Failure", "message": "command terminated with exit code 1"})
    assert "exit code 1" in kube_exec.exit_message(payload)


def test_shell_falls_back_when_bash_is_missing():
    """이미지마다 bash 가 있기도 없기도 하다 — 접속하는 쪽이 고르게 하지 않는다."""
    command = kube_exec.default_command()
    assert command[0] == "/bin/sh"
    assert "bash" in command[-1] and "sh -i" in command[-1]


def test_shell_is_interactive():
    """대화형이 아니면 프롬프트가 없어 "명령이 실행되지 않는다" 로 보인다."""
    script = kube_exec.default_command()[-1]
    assert "bash -i" in script and "sh -i" in script


def test_shell_keeps_stderr():
    """프롬프트는 stdout 이 아니라 stderr 로 나간다.

    `2>/dev/null` 로 bash 없음 오류를 감추려다 프롬프트까지 지웠던 적이 있다.
    """
    assert "2>/dev/null" not in kube_exec.default_command()[-1].replace(
        "command -v bash >/dev/null 2>&1", ""
    )


def test_shell_sets_term():
    """TERM 이 없으면 vim·top 이 "terminal not fully functional" 로 뜬다."""
    assert "TERM=" in kube_exec.default_command()[-1]


def test_connect_clears_the_read_timeout(app, monkeypatch):
    """붙은 뒤에도 타임아웃이 걸려 있으면 조용히 열어 둔 셸이 그때마다 끊긴다.

    실제로 그랬다 — 열리고 정확히 10초(연결 제한 시간) 뒤에 닫혔다.
    """
    import sys
    import types

    class _Conn:
        def __init__(self):
            self.timeout = 10

        def settimeout(self, value):
            self.timeout = value

    conn = _Conn()
    fake = types.ModuleType("websocket")
    fake.create_connection = lambda *a, **k: conn
    monkeypatch.setitem(sys.modules, "websocket", fake)
    monkeypatch.setattr(kube_exec, "_token", lambda: "t")

    with app.app_context():
        assert kube_exec.connect("ns", "p") is conn
    assert conn.timeout is None


# --------------------------------------------------------------------------- #
# Pod 상태 확인 — 터미널은 Running 인 Pod 에만 연다
# --------------------------------------------------------------------------- #
def _status(client, query):
    return client.get(f"/api/v1/terminals/pod-status?{query}")


def test_pod_status_requires_namespace_and_pod(client):
    assert _status(client, "namespace=oncloud-ai-devops-service").status_code == 400
    assert _status(client, "pod=p").status_code == 400


def test_pod_status_refuses_namespaces_outside_the_allowlist(client):
    res = _status(client, "namespace=kube-system&pod=etcd-0")
    assert res.status_code == 403
    assert "kube-system" not in res.get_json()["message"]


def test_pod_status_outside_cluster_is_502(client):
    res = _status(client, "namespace=oncloud-ai-devops-service&pod=p")
    assert res.status_code == 502


def test_missing_pod_is_not_available(client, monkeypatch):
    """사라진 Pod 는 exists/available 이 false — 화면은 상태 정보만 그린다."""
    monkeypatch.setattr(kube_exec, "in_cluster", lambda: True)
    monkeypatch.setattr(kube_exec, "pod_status", lambda ns, pod: None)
    body = _status(client, "namespace=oncloud-ai-devops-service&pod=gone").get_json()
    assert body["exists"] is False
    assert body["available"] is False
    assert body["message"]


def test_running_pod_is_available(client, monkeypatch):
    monkeypatch.setattr(kube_exec, "in_cluster", lambda: True)
    monkeypatch.setattr(
        kube_exec,
        "pod_status",
        lambda ns, pod: {
            "phase": "Running", "ready": True,
            "startedAt": "2026-07-30T01:00:00Z", "reason": "",
        },
    )
    body = _status(client, "namespace=oncloud-ai-devops-service&pod=web-1").get_json()
    assert body["exists"] is True and body["available"] is True


def test_terminated_pod_gets_no_terminal(client, monkeypatch):
    """로그와 다른 점 — Pod 오브젝트가 남아 있어도 Running 이 아니면 exec 는 안 된다."""
    monkeypatch.setattr(kube_exec, "in_cluster", lambda: True)
    monkeypatch.setattr(
        kube_exec,
        "pod_status",
        lambda ns, pod: {
            "phase": "Succeeded", "ready": False,
            "startedAt": "2026-07-30T01:00:00Z", "reason": "",
        },
    )
    body = _status(client, "namespace=oncloud-ai-devops-service&pod=done").get_json()
    assert body["exists"] is True
    assert body["available"] is False
    assert body["message"]
