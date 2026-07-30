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
        "oncloud-ai-devops-workspace,oncloud-ai-devops-service,"
        "oncloud-ai-model-workspace,oncloud-ai-model-serving",
    )


    #: 유휴 연결이 프록시에 끊기지 않도록 보내는 ping 간격(초).
    #:
    #: 터미널은 사용자가 아무것도 치지 않는 동안 아무 것도 흐르지 않는다. Kong 같은
    #: 프록시는 그런 연결을 끊으므로 주기적으로 알려 줘야 한다.
    WS_PING_SECONDS = int(os.getenv("WS_PING_SECONDS", "25"))
    #: exec 스트림을 여는 데 걸어 두는 시간(초). 연결 자체는 무한히 산다.
    K8S_CONNECT_TIMEOUT = int(os.getenv("K8S_CONNECT_TIMEOUT", "10"))


class TestConfig(Config):
    AUTH_DISABLED = True
    JSON_LOGS = False
    ALLOWED_NAMESPACES = ["oncloud-ai-devops-service", "oncloud-ai-devops-workspace"]
