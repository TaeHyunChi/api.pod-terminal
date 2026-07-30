"""Pod 원격 터미널 서비스.

화면이 알려 준 Pod 에 셸을 띄우고, 브라우저의 터미널과 컨테이너 사이를 중계한다.
Guacamole 과 같은 결이다 — 사용자는 아무것도 설치하지 않고 게이트웨이가 프로토콜을
바꿔 준다(k8s 의 `v4.channel.k8s.io` ↔ 화면이 읽기 쉬운 JSON).

왜 별도 서비스인가
------------------
터미널은 **응답이 끝나지 않는 연결**이고, 연결 하나가 스레드를 둘 쓴다(브라우저를
읽는 쪽과 컨테이너를 읽는 쪽). 일반 REST 서비스에 붙이면 터미널 몇 개만 열어도 그
서비스의 다른 API 가 막힌다. 로그 중계(log-stream-service)를 떼어 낸 것과 같은
이유이고, 권한이 전혀 다르다는 이유가 하나 더 있다 — 이 서비스만 `pods/exec` 를
갖는다.

DB 를 쓰지 않는다. 상태는 열려 있는 연결이 전부다.
"""

import uuid

from flask import Flask, g, request

from .api.v1 import register_v1
from .config import Config
from .errors import register_error_handlers
from .health import bp as health_bp
from .logging_config import configure_logging


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config()
    app = Flask(__name__)
    app.config.from_object(cfg)
    # 오류 문구가 한글이라 \uXXXX 로 이스케이프되지 않게 UTF-8 그대로 내보낸다.
    app.json.ensure_ascii = False

    configure_logging(app.config["LOG_LEVEL"], app.config["JSON_LOGS"])

    register_error_handlers(app)
    app.register_blueprint(health_bp)
    register_v1(app, app.config["API_PREFIX"])

    @app.before_request
    def _assign_request_id():
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex

    @app.after_request
    def _propagate_request_id(response):
        response.headers.setdefault("X-Request-ID", getattr(g, "request_id", "-"))
        return response

    return app
