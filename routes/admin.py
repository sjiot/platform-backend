"""관리자 경로 — fault 해제 · 라인 초기화 · 조립대 호출.

fault 는 stage 를 그대로 둔 채 주문만 멈춰 세운다. 그래서 해제는 "그 자리에서 이어가기"다.
새로 밀어주는 건 `fault` 가 빠진 최신 스냅샷 하나뿐이고, 창고·AGV 는
같은 order_id 에 fault 가 없으면 해결된 것으로 본다 (명세 4-3).
"""
import json
from datetime import datetime

from flask import Blueprint, jsonify, request

from config import (QUEUE_KEY,
                    AGV_KEY,
                    STATION_KEY,
                    STATION_ORDER_PREFIX,
                    WAREHOUSE_ORDER_KEY,
                    STATION_RESET_PASSWORD,
                    ARCHIVE_PREFIX,
                    KST,
                    PAPER_KEY,
                    PAPER_MAX_COUNT,
                   )
from infra.extensions import r
from infra.logger import logger
from services import agv_fault, recovery, warehouse_fault
from services import line as line_state
from services.archive import abort_order
from services import snapshot as debug_snapshot
from services.process import clear_fault, clear_faults_at
from services.station_service import STATIONS, close_call, open_call, open_calls, start_order
from services.watchdog import CALL_EXTEND, MAX_CALL_EXTENDS
from services import stock as stock_svc
from services.stock import get_stocks, max_count, ALL_SLOTS as STOCK_SLOTS

bp = Blueprint("admin", __name__)

HARD_RESET_CONFIRM = "HARD-RESET"

STATION_IDS = ("1", "2", "3")

# 창고 재고 최대 수량 (2026-09-09 팀장님 확인값)
WAREHOUSE_MAX = {
    "board": {"g": 325, "b": 325, "r": 325, "y": 325},
    "keycap": {
        "E": {"g": 158, "b": 157, "r": 158, "y": 157},
        "I": {"g": 143, "b": 142, "r": 143, "y": 142},
        "S": {"g": 168, "b": 167, "r": 168, "y": 167},
        "N": {"g": 133, "b": 132, "r": 133, "y": 132},
        "T": {"g": 138, "b": 137, "r": 138, "y": 137},
        "F": {"g": 163, "b": 162, "r": 163, "y": 162},
        "J": {"g": 148, "b": 147, "r": 148, "y": 147},
        "P": {"g": 153, "b": 152, "r": 153, "y": 152},
    },
}

def _to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _max_for_part(part):
    """
    STOCK 슬롯 이름(part)의 1칸 최대 수량 — 키캡 40 · 보드 3 (config, 리셋과 같은 값).
    보드: "r"·"y"·"g"·"b" (밑줄 없음)
    키캡: "E_r" 형식 (글자_색상)
    존재하지 않는 part면 None.
    """
    if part not in STOCK_SLOTS:
        return None
    return max_count(part)


def _live_order_ids():
    """대기열 + 조립대 + 창고가 지금 들고 있는 주문. 최대 7건."""
    ids = list(r.lrange(QUEUE_KEY, 0, -1))
    for sid in STATION_IDS:
        oid = r.get(f"{STATION_ORDER_PREFIX}{sid}")
        if oid and oid not in ids:
            ids.append(oid)
    oid = r.get(WAREHOUSE_ORDER_KEY)
    if oid and oid not in ids:
        ids.append(oid)
    return ids


def _get_order_summary(order_id):
    order_hash = r.hgetall(f"order:{order_id}")
    if not order_hash:
        return None

    colors_raw = order_hash.get("colors", "")
    return {
        "order_id": order_id,
        "board": order_hash.get("board"),
        "keycap": order_hash.get("keycap"),
        "colors": colors_raw.split(",") if colors_raw else [],
        "stage": order_hash.get("stage"),
        "fault": order_hash.get("fault") or None,
        "station_id": order_hash.get("station_id"),
        "order_seq": _to_int(order_hash.get("order_seq")),
        "created_at": order_hash.get("created_at"),
    }


def _today_archive_key():
    return f"{ARCHIVE_PREFIX}{datetime.now(KST).strftime('%Y%m%d')}"


def _get_archived_orders_today():
    archive = r.hgetall(_today_archive_key())
    orders = []
    for order_id, raw in archive.items():
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(f"[admin] 아카이브 레코드 파싱 실패: {order_id}")
            continue

        colors_raw = record.get("colors", "")
        colors = colors_raw.split(",") if isinstance(colors_raw, str) and colors_raw else (colors_raw or [])

        orders.append({
            "order_id": record.get("order_id", order_id),
            "board": record.get("board"),
            "keycap": record.get("keycap"),
            "colors": colors,
            "stage": record.get("ended_at_stage") or record.get("final_status"),
            "fault": record.get("fault") or None,
            "station_id": record.get("station_id"),
            "order_seq": _to_int(record.get("order_seq")),
            "created_at": record.get("created_at"),
            "ended_at": record.get("ended_at"),
            "reason": record.get("reason"),
        })
    return orders


@bp.route("/admin/faults", methods=["GET"])
def list_faults():
    """
    fault 로 멈춰 있는 주문 목록
    ---
    tags:
      - Admin
    responses:
      200:
        description: 조회 성공
        schema:
          type: object
          properties:
            faults:
              type: array
              items:
                type: object
              example: [{"order_id": "ord_a1b2c3d4", "stage": "loaded", "fault": "drop_failed",
                         "fault_section": "agv", "station_id": "2"}]
    """
    out = []
    for oid in _live_order_ids():
        o = r.hgetall(f"order:{oid}")
        if o.get("fault"):
            out.append({
                "order_id": oid,
                "stage": o.get("stage"),
                "fault": o.get("fault"),
                "fault_section": o.get("fault_section"),
                "station_id": o.get("station_id"),
                "order_seq": _to_int(o.get("order_seq")),
            })
    return jsonify({"faults": out,
                    "agv_fault": agv_fault.items(),
                    "warehouse_fault": warehouse_fault.items()}), 200


@bp.route("/admin/agv/fault/clear", methods=["POST"])
def clear_agv_fault():
    """
    주문 없는 AGV 고장 해제 — cnt_process 의 agv_fault 를 null 로 올린다
    ---
    tags:
      - Admin
    description: |
      대상 주문이 없는 AGV 고장(agv_fault)을 전부 해제한다. 관리자 해제로만 풀린다.
      주문에 붙은 AGV fault(orders.{id}.fault)는 /admin/order/{id}/fault/clear 로 푼다.
      해제 뒤 배정·새 주문 접수가 다시 열리고, 대기 주문 배정을 한 번 시도한다.
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            password:
              type: string
            parked:
              type: boolean
              default: false
              description: "AGV 를 대기장소에 직접 옮겨 parked 보고가 오지 않는 경우 true — agv:occupancy 를 idle 로 돌린다"
    responses:
      200:
        description: 해제 완료
        schema:
          type: object
          example: {"ok": true, "cleared": ["agv_broken"], "agv": "idle", "assigned": null}
      400:
        description: 비밀번호 불일치
      409:
        description: 주문 없는 AGV 고장이 없음
    """
    from services.dispatch import try_assign_next
    from services.process import push_process

    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400
    if not agv_fault.active():
        return jsonify({"error": "주문 없는 AGV 고장이 없습니다"}), 409

    cleared = agv_fault.clear()
    agv_fault.clear_status_if_fault()
    if data.get("parked"):
        r.set(AGV_KEY, "idle")
    logger.info(f"[admin] AGV 고장 해제 {cleared}" + (" · occupancy idle" if data.get("parked") else ""))

    push_process()                  # agv_fault: null
    assigned = try_assign_next()

    return jsonify({
        "ok": True,
        "cleared": cleared,
        "agv": r.get(AGV_KEY),
        "assigned": assigned,
    }), 200


@bp.route("/admin/order/<order_id>/fault/clear", methods=["POST"])
def clear_order_fault(order_id):
    """
    fault 해제 — stage 는 그대로 두고 그 자리에서 이어간다
    ---
    tags:
      - Admin
    parameters:
      - in: path
        name: order_id
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            password:
              type: string
    responses:
      200:
        description: 해제 완료
      400:
        description: 비밀번호 불일치
      404:
        description: 존재하지 않는 주문
      409:
        description: fault 가 걸려 있지 않은 주문
    """
    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400

    order = r.hgetall(f"order:{order_id}")
    if not order:
        return jsonify({"error": "존재하지 않거나 만료된 주문입니다"}), 404

    fault = order.get("fault")
    if not fault:
        return jsonify({"error": "fault 가 걸려 있지 않습니다", "stage": order.get("stage")}), 409

    clear_fault(order_id)
    logger.info(f"[admin] {order_id} fault 해제 ({fault}) stage={order.get('stage')} 유지")

    return jsonify({
        "ok": True,
        "order_id": order_id,
        "cleared": fault,
        "stage": order.get("stage"),
    }), 200


@bp.route("/admin/warehouse/fault/clear", methods=["POST"])
def clear_warehouse_fault():
    """
    주문 없는 창고 고장 해제 — cnt_process 의 warehouse_fault 항목을 지운다
    ---
    tags:
      - Admin
    description: |
      warehouse_fault 항목만 지운다. fault_section 을 주면 그 항목 하나, 없으면 전부.
      남은 항목이 없으면 cnt_process 에 warehouse_fault: null 이 나가고 새 주문 접수가 다시 열린다.
      칸 상태(disable·empty)와 선반 차단은 건드리지 않는다 — 그 칸을 쓰는 조합은 계속 품절로 막히고,
      창고가 idle·busy 를 올리거나 /admin/stock/revive 로 되살릴 때 풀린다.
      revive 가 409 로 안 되는 경우(고장 뒤 empty 가 와서 disable 이 아님 등)에 쓴다.
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            password:
              type: string
            fault_section:
              type: string
              description: "지울 항목. 예: keycap_cartridge:keycap1:E_r · keycap_cartridge:keycap1. 생략하면 전부"
    responses:
      200:
        description: 해제 완료
        schema:
          type: object
          example: {"ok": true, "cleared": ["keycap_cartridge:keycap1:E_r"], "warehouse_fault": null}
      400:
        description: 비밀번호 불일치
      409:
        description: 지울 항목이 없음 (고장 없음 또는 그 fault_section 없음)
    """
    from services.process import push_process

    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400

    section = data.get("fault_section")
    if section:
        cleared = warehouse_fault.remove([str(section)])
    else:
        cleared = warehouse_fault.clear()
    if not cleared:
        return jsonify({"error": "지울 창고 고장이 없습니다",
                        "fault_section": section,
                        "warehouse_fault": warehouse_fault.items()}), 409

    logger.info(f"[admin] 창고 고장 해제 {cleared}")
    push_process()                  # 남은 게 없으면 warehouse_fault: null

    return jsonify({
        "ok": True,
        "cleared": cleared,
        "warehouse_fault": warehouse_fault.items(),
    }), 200


@bp.route("/admin/order/<order_id>/abort", methods=["POST"])
def admin_abort_order(order_id):
    """
    주문 하나만 중단 — 라인 전체를 리셋하지 않고 이 주문만 끝낸다
    ---
    tags:
      - Admin
    description: |
      fault 해제(그 자리에서 이어가기)로 살릴 수 없는 주문 하나를 정리한다. 다른 주문은 건드리지 않는다.

      **창고 구간(assigned~board_packed)은 벨트 위 트레이·부품을 사람이 먼저 치운 뒤에 누른다.**
      창고를 풀면 바로 다음 주문이 배정돼 벨트가 다시 돈다.

      | stage | 처리 |
      |---|---|
      | queued | 대기열에서 빼고 아카이브 |
      | assigned ~ board_packed | completed — 창고·조립대 해제 |
      | pickup_reached | completed — 픽업대 트레이는 사람이 회수 |
      | loaded · arrived | discard=true(기본) → unclaimed, AGV 가 폐기 후 조립대 해제 / false → completed (사람이 회수) |
      | verified · received | completed |
      | unclaimed | 폐기 보고를 기다리지 않고 조립대 강제 해제 |
      | completed | 409 |

      아카이브에 reason=admin_abort 로 남고 대시보드 완료 통계에는 안 잡힌다.
    parameters:
      - in: path
        name: order_id
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          example:
            password: "관리자 비밀번호"
            discard: true
          properties:
            password:
              type: string
            discard:
              type: boolean
              default: true
              description: "loaded·arrived 전용. true=AGV 가 폐기(unclaimed 경로), false=트레이를 사람이 회수했다"
    responses:
      200:
        description: 중단 완료. action 에 사람이 확인할 일이 적혀 있다
        schema:
          type: object
          example: {"ok": true, "order_id": "ord_a1b2c3d4", "from_stage": "arrived",
                    "final_stage": "unclaimed", "fault": "drop_failed", "station_id": "2",
                    "assigned_next": [],
                    "action": "AGV 가 폐기장소로 가는지 확인. discarded 가 오면 조립대가 풀린다 (안 오면 300초 뒤 워치독이 강제 해제)"}
      400:
        description: 비밀번호 불일치
      404:
        description: 존재하지 않거나 만료된 주문
      409:
        description: 이미 끝난 주문
    """
    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400

    try:
        result = abort_order(order_id, discard=data.get("discard", True) is not False)
    except LookupError:
        return jsonify({"error": "존재하지 않거나 만료된 주문입니다"}), 404
    except ValueError as e:
        return jsonify({"error": str(e), "stage": r.hget(f"order:{order_id}", "stage")}), 409

    logger.info(f"[admin] {order_id} 중단 {result['from_stage']} -> {result['final_stage']}")
    return jsonify({"ok": True, **result}), 200


@bp.route("/admin/order/<order_id>/start", methods=["POST"])
def admin_start_order(order_id):
    """
    QR 인증 대신 처리 — 손님이 QR을 찍지 못할 때 관리자가 인증한다
    ---
    tags:
      - Admin
    description: |
      비밀번호를 확인한 뒤 `POST /station/<order_id>/start` 와 같은 경로로 처리한다.
      검사 항목과 stage 전이도 같아서 arrived 에서만 통과한다.
    parameters:
      - in: path
        name: order_id
        type: string
        required: true
      - in: body
        name: body
        required: true
        schema:
          type: object
          example:
            password: "관리자 비밀번호"
            station_id: "1"
          properties:
            password:
              type: string
            station_id:
              type: string
              description: 주문이 배정된 조립대 번호
    responses:
      200:
        description: 인증 처리 성공
      400:
        description: 비밀번호 불일치 · 아직 조립대에 배정되지 않은 주문
      403:
        description: station_id 불일치
      404:
        description: 존재하지 않거나 만료된 주문
      409:
        description: arrived 가 아니거나 이 조립대의 현재 주문이 아님
    """
    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400

    payload, status = start_order(order_id, data.get("station_id", ""))
    if status == 200:
        logger.info(f"[admin] {order_id} QR 인증 대신 처리 station={payload['station_id']}")
    return jsonify(payload), status


@bp.route("/admin/reset", methods=["POST"])
def admin_reset():
    """
    라인 초기화 — 전체 재고 / 상태 / 진행 중 주문 복구
    ---
    tags:
      - Admin
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          example:
            password: "관리자 비밀번호"
            mode: "zero"
            confirm: ""
            keep_queue: false
            reset_seq: false
            flush_unknown: false
          properties:
            password:
              type: string
              description: "관리자 비밀번호 (STATION_RESET_PASSWORD 와 동일)"
            mode:
              type: string
              enum: [zero, mobius, hard]
              default: zero
              description: "zero=전부 초기값(키캡 32칸=40 · 보드 4칸=3, 전 설비 idle, 진행 중 주문 삭제) · mobius=zero 뒤 Mobius 최신값으로 복구 · hard=zero + 아카이브·집계·일련번호까지 삭제(confirm 필요)"
            confirm:
              type: string
              description: "mode=hard 전용·필수. 정확히 HARD-RESET 이어야 한다. 비밀번호만으로는 오타 한 번에 90일치 기록이 날아가서 한 겹 더 둔다"
            keep_queue:
              type: boolean
              default: false
              description: "대기열(order:queue)과 그 주문 해시를 보존할지. 대기열은 Mobius 어디에도 없어서 지우면 복구가 불가능하다"
            reset_seq:
              type: boolean
              default: false
              description: "mode=hard 전용. process:seq 까지 리셋. 머신은 seq 가 안 오르면 무시하므로, 리셋하면 창고·AGV 머신도 같이 재시작해야 한다"
            flush_unknown:
              type: boolean
              default: false
              description: "mode=hard 전용. 이 서비스가 쓰지 않는 키까지 삭제. 실 Redis 는 배포본과 같은 DB 라 기본은 목록만 보고한다"
    responses:
      200:
        description: 초기화 완료
      400:
        description: 비밀번호 불일치쨌지원 안 하는 mode쨌confirm 누락
      500:
        description: 초기화 중 오류
    """
    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400

    mode = str(data.get("mode") or "zero")
    if mode not in ("zero", "mobius", "hard"):
        return jsonify({"error": "mode 는 zero 쨌 mobius 쨌 hard 중 하나여야 합니다",
                        "got": mode}), 400

    if mode == "hard" and data.get("confirm") != HARD_RESET_CONFIRM:
        return jsonify({
            "error": f'mode=hard 는 confirm="{HARD_RESET_CONFIRM}" 이 필요합니다',
            "will_delete": ["orders:archive:*", "order:counter:*",
                            "상태·진행 중 주문 전부"],
        }), 400

    try:
        report = recovery.reset(
            mode=mode,
            keep_queue=bool(data.get("keep_queue")),
            reset_seq=bool(data.get("reset_seq")) and mode == "hard",
            flush_unknown=bool(data.get("flush_unknown")) and mode == "hard",
        )
    except Exception as e:
        logger.exception("[admin] 초기화 실패")
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, **report}), 200


# ── 영업 상태 (services/line.py) ────────────────────────────────────────
@bp.route("/admin/line", methods=["GET"])
def get_line():
    """
    영업 상태 — 접수 중인지 · 남은 주문 · 장비를 꺼도 되는지
    ---
    tags:
      - Admin
    responses:
      200:
        description: |
          closed 여도 이미 받은 주문은 끝까지 돈다. remaining 이 0 이고 AGV 가 대기장소로
          돌아오면(agv=idle) drained=true — 그때 장비를 꺼도 된다.
          노쇼 주문은 AGV 가 폐기를 마칠 때까지 remaining 에 남는다.
        schema:
          type: object
          properties:
            status:
              type: string
              enum: [open, closed]
            changed_at:
              type: string
            queue:
              type: integer
            in_progress:
              type: integer
            remaining:
              type: integer
            agv:
              type: string
            drained:
              type: boolean
          example: {"status": "closed", "changed_at": "2026-10-02T18:00:03+09:00",
                    "queue": 1, "in_progress": 2, "remaining": 3, "agv": "busy", "drained": false}
    """
    return jsonify(line_state.summary()), 200


@bp.route("/admin/line", methods=["POST"])
def set_line():
    """
    오픈 / 마감 전환
    ---
    tags:
      - Admin
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          example:
            password: "관리자 비밀번호"
            status: "open"
          properties:
            password:
              type: string
            status:
              type: string
              enum: [open, closed]
              description: "closed 는 새 주문 접수(POST /order)만 막는다. 대기열·진행 중 주문은 끝까지 돈다. 리셋(/admin/reset)을 하면 자동으로 closed 가 된다"
    responses:
      200:
        description: 전환 완료. GET /admin/line 과 같은 요약 + previous(직전 상태)
      400:
        description: 비밀번호 불일치 · 잘못된 status
    """
    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400

    value = str(data.get("status") or "")
    if value not in line_state.STATUSES:
        return jsonify({"error": "status 는 open · closed 중 하나여야 합니다", "got": value}), 400

    prev = line_state.set_status(value)
    logger.info(f"[admin] 영업 상태 {prev} -> {value}")
    return jsonify({"ok": True, "previous": prev, **line_state.summary()}), 200


# 디버깅 스냅샷 (services/snapshot.py)
@bp.route("/admin/debug/snapshot", methods=["GET"])
def admin_debug_snapshot():
    """
    디버깅 스냅샷 — 상태 키 전부 + 주문별 collector·마감 + 불변식 검사 (읽기 전용)
    ---
    tags:
      - Admin
    description: |
      redis-cli 로 키를 하나씩 열어보던 걸 대신한다. 아무것도 바꾸지 않는다.

      - **violations** 가 빈 리스트면 Redis 상태끼리 서로 맞다. 리셋 직후엔 이것만 보면 된다.
      - **mobius=1** 이면 Redis 가 마지막으로 보낸 스냅샷과 머신이 보는 cnt_process 최신 CIN 을 대조한다.
        mobius.warning 이 있으면 머신은 Redis 와 다른 걸 보고 있다.
      - **ok** = violations 없음 + mobius.warning 없음.

      단계마다 저장하고 직전과 비교하려면 로컬에서 `python debug/snapshot.py --watch 2`.
      sync 워커 1개라 이 요청이 도는 동안 콜백이 줄을 선다 — 운영 중 연타 금지.
    parameters:
      - in: query
        name: mobius
        type: integer
        enum: [0, 1]
        default: 0
        description: 1 이면 Mobius cnt_process·cnt_agv 최신값과 대조 (Mobius GET 2건 추가)
    responses:
      200:
        description: 스냅샷
        schema:
          type: object
          properties:
            ok:
              type: boolean
            violations:
              type: array
              items:
                type: string
              example: ["warehouse:current_order=ord_a1b2c3d4 인데 occupancy='idle'"]
            line:
              type: object
            queue:
              type: array
              items:
                type: string
            warehouse:
              type: object
            stations:
              type: object
            agv:
              type: object
            orders:
              type: object
              description: order:ord_* 전부. live=false 는 끝난 뒤 TTL(10분) 동안 남은 것
            process:
              type: object
            stock:
              type: object
            mobius:
              type: object
    """
    include = request.args.get("mobius", "").lower() in ("1", "true", "yes")
    return jsonify(debug_snapshot.build(include_mobius=include)), 200


# 상태는 station:call:{sid}
# 누적 횟수만 order:{id}.call_count

def _bad_station(station_id):
    if str(station_id) not in STATIONS:
        return jsonify({"error": f"없는 조립대입니다: {station_id}"}), 404
    return None

@bp.route('/admin/station/<station_id>/call', methods=['POST'])
def station_admin_call(station_id):
    """
    관리자 호출 — 조립대 사용자가 도움을 요청한다
    ---
    tags:
      - Admin
    parameters:
      - in: path
        name: station_id
        type: string
        required: true
        description: 조립대 번호 (기기 자체 고정값). order_id 는 서버가 station:current_order 에서 꺼낸다
        example: 1
    responses:
      200:
        description: |
          호출 접수. 배정된 주문이 없어도 접수한다 ('부품이 안 온다' 가 호출의 주 용도 중 하나라
          주문 없음을 이유로 거절하면 그 케이스가 막힌다). 이미 호출 중이면 already_open=true 로
          200 을 준다 — called_at 은 최초값을 유지한다.
        schema:
          type: object
          properties:
            ok:
              type: boolean
            station_id:
              type: string
              example: "1"
            order_id:
              type: string
              example: ord_a1b2c3d4
            stage:
              type: string
              example: verified
            called_at:
              type: string
              example: "2026-09-18T14:03:11+09:00"
            waited_sec:
              type: integer
              example: 0
            call_count:
              type: integer
              description: 이 주문의 누적 호출 횟수
              example: 1
            extended_sec:
              type: integer
              description: 이번 호출로 stage 마감을 밀어준 초 (0 이면 연장 대상 stage 가 아니거나 상한 소진)
              example: 180
            already_open:
              type: boolean
      404:
        description: 없는 조립대 번호
    """
    bad = _bad_station(station_id)
    if bad:
        return bad

    state, already, extended = open_call(station_id)

    body = {"ok": True, "already_open": already, "extended_sec": extended, **state}
    oid = state.get("order_id")
    if oid:
        od = r.hgetall(f"order:{oid}")
        body["board"] = od.get("board")
        body["keycap"] = od.get("keycap")
        body["colors"] = [c for c in (od.get("colors") or "").split(",") if c]
        body["call_count"] = int(od.get("call_count") or 0)
    return jsonify(body), 200

@bp.route('/admin/station/<station_id>/call/ok', methods=['POST'])
def station_admin_call_ok(station_id):
    """
    호출 해결 — 관리자가 조치를 마쳤다
    ---
    tags:
      - Admin
    parameters:
      - in: path
        name: station_id
        type: string
        required: true
        example: 1
    responses:
      200:
        description: |
          호출 상태를 지우고, 해결 시점부터 기본 마감을 다시 잰다 (조치가 끝나도 머신이
          다시 움직일 시간은 필요하다). 종료된 주문·노쇼 폐기 대기 중이면 마감은 건드리지 않는다.
        schema:
          type: object
          properties:
            ok:
              type: boolean
            station_id:
              type: string
              example: "1"
            order_id:
              type: string
              example: ord_a1b2c3d4
            waited_sec:
              type: integer
              description: 호출부터 해결까지 걸린 초
              example: 74
            count:
              type: integer
              description: 이번 호출에서 버튼이 눌린 횟수
              example: 2
      404:
        description: 없는 조립대 번호
      409:
        description: 호출 중이 아닌 조립대
    """
    bad = _bad_station(station_id)
    if bad:
        return bad

    state = close_call(station_id)
    if state is None:
        return jsonify({"error": "호출 중이 아닙니다", "station_id": str(station_id)}), 409

    return jsonify({"ok": True, **state}), 200

@bp.route('/admin/calls', methods=['GET'])
def list_calls():
    """
    열려 있는 호출 목록 — 관리자 화면용 (오래 기다린 순)
    ---
    tags:
      - Admin
    description: |
      호출 상태는 조립대 소유(station:call:{sid})다. 주문 없이 걸린 호출은 order_id 가 null 이다.
      조립대 점유·배정 주문은 GET /admin/stations 를 본다.
    responses:
      200:
        description: 열린 호출. 없으면 빈 배열
        schema:
          type: object
          properties:
            calls:
              type: array
              items:
                type: object
              example: [{"station_id": "3", "order_id": "ord_a1b2c3d4", "order_seq": "9",
                         "stage": "verified", "fault": null,
                         "called_at": "2026-10-01T11:24:03+09:00", "waited_sec": 74, "count": 2}]
    """
    return jsonify({"calls": open_calls()}), 200


@bp.route('/admin/stock', methods=['GET'])
def admin_stock():
    """
    창고 재고 현황 (관리자 페이지용) — services.stock.get_stocks() 재사용 + 칸 최대(키캡 40 · 보드 3) 추가
    ---
    tags:
      - Admin
    responses:
      200:
        description: 재고 수량 + 최대 수량
      503:
        description: 재고 캐시가 아직 없음
    """
    stocks = get_stocks()
    if stocks is None:
        return jsonify({"error": "재고 정보가 아직 없습니다"}), 503

    # status 도 같이 싣는다 — 관리자 페이지가 disable 칸을 구분해 '고장 해제'로 보내야 한다.
    # 창고 초기화 CIN 은 status 가 null 로 오므로 그때는 "unknown" 이다 (services/stock.get_status 와 동일).
    states = stock_svc.all_states()
    dead_shelves = stock_svc.driver_failed_shelves()

    def _status(slot):
        return (states.get(slot) or {}).get("status") or "unknown"

    result = {"board": {}, "keycap": {}}

    for color, qty in stocks.get("board", {}).items():
        result["board"][color] = {
            "qty": qty,
            "max": max_count(color),
            "status": _status(color),
        }

    for slot, qty in stocks.get("keycap", {}).items():
        letter, _, color = slot.partition("_")
        result["keycap"].setdefault(letter, {})
        result["keycap"][letter][color] = {
            "qty": qty,
            "max": max_count(slot),
            "status": _status(slot),
            "shelf_failed": stock_svc.shelf_of(slot) in dead_shelves,   # 선반 드라이버 고장이면 idle 이어도 못 낸다
        }

    return jsonify(result), 200

@bp.route('/admin/stock/refill', methods=['POST'])
def admin_stock_refill():
    """
    재고 채움 업데이트 (관리자 페이지 "채우기" 버튼)
    ---
    tags:
      - Admin
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            part:
              type: string
              description: 재고 칸 이름 — 보드는 "r"쨌"y"쨌"g"쨌"b", 키캡은 "E_r" 형식(글자_색상)
              example: "E_r"
            count:
              type: integer
              description: >
                생략하면 해당 칸의 최대치로 자동 채움("채우기" 버튼 기본 동작).
                값을 보내면 그 수량으로 채우되 최대치를 넘지 못하게 자동으로 잘림.
              example: 30
    responses:
      200:
        description: 업데이트 완료. 최대치로 잘렸으면 clamped=true.
      400:
        description: part 누락쨌존재하지 않는 칸쨌count 형식 오류
    """
    data = request.get_json(silent=True) or {}
    part = data.get("part")
    raw_count = data.get("count")

    if not part or not isinstance(part, str):
        return jsonify({"error": "part가 필요합니다 (예: 'E_r' 또는 'r')"}), 400

    max_qty = _max_for_part(part)
    if max_qty is None:
        return jsonify({"error": f"존재하지 않는 재고 칸입니다: {part}"}), 400

    existing_raw = r.hget("warehouse:stock", part)
    if existing_raw is None:
        return jsonify({"error": f"아직 재고 데이터가 없는 칸입니다: {part}"}), 400
    existing = json.loads(existing_raw)

    clamped = False
    if raw_count is None:
        # count 생략 = "채우기" 버튼 기본 동작 — 자동으로 최대치까지
        count = max_qty
    else:
        if not isinstance(raw_count, int) or raw_count < 0:
            return jsonify({"error": "count는 0 이상의 정수여야 합니다"}), 400
        if raw_count > max_qty:
            count = max_qty
            clamped = True
        else:
            count = raw_count

    # status는 count에 맞춰 자동 조정하되, disable(하드웨어 고장)은 채운다고 해결되는 게
    # 아니므로 절대 건드리지 않는다 (Redis ERD 6-3장 restock_list 원칙과 동일).
    if existing.get("status") != "disable":
        existing["status"] = "idle" if count > 0 else "empty"
    existing["count"] = count
    r.hset("warehouse:stock", part, json.dumps(existing))

    # 창고에 "이 칸을 채웠다" 를 알리는 건 여기뿐이다. 창고가 0 을 보고했을 때가 아니라
    # 사람이 실제로 채우고 이 버튼을 눌렀을 때 cnt_process 의 *_restock 에 들어간다.
    # 창고가 그 칸에 count>0 을 올리면 callbacks/warehouse.py 가 목록에서 뺀다.
    if count > 0:
        stock_svc.mark_restocked(part)
    else:
        stock_svc.clear_restocked(part)          # 0 으로 되돌림 = 창고가 반영할 게 없다

    logger.info(f"[admin] 재고 채움: {part} -> {count} (max={max_qty}, clamped={clamped})")

    # 채워졌으니 이 칸 때문에 기다리던 주문을 다시 배정해 본다. 배정이 없어도
    # on_stock_recovered 가 push_process 를 불러 restock 목록이 창고로 나간다.
    from services.dispatch import on_stock_recovered
    assigned = on_stock_recovered()

    return jsonify({
        "ok": True,
        "part": part,
        "count": count,
        "max": max_qty,
        "clamped": clamped,
        "status": existing.get("status"),
        "restock": count > 0,
        "assigned": assigned,
    }), 200


@bp.route('/admin/stock/revive', methods=['POST'])
def admin_stock_revive():
    """
    고장(disable) 칸 해제 — 관리자 페이지에서 disable 칸을 눌렀을 때
    ---
    tags:
      - Admin
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            part:
              type: string
              description: 재고 칸 이름 — 보드는 "r"·"y"·"g"·"b", 키캡은 "E_r" 형식
              example: "E_r"
            password:
              type: string
              description: "관리자 비밀번호 (STATION_RESET_PASSWORD). '고쳤다'는 선언이라 fault 해제와 같은 잠금"
    responses:
      200:
        description: >
          idle 로 되돌림. 그 선반의 드라이버 고장 표시가 있었으면 같이 지우고 shelf_cleared=true.
          이 칸·선반을 가리키는 주문 fault 가 있었으면 같이 해제하고 faults_cleared 에 주문 id 를 싣는다
          (cnt_process 에 fault·fault_section 이 null 로 나간다).
          주문 없는 창고 고장(cnt_process warehouse_fault)의 이 칸·선반 항목도 지우고
          warehouse_faults_cleared 에 그 fault_section 을 싣는다. 남은 항목이 없으면 warehouse_fault 는 null.
      400:
        description: 비밀번호 불일치 · part 누락 · 존재하지 않는 칸 · 재고 데이터 없음
      409:
        description: disable 상태가 아닌 칸
    """
    data = request.get_json(silent=True) or {}
    if data.get("password") != STATION_RESET_PASSWORD:
        return jsonify({"error": "비밀번호가 틀렸습니다"}), 400

    part = data.get("part")
    if not part or not isinstance(part, str):
        return jsonify({"error": "part가 필요합니다 (예: 'E_r' 또는 'r')"}), 400
    if part not in STOCK_SLOTS:
        return jsonify({"error": f"존재하지 않는 재고 칸입니다: {part}"}), 400

    cur = stock_svc.get(part)
    if cur is None:
        return jsonify({"error": f"아직 재고 데이터가 없는 칸입니다: {part}"}), 400
    if cur.get("status") != "disable":
        return jsonify({"error": f"{part} 는 disable 상태가 아닙니다",
                        "status": cur.get("status")}), 409

    # count 는 창고가 보낸 마지막 값 그대로 둔다 (services/stock.revive_slots 원칙).
    # 그 칸이 선반 드라이버 고장으로도 막혀 있으면 같이 푼다 — 사람이 '고쳤다'고 누른 것이고,
    # 창고가 그 선반에서 정상 status 를 올리면 어차피 풀리는 표시다 (callbacks/warehouse.py).
    revived = stock_svc.revive_slots([part])
    shelf = stock_svc.shelf_of(part)
    shelf_cleared = bool(stock_svc.clear_driver_failed(shelf)) if shelf else False
    logger.info(f"[admin] {part} disable→idle (count={cur.get('count')})"
                + (f" · 선반 {shelf} 드라이버 고장 표시 해제" if shelf_cleared else ""))

    # 이 칸(또는 선반) 때문에 걸린 주문 fault 도 같은 선언으로 푼다 — 안 풀면 cnt_process 에
    # fault·fault_section 이 계속 실려 창고가 멈춘 채로 본다. clear_fault 가 push 까지 한다.
    faults_cleared = clear_faults_at(part)
    if faults_cleared:
        logger.info(f"[admin] {part} 되살림 → 주문 fault 해제 {faults_cleared}")
    # 주문 없는 창고 고장(warehouse_fault)도 그 칸 + 그 선반 항목을 지운다.
    # push 는 아래 on_stock_recovered 가 한다 (배정이 없어도 push_process).
    warehouse_faults_cleared = warehouse_fault.clear_slot(part)

    # 이 칸 때문에 blocked_by 로 기다리던 주문을 다시 배정해 본다 (콜백 복구와 같은 경로).
    from services.dispatch import on_stock_recovered
    assigned = on_stock_recovered()

    return jsonify({
        "ok": True,
        "part": part,
        "count": cur.get("count"),
        "status": "idle",
        "revived": revived,
        "shelf": shelf,
        "shelf_cleared": shelf_cleared,
        "faults_cleared": faults_cleared,
        "warehouse_faults_cleared": warehouse_faults_cleared,
        "assigned": assigned,
    }), 200

@bp.route('/admin/stations', methods=['GET'])
def admin_stations():
    """
    조립대 상태 현황 (관리자 페이지용)
    ---
    tags:
      - Admin
    responses:
      200:
        description: 조립대 3칸의 점유 여부 + 배정된 주문 정보
    """
    occupancy = r.hgetall(STATION_KEY)

    stations = []
    for sid in STATION_IDS:
        status = occupancy.get(sid, "idle")
        order_id = r.get(f"{STATION_ORDER_PREFIX}{sid}")

        order_info = _get_order_summary(order_id) if order_id else None

        stations.append({
            "station_id": sid,
            "occupancy": status,
            "order": order_info,
        })

    return jsonify({"stations": stations}), 200


@bp.route('/admin/orders', methods=['GET'])
def admin_orders():
    """
    모든 주문 조회 (관리자 페이지용) — 오늘 접수된 전체
    ---
    tags:
      - Admin
    responses:
      200:
        description: 진행 중 + 오늘 종료된 주문 전체 목록
    """
    orders = []

    for order_id in _live_order_ids():
        summary = _get_order_summary(order_id)
        if summary:
            orders.append(summary)

    orders.extend(_get_archived_orders_today())

    return jsonify({"orders": orders}), 200


def _paper_remaining():
    used = _to_int(r.hget(PAPER_KEY, "used")) or 0
    return max(PAPER_MAX_COUNT - used, 0)


@bp.route('/admin/paper', methods=['GET'])
def admin_paper():
    """
    영수증 용지 잔량 (관리자 페이지 "영수증 용지" 탭)
    ---
    tags:
      - Admin
    description: |
      남은 용지 = PAPER_MAX_COUNT(900) - kiosk:paper.used.
      used 는 마지막 채우기(POST /admin/paper/refill) 이후 접수된 주문 수다 (주문 1건 = 1매).
      접수에 성공한 주문만 센다 — 대기열 가득 409 · 입력 오류 400 · 접수 마감 503 은 제외, 중단·노쇼는 포함.
      0 밑으로는 내려가지 않는다. /admin/reset 은 모드와 상관없이 이 값을 건드리지 않는다.
    responses:
      200:
        description: 남은 용지 수
        schema:
          type: object
          properties:
            remaining:
              type: integer
          example: {"remaining": 873}
    """
    return jsonify({"remaining": _paper_remaining()}), 200


@bp.route('/admin/paper/refill', methods=['POST'])
def admin_paper_refill():
    """
    영수증 용지 채우기 (관리자 페이지 "용지 채우기" 버튼) — 다시 900매부터 센다
    ---
    tags:
      - Admin
    description: |
      kiosk:paper.used 를 0 으로 되돌린다. 대기번호(order:counter)·주문 통계는 건드리지 않는다.
      body 는 받지 않는다 (항상 가득 = PAPER_MAX_COUNT).
    responses:
      200:
        description: 채움 완료
        schema:
          type: object
          properties:
            ok:
              type: boolean
            remaining:
              type: integer
          example: {"ok": true, "remaining": 900}
    """
    before = _paper_remaining()
    r.hset(PAPER_KEY, "used", 0)
    logger.info(f"[admin] 영수증 용지 채움: {before} -> {PAPER_MAX_COUNT}")
    return jsonify({"ok": True, "remaining": _paper_remaining()}), 200
