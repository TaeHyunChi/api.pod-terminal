"""노드 셸 — 권한 게이트 · 특권 Pod 규격 · 정리.

k8s 는 타지 않는다. 이 기능은 **노드 root 로 이어지는 권한**이라 규격과 게이트가
의도대로인지가 가장 중요하다.
"""

import datetime as dt

import jwt
import pytest

from app import node_shell
from app.api.v1.terminals import _is_node_shell_admin

BASE = "/api/v1/terminals"


@pytest.fixture(autouse=True)
def secured(app):
    """TestConfig 는 인증을 꺼 두므로(AUTH_DISABLED) 이 파일에서는 켠다.

    노드 셸의 핵심이 **권한 게이트**라, 인증이 꺼진 채로는 검증할 것이 없다.
    """
    app.config["AUTH_DISABLED"] = False
    app.config["JWT_SECRET"] = "test-secret"
    return app


def _token(app, **claims) -> str:
    payload = {"sub": "tester", "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)}
    payload.update(claims)
    return jwt.encode(payload, app.config["JWT_SECRET"], algorithm="HS256")


# --------------------------------------------------------------------------- #
# 권한 게이트 — 노드 셸은 관리자만
# --------------------------------------------------------------------------- #
def test_admin_role_is_allowed(app):
    with app.test_request_context():
        assert _is_node_shell_admin({"roleIds": ["seed-admin"]}) is True


def test_a_normal_user_is_refused(app):
    """Pod 터미널은 로그인만으로 열 수 있지만 노드 셸은 아니다."""
    with app.test_request_context():
        assert _is_node_shell_admin({"roleIds": ["seed-user"]}) is False


def test_no_roles_claim_is_refused(app):
    with app.test_request_context():
        assert _is_node_shell_admin({}) is False


def test_an_empty_role_list_locks_everyone_out(app):
    """설정이 비면 **아무도** 열 수 없다 — 여는 쪽이 기본값이면 위험하다."""
    with app.test_request_context():
        app.config["NODE_SHELL_ROLE_IDS"] = []
        assert _is_node_shell_admin({"roleIds": ["seed-admin"]}) is False


# --------------------------------------------------------------------------- #
# 상태 확인 엔드포인트
# --------------------------------------------------------------------------- #
def test_node_status_requires_auth(client):
    assert client.get(f"{BASE}/node-status?node=k3s-worker").status_code == 401


def test_node_status_requires_the_node_parameter(app, client):
    response = client.get(f"{BASE}/node-status?token={_token(app, roleIds=['seed-admin'])}")
    assert response.status_code == 400


def test_node_status_reports_not_allowed_for_a_normal_user(app, client, monkeypatch):
    """화면이 버튼을 미리 잠글 수 있어야 한다 — 붙고 나서 거절하면 원인을 모른다."""
    monkeypatch.setattr("app.kube_exec.in_cluster", lambda: True)
    monkeypatch.setattr(node_shell, "node_exists", lambda node: True)

    body = client.get(
        f"{BASE}/node-status?node=k3s-worker&token={_token(app, roleIds=['seed-user'])}"
    ).get_json()

    assert body["exists"] is True
    assert body["allowed"] is False
    assert body["available"] is False
    assert "관리자" in body["message"]


def test_node_status_reports_a_missing_node(app, client, monkeypatch):
    monkeypatch.setattr("app.kube_exec.in_cluster", lambda: True)
    monkeypatch.setattr(node_shell, "node_exists", lambda node: False)

    body = client.get(
        f"{BASE}/node-status?node=없는노드&token={_token(app, roleIds=['seed-admin'])}"
    ).get_json()
    assert body["exists"] is False
    assert body["available"] is False


def test_node_status_available_for_an_admin(app, client, monkeypatch):
    monkeypatch.setattr("app.kube_exec.in_cluster", lambda: True)
    monkeypatch.setattr(node_shell, "node_exists", lambda node: True)

    body = client.get(
        f"{BASE}/node-status?node=k3s-worker&token={_token(app, roleIds=['seed-admin'])}"
    ).get_json()
    assert body["available"] is True
    assert body["message"] == ""


# --------------------------------------------------------------------------- #
# 특권 Pod 규격 — 이 기능의 위험이 전부 여기 담긴다
# --------------------------------------------------------------------------- #
@pytest.fixture
def manifest(app):
    with app.test_request_context():
        return node_shell.debug_pod_manifest("k3s-worker", "node-shell-k3s-worker-ab12", "user-1")


def test_pod_is_pinned_to_the_target_node(manifest):
    assert manifest["spec"]["nodeName"] == "k3s-worker"


def test_pod_enters_the_host_namespaces(manifest):
    """호스트에 붙으려면 PID 네임스페이스를 공유해야 nsenter 로 PID 1 에 들어간다."""
    spec = manifest["spec"]
    assert spec["hostPID"] is True
    assert spec["containers"][0]["securityContext"]["privileged"] is True


def test_pod_tolerates_every_taint(manifest):
    """컨트롤 플레인에는 taint 가 있다 — 진단이 가장 필요한 노드가 대개 그쪽이다."""
    assert manifest["spec"]["tolerations"] == [{"operator": "Exists"}]


def test_pod_has_a_deadline(manifest):
    """서버가 지우지 못하고 죽어도 특권 Pod 가 방치되면 안 된다."""
    assert manifest["spec"]["activeDeadlineSeconds"] > 0
    assert manifest["spec"]["restartPolicy"] == "Never"


def test_pod_carries_the_platform_labels(manifest):
    """플랫폼 규약 — 생성 사용자 id 와 API 이름을 붙인다."""
    labels = manifest["metadata"]["labels"]
    assert labels["oncloud-ai/created-by"] == "user-1"
    assert labels["oncloud-ai/created-by-api"] == "pod-terminal-service"


def test_pod_lands_in_the_dedicated_namespace(app, manifest):
    """앱 네임스페이스에 섞이면 RBAC 을 좁게 줄 수 없다."""
    assert manifest["metadata"]["namespace"] == app.config["NODE_SHELL_NAMESPACE"]


def test_nsenter_enters_pid_one():
    """PID 1 은 호스트의 init 이다 — 그 네임스페이스로 들어가야 호스트 셸이다."""
    assert node_shell.NSENTER_COMMAND[:3] == ["nsenter", "-t", "1"]


# --------------------------------------------------------------------------- #
# 이름 규칙 · 정리
# --------------------------------------------------------------------------- #
def test_pod_name_is_sanitised_for_k8s():
    assert node_shell._safe("k3s-Worker.01") == "k3s-worker-01"


def test_pod_name_falls_back_when_nothing_survives():
    assert node_shell._safe("!!!") == "node"


def test_delete_never_raises(app, monkeypatch):
    """정리 실패로 사용자에게 오류를 보여 봐야 할 일이 없다(수명 제한이 받아 준다)."""
    def _raise(method, path, payload=None):
        raise node_shell.ExecError("k8s 다운")

    monkeypatch.setattr(node_shell, "_request", _raise)
    with app.test_request_context():
        node_shell.delete_debug_pod("node-shell-x")  # 예외가 나오면 실패다
