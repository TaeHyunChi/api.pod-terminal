"""다른 클러스터(클러스터 관리에 추가된 것)의 터미널 — clusterId 해석·권한·허용 목록·노드 셸 네임스페이스."""

import datetime as dt

import jwt
import pytest

from app import cluster_access, kube_exec, node_shell

KUBECONFIG = """
apiVersion: v1
kind: Config
current-context: test
contexts:
- name: test
  context: {cluster: test, user: admin}
clusters:
- name: test
  cluster: {server: "https://10.74.89.47:6443", insecure-skip-tls-verify: true}
users:
- name: admin
  user: {token: remote-token}
"""


@pytest.fixture(autouse=True)
def _remote(monkeypatch):
    cluster_access.clear_cache()
    monkeypatch.setattr(
        cluster_access,
        "_credentials",
        lambda cid: {"clusterId": cid, "apiServerUrl": "https://10.74.89.47:6443", "kubeconfig": KUBECONFIG},
    )
    yield
    cluster_access.clear_cache()


def test_remote_pod_status_skips_platform_allowlist(client, monkeypatch):
    seen = {}

    def fake(ns, pod, access=None):
        seen["server"] = access.server
        return {"phase": "Running", "ready": True, "startedAt": "", "reason": "", "containers": []}

    monkeypatch.setattr(kube_exec, "pod_status", fake)
    res = client.get("/api/v1/terminals/pod-status?namespace=kube-system&pod=coredns&clusterId=c-remote")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["available"] is True
    assert seen["server"] == "https://10.74.89.47:6443"


def test_cluster_identity_of_another_cluster(client, monkeypatch):
    monkeypatch.setattr(node_shell, "node_uids", lambda access=None: ["vm-node-uid"] if not access.local else [])
    body = client.get("/api/v1/terminals/cluster-identity?clusterId=c-remote").get_json()
    assert body["available"] is True and body["nodeUids"] == ["vm-node-uid"] and body["remote"] is True


def _token(**claims):
    now = dt.datetime.now(dt.UTC)
    return jwt.encode({"sub": "u1", "exp": now + dt.timedelta(minutes=5), **claims}, "s3cret", algorithm="HS256")


def test_remote_requires_admin_or_cluster_permission(app, client, monkeypatch):
    app.config.update(AUTH_DISABLED=False, JWT_SECRET="s3cret")
    monkeypatch.setattr(kube_exec, "pod_status", lambda ns, pod, **_kw: None)
    url = "/api/v1/terminals/pod-status?namespace=default&pod=p&clusterId=c-remote"
    assert client.get(url, headers={"Authorization": f"Bearer {_token(roleIds=['seed-user'])}"}).status_code == 403
    assert client.get(url, headers={"Authorization": f"Bearer {_token(roleIds=['seed-admin'])}"}).status_code == 200
    manager = _token(roleIds=["r"], permissions=["k8s-cluster.cluster.manage"])
    assert client.get(url, headers={"Authorization": f"Bearer {manager}"}).status_code == 200
    identity = client.get(
        "/api/v1/terminals/cluster-identity?clusterId=c-remote",
        headers={"Authorization": f"Bearer {_token(roleIds=['seed-user'])}"},
    ).get_json()
    assert identity["available"] is False and identity["code"] == "FORBIDDEN"


def test_node_shell_creates_its_namespace_on_another_cluster(app, monkeypatch):
    calls = []

    def fake(method, path, payload=None, access=None):
        calls.append((method, path))
        if method == "GET" and path.startswith("/api/v1/namespaces/"):
            raise node_shell.ExecError("대상을 찾을 수 없습니다.")
        return {}

    monkeypatch.setattr(node_shell, "_request", fake)
    with app.test_request_context("/?clusterId=c-remote"):
        access = cluster_access.resolve("c-remote")
        node_shell.create_debug_pod("oncloud-test", "u1", access=access)
    assert ("POST", "/api/v1/namespaces") in calls
    assert any(m == "POST" and p.endswith("/pods") for m, p in calls)
