import os
import sys
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv
from flasgger import Swagger

load_dotenv()

from infra import latency
from infra.logger import logger
from infra.keys import _ensure_initial_state

from routes.order import bp as order_bp
from routes.stock import bp as stock_bp
from routes.dashboard import bp as dashboard_bp
from routes.station import bp as station_bp
from routes.callback import bp as callback_bp
from routes.admin import bp as admin_bp
from routes.calling import bp as calling_bp


def _boot_background():
    """부팅 부수작업. 워커가 여러 개여도 한 번만 돌게 Redis 락으로 막는다."""
    import threading
    from infra.extensions import r
    from services import stock, watchdog

    watchdog.start_sweeper()
    latency.start_sampler()

    def seed():
        # 구독은 '앞으로 바뀌는 것'만 알려준다. 지금 재고는 직접 읽어야 한다.
        if not r.set("stock:seed:lock", "1", nx=True, ex=60):
            return
        try:
            stock.seed_from_mobius()
        except Exception:
            logger.exception("[boot] 재고 시드 실패 — 콜백이 채울 때까지 unknown")

    threading.Thread(target=seed, name="stock-seed", daemon=True).start()


def create_app():
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config['JSON_AS_ASCII'] = False

    CORS(app, resources={r"*": {"origins": "*"}})

    swagger_config = {
        # flasgger 0.9.7.1 버그 우회 — ui3 템플릿이 이렇게 렌더한다.
        #     let auth_config = {{ flasgger_config.get("auth") | safe }};
        # 이 키가 없으면 파이썬 None 이 그대로 JS 에 박혀 ReferenceError 가 나고,
        # window.onload 가 그 줄에서 죽어 window.ui 대입까지 못 간다.
        # 그러면 Swagger UI 초기화가 안 끝나서 **모든 엔드포인트의 "Try it out" 이 사라진다.**
        "auth": {},
        "headers": [],
        "specs": [{
            "endpoint": 'apispec_1',
            "route": '/apispec_1.json',
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/apidocs/",
    }
    template = {
        "swagger": "2.0",
        "info": {
            "title": "SJIOT Keycap Order API",
            "description": "키캡 주문/상태 조회 API",
            "version": "1.0.0",
        },
        "basePath": "/",
    }
    Swagger(app, config=swagger_config, template=template)

    latency.install_request_trace(app)

    app.register_blueprint(order_bp)
    app.register_blueprint(stock_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(station_bp)
    app.register_blueprint(callback_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(calling_bp)

    _ensure_initial_state()

    # 개발 서버 리로더는 프로세스를 두 번 띄운다. 자식에서만 백그라운드를 시작한다.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "false":
        _boot_background()

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
