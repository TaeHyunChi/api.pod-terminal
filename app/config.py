"""설정. 모든 값은 환경변수로 덮어쓴다(ConfigMap/Secret)."""

import os


def _csv(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "pod-terminal-service")
    API_PREFIX = os.getenv("API_PREFIX", "/api/v1")
    PORT = int(os.getenv("PORT", "8247"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    JSON_LOGS = os.getenv("JSON_LOGS", "true").lower() == "true"

    # WebSocket 은 브라우저가 헤더를 붙일 수 없어 `?token=` 쿼리로 JWT 를 받는다.
    JWT_SECRET = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
    AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() == "true"

    # --- k8s ---
    K8S_API = os.getenv("K8S_API", "https://kubernetes.default.svc")

    #: 터미널을 열어도 되는 네임스페이스. **비워 두면 아무 데도 못 붙는다.**
    #:
    #: exec 는 컨테이너 안에서 임의의 명령을 실행하는 권한이다 — 목록이 없으면
    #: 로그인한 사람이 `kube-system` 의 컨테이너에 셸을 띄울 수 있다. RBAC 으로도
    #: 막지만(Role 을 이 네임스페이스들에만 준다) 서버가 먼저 거절한다.
    ALLOWED_NAMESPACES = _csv(
        "ALLOWED_NAMESPACES",
        "oncloud-ai-platform,oncloud-ai-sandbox,oncloud-ai-devops-workspace,oncloud-ai-devops-service,"
        "oncloud-ai-model-workspace,oncloud-ai-model-serving",
    )


    # --- 다른 클러스터(클러스터 관리에 추가된 것) — app/cluster_access.py ---
    #: kubeconfig 원문을 받는 곳(서비스 토큰 전용 /clusters/{id}/credentials). 비우면 이 클러스터만.
    K8S_CLUSTER_SERVICE_URL = os.getenv(
        "K8S_CLUSTER_SERVICE_URL", "http://k8s-cluster-service.oncloud-ai-platform.svc.cluster.local:8209/api/v1"
    ).rstrip("/")
    #: 이 플랫폼 클러스터로 볼 클러스터 id·API 서버 주소(노드 주소로 등록된 경우) — ServiceAccount 로 붙는다.
    LOCAL_CLUSTER_IDS = tuple(_csv("LOCAL_CLUSTER_IDS", ""))
    LOCAL_API_SERVER_URLS = tuple(_csv("LOCAL_API_SERVER_URLS", ""))
    #: 다른 클러스터의 Pod 터미널을 열 수 있는 권한 키(관리자는 늘 가능).
    #: 노드 셸은 여기에 더해 노드 셸 자격도 필요하다.
    REMOTE_CLUSTER_PERMISSIONS = tuple(
        _csv("REMOTE_CLUSTER_PERMISSIONS", "k8s-cluster.resource.manage,k8s-cluster.cluster.manage")
    )

    # --- 노드 셸 (호스트 터미널) ---
    #: 특권 debug Pod 를 만들 네임스페이스. 앱 네임스페이스와 섞지 않는다 —
    #: RBAC 을 좁게 주고, 그 이름으로 다른 워크로드를 흉내 낼 수 없게 한다.
    NODE_SHELL_NAMESPACE = os.getenv("NODE_SHELL_NAMESPACE", "oncloud-ai-node-shell")

    # 네임스페이스 권한 설정(volume-service 0.7.0, 클러스터별) — '웹 터미널' 이 켜진 네임스페이스를 더한다
    # (app/granted.py). 기동 뒤 처음 쓸 때 한 번 읽는다('권한 적용'이 재시작). 비우면 설정 목록만 쓴다.
    VOLUME_SERVICE_URL = os.getenv("VOLUME_SERVICE_URL", "")
    #: debug Pod 이미지. 노드에 이미 있는 것을 쓴다(외부망이 막혀도 뜬다).
    #: nsenter 애플릿이 있어야 한다 — busybox 에 들어 있다.
    NODE_SHELL_IMAGE = os.getenv(
        "NODE_SHELL_IMAGE", "oncloud-ai.co.kr:5000/system/busybox:1.36"
    )
    #: 노드 셸을 열 수 있는 역할 id. **비워 두면 아무도 열 수 없다** —
    #: 노드 root 권한이라 기본값을 여는 쪽으로 두지 않는다.
    NODE_SHELL_ROLE_IDS = _csv("NODE_SHELL_ROLE_IDS", "seed-admin")
    #: 플랫폼 관리자 Role id — 권한 검사(`has_permission`)에서 관리자는 늘 모든 권한을 가진다.
    #: 다른 서비스 ConfigMap 의 ADMIN_ROLE_ID 와 같은 값이어야 한다.
    ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID", "seed-admin")
    # 관리자 전용 네임스페이스 — 허용 목록 밖이라도 관리자(ADMIN_ROLE_ID)는 연다. 일반 사용자는
    # "관리자 전용 POD 입니다."(ADMIN_ONLY). `*` 는 허용 목록 밖 전부(kube-system 포함), 비우면 끈다.
    # RBAC 도 클러스터 단위로 줘야 한다(30-rbac.yaml 의 ClusterRole).
    ADMIN_NAMESPACES = _csv("ADMIN_NAMESPACES", "*")
    #: 다른 클러스터에서 관리자만 여는 시스템 네임스페이스 — 클러스터 관리 권한자도 "관리자 전용 POD 입니다."
    #: `kube-` 로 시작하는 곳은 목록과 관계없이 포함(app/auth.py is_remote_system_namespace).
    REMOTE_ADMIN_NAMESPACES = _csv("REMOTE_ADMIN_NAMESPACES", "default,kube-system,kube-public,kube-node-lease")
    #: debug Pod 가 Running 이 될 때까지 기다리는 시간(초).
    NODE_SHELL_START_TIMEOUT = int(os.getenv("NODE_SHELL_START_TIMEOUT", "60"))
    #: debug Pod 의 최대 수명(초). 서버가 지우지 못하고 죽어도 스스로 끝난다.
    NODE_SHELL_MAX_SECONDS = int(os.getenv("NODE_SHELL_MAX_SECONDS", "3600"))

    #: 유휴 연결이 프록시에 끊기지 않도록 보내는 ping 간격(초).
    #:
    #: 터미널은 사용자가 아무것도 치지 않는 동안 아무 것도 흐르지 않는다. Kong 같은
    #: 프록시는 그런 연결을 끊으므로 주기적으로 알려 줘야 한다.
    WS_PING_SECONDS = int(os.getenv("WS_PING_SECONDS", "25"))
    #: exec 스트림을 여는 데 걸어 두는 시간(초). 연결 자체는 무한히 산다.
    K8S_CONNECT_TIMEOUT = int(os.getenv("K8S_CONNECT_TIMEOUT", "10"))


class TestConfig(Config):
    AUTH_DISABLED = True
    K8S_CLUSTER_SERVICE_URL = ""
    JSON_LOGS = False
    ALLOWED_NAMESPACES = ["oncloud-ai-devops-service", "oncloud-ai-devops-workspace"]
    NODE_SHELL_ROLE_IDS = ["seed-admin"]
