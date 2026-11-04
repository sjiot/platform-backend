import json
import time
from collections import deque
from datetime import datetime

from flask import Blueprint, g, jsonify, request

from infra import latency
from infra.logger import logger
from infra.mobius import _extract_notification_con

from callbacks.warehouse import handle_warehouse
from callbacks.conveyor import handle_conveyor
from callbacks.robot_arm import handle_robot_arm
from callbacks.agv import handle_agv

bp = Blueprint("callback", __name__)

HANDLERS = {
    "cnt_warehouse": handle_warehouse,
    "cnt_conveyor": handle_conveyor,
    "cnt_robot_arm": handle_robot_arm,
    "cnt_agv": handle_agv,
}

# 최근 수신 이력 (프로세스 메모리, 재시작 시 사라짐)
RECENT = deque(maxlen=50)

def _ack(body=None, status=200):
    """oneM2M 알림 응답."""
    resp = jsonify(body if body is not None else {"ok": True})
    resp.headers["X-M2M-RSC"] = "2000"
    for h in ("X-M2M-RI", "X-M2M-RVI"):
        if request.headers.get(h):
            resp.headers[h] = request.headers[h]
    return resp, status


def _record(cnt, kind, detail):
    RECENT.appendleft({
        "at": datetime.now().isoformat(timespec="seconds"),
        "cnt": cnt,
        "kind": kind,          # verification | notification | error
        "detail": detail,
    })

@bp.route("/mobius/callback/<path:cnt>", methods=["POST"])
def mobius_callback(cnt):
    """Mobius 구독 알림 수신 (44개 컨테이너 공용)"""
    received_at = time.time()
    started = time.perf_counter()
    token = latency.begin_trace()
    g.callback_failed = False
    try:
        result = _handle_callback(cnt, received_at)
        ok = result[1] < 400 and not g.callback_failed
        latency.record("callback_handle", (time.perf_counter() - started) * 1000, ok=ok,
                       cnt=cnt, status=result[1])
        return result
    finally:
        latency.end_trace(token)


def _handle_callback(cnt, received_at):
    data = request.get_json(force=True, silent=True) or {}
    sgn = data.get("m2m:sgn") or {}

    # 구독 생성 직후 Mobius가 보내는 검증 요청 → 2xx면 구독 활성화
    if sgn.get("vrq"):
        logger.info(f"[callback:{cnt}] 구독 검증 요청 sur={sgn.get('sur')}")
        _record(cnt, "verification", {"sur": sgn.get("sur")})
        return _ack()

    rep = (sgn.get("nev") or {}).get("rep") or {}
    latency.record_subscribe(cnt, (rep.get("m2m:cin") or {}).get("rn"), received_at)

    parts = cnt.strip("/").split("/")
    group = parts[0]

    handler = HANDLERS.get(group)
    if handler is None:
        logger.warning(f"[callback] 등록되지 않은 컨테이너: {cnt}")
        _record(cnt, "error", {"reason": f"unknown container: {group}"})
        return jsonify({"error": f"unknown container: {group}"}), 404

    try:
        con = _extract_notification_con(data)
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"[callback:{cnt}] con 파싱 실패: {e}")
        _record(cnt, "error", {"reason": f"parse: {e}"})
        return jsonify({"error": "bad notification payload"}), 400

    logger.info(f"[callback:{cnt}] con={json.dumps(con, ensure_ascii=False)[:300]}")
    _record(cnt, "notification", {"con": con})

    try:
        handler(parts[1:], con)
    except Exception:
        logger.exception(f"[callback:{cnt}] 처리 실패")
        g.callback_failed = True

    return _ack()

@bp.route("/mobius/callback/_probe", methods=["GET"])
def callback_probe():
    """Mobius 서버에서 이 URL이 닿는지 확인용"""
    return jsonify({"ok": True, "at": datetime.now().isoformat(timespec="seconds")}), 200


@bp.route("/mobius/callback/_recent", methods=["GET"])
def callback_recent():
    """구독 수신 이력 조회. ?cnt=cnt_process 로 필터"""
    f = request.args.get("cnt")
    items = [r for r in RECENT if not f or r["cnt"].startswith(f)]
    return jsonify({"count": len(items), "items": items}), 200