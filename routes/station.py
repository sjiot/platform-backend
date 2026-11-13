import math

from flask import Blueprint, jsonify, request

from config import (STATION_KEY,
                    STATION_ORDER_PREFIX,
                    STATION_UNCLAIM_MIN_SEC,
                   )

from infra.logger import logger

from infra.extensions import r
from services.process import TERMINAL, is_before, is_after
from services.archive import finish_order
from services.watchdog import DEADLINE_KEY
from infra.keys import (_order_counter_key,
                        _ensure_initial_state,
                      )

from services.order_service import _is_valid_mbti
from services.station_service import (elapsed_since_arrival,
                                      start_order,
                                      unclaim_guard)

bp = Blueprint('station', __name__)


# arrived_at 계산·노쇼 판정은 services/station_service.py 에 있다 (라우트는 응답만 만든다)
_elapsed_since_arrival = elapsed_since_arrival

@bp.route('/station/<order_id>/start', methods=['POST'])
def station_start(order_id):
    """
    조립대 시작 (QR 인증)
    ---
    tags:
      - Station
    parameters:
      - in: path
        name: order_id
        type: string
        required: true
        example: "ord_a1b2c3d4"
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            station_id:
              type: string
              description: QR을 스캔한 현재 조립대 번호 (기기 자체 고정값)
              example: 1
    responses:
      200:
        description: 시작 처리 성공
      400:
        description: 아직 조립대에 배정되지 않은 주문
      403:
        description: station_id 불일치 (다른 조립대에서 스캔함)
      404:
        description: 존재하지 않거나 만료된 주문
      409:
        description: 이미 진행 중
    """
    data = request.get_json(silent=True) or {}
    payload, status = start_order(order_id, data.get("station_id", ""))
    return jsonify(payload), status

@bp.route('/station/<station_id>/complete', methods=['POST'])
def station_complete(station_id):
    """
    조립대 완료
    ---
    tags:
      - Station
    parameters:
      - in: path
        name: station_id
        type: string
        required: true
        example: 1
      - in: body
        name: body
        required: false
        schema:
          type: object
          properties:
            order_id:
              type: string
              description: 디스플레이가 들고 있던 주문 ID (서버 기록과 교차검증용)
              example: "ord_a1b2c3d4"
    responses:
      200:
        description: 완료 처리 성공
        schema:
          type: object
          properties:
            ok:
              type: boolean
              example: true
            order_id:
              type: string
              example: "ord_a1b2c3d4"
      400:
        description: 이 조립대에 진행 중인 주문이 없음
      409:
        description: 화면에 표시된 order_id가 서버 기록과 불일치 (stale 상태) · 이미 종료된 주문 (unclaimed·completed)
        schema:
          type: object
          properties:
            error:
              type: string
            current_order_id:
              type: string
              example: "ord_a1b2c3d4"
    """
    station_id = str(station_id)
    data = request.get_json(silent=True) or {}
    req_order_id = data.get("order_id")

    order_key = f"{STATION_ORDER_PREFIX}{station_id}"
    order_id = r.get(order_key)

    if not order_id:
        return jsonify({'error': '이 조립대에 진행 중인 주문이 없습니다'}), 400

    if req_order_id and req_order_id != order_id:
        return jsonify({
            'error': '화면에 표시된 주문 정보가 서버 기록과 다릅니다. 새로고침 후 다시 시도해주세요',
            'current_order_id': order_id
        }), 409

    stage = r.hget(f"order:{order_id}", "stage")
    # unclaimed 는 STAGE_ORDER 에서 received 보다 앞이라 is_after 만으로는 통과한다. 노쇼 주문은
    # AGV 가 폐기를 마칠 때까지 조립대를 잡고 있으므로(services/archive.py) 여기서 완료로 풀면 안 된다.
    if stage in TERMINAL or is_after(stage, "received"):
        return jsonify({'error': '이미 종료된 주문입니다', 'stage': stage}), 409

    # 아카이브 + 집계 → stage 전이 → 조립대 해제 → 다음 배정 (services/archive.py)
    finish_order(order_id, "completed")
    # 배정이 됐으면 _assign이 cnt_station을 올린다. 안 됐으면 올릴 변화가 없다.

    return jsonify({'ok': True, 'order_id': order_id}), 200

@bp.route('/station/<station_id>/unclaim', methods=['POST'])
def station_unclaim(station_id):
    """
    조립대 만료됨(노쇼)
    ---
    tags:
      - Station
    parameters:
      - in: path
        name: station_id
        type: string
        required: true
        example: 1
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            order_id:
              type: string
              description: 디스플레이가 들고 있던 주문 ID (서버 기록과 교차검증용)
              example: "ord_a1b2c3d4"           
    responses:
      200:
        description: 시작 처리 성공
      400:
        description: 아직 조립대에 배정되지 않은 주문
      403:
        description: station_id 불일치 (다른 조립대에서 스캔함)
      404:
        description: 존재하지 않거나 만료된 주문
      409:
        description: 이미 진행 중
    """
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")

    order_data = r.hgetall(f"order:{order_id}")
    if not order_data:
        return jsonify({'error': '존재하지 않거나 만료된 주문입니다'}), 404

    assigned_station_id = order_data.get("station_id")
    if not assigned_station_id:
        return jsonify({'error': '아직 조립대에 배정되지 않은 주문입니다'}), 400
    assigned_station_id = str(assigned_station_id)
    if station_id != assigned_station_id:
        return jsonify({'error': '이 조립대에 배정된 주문이 아닙니다', 'assigned_station_id': (assigned_station_id)}), 403
    if r.get(f"{STATION_ORDER_PREFIX}{station_id}") != order_id:
        return jsonify({'error': '이미 종료되었거나 이 조립대의 현재 주문이 아닙니다'}), 409

    # /start와 같은 이유로 arrived에서만 받는다.
    # 특히 verified 이후를 막아야 한다 — 사용자가 인증한 직후 프론트 타이머가 뒤늦게
    # 터지면 verified를 unclaimed로 덮어쓰고, AGV는 이미 받은 하차 명령대로 움직인다.
    stage = order_data.get("stage")
    if is_before(stage, "arrived"):
        return jsonify({'error': '아직 부품이 도착하지 않았습니다', 'stage': stage}), 409
    if is_after(stage, "arrived"):
        return jsonify({'error': '이미 처리된 주문입니다', 'stage': stage}), 409

    # 노쇼로 처리해도 되는 시점인지는 서버가 arrived_at 으로 다시 잰다 (station_service).
    ok, elapsed, remain = unclaim_guard(order_data)
    if not ok:
        return jsonify({
            'error': '아직 대기 시간이 남았습니다',
            'elapsed_sec': elapsed,
            'remain_sec': remain,
        }), 425
    if elapsed is None:
        logger.warning(f"[station] {order_id} arrived_at 없음 — 시간 가드 건너뜀")

    # 노쇼는 fault가 아니라 정상 종료 경로. 아카이브까지 같이 남긴다.
    # agv:occupancy는 busy로 둔다. AGV가 아직 트레이를 들고 폐기장소로 가는 중이고,
    # discarded → parked를 올리면 그때 idle이 된다.
    finish_order(order_id, "unclaimed", reason="unclaimed")

    return jsonify({
        'ok': True,
        'order_id': order_id,
        'stage': 'unclaimed',
    }), 200

@bp.route('/station/free', methods=['GET'])
def free_stations():
    """
    빈 조립대 목록 조회 (AGV용)
    ---
    tags:
      - Station
    responses:
      200:
        description: 빈 조립대 번호 목록
        schema:
          type: object
          properties:
            free_stations:
              type: array
              items:
                type: string
              example: ["2", "3"]
    """
    station_status = r.hgetall(STATION_KEY)
    free = [sid for sid, status in station_status.items() if status == "idle"]
    return jsonify({'free_stations': free}), 200

# /station/<id>/reset 은 POST /admin/reset 으로 옮겼다 (routes/admin.py).
# 초기화는 조립대 하나가 아니라 라인 전체의 일이고, 재고 초기화와 Mobius 복구가
# 같이 붙었기 때문이다. 프론트는 이 경로를 쓰지 않는다.

@bp.route('/station/<station_id>/status', methods=['GET'])
def get_station_status(station_id):
    """
    조립대 배정 주문 조회 (AGV 도착 알림 후 디스플레이가 주문번호 띄울 때 사용)
    ---
    tags:
      - Station
    parameters:
      - in: path
        name: station_id
        type: string
        required: true
        example: 1
    responses:
      200:
        description: 조회 성공
        schema:
          type: object
          properties:
            order_id:
              type: string
              example: ord_a1b2c3d4
            order_seq:
              type: integer
              example: 12
      400:
        description: 이 조립대에 배정된 주문이 없음 · 아직 도착 전 · 이미 종료된 주문(노쇼 폐기 대기 포함). ready=false
        schema:
          type: object
          properties:
            error:
              type: string
              example: 이 조립대에 배정된 주문이 없습니다
    """
    station_id = str(station_id)
    order_id = r.get(f"{STATION_ORDER_PREFIX}{station_id}")
    if not order_id:
        return jsonify({'ready': False, 'error': '이 조립대에 배정된 주문이 없습니다'}), 400

    order_data = r.hgetall(f"order:{order_id}")
    if not order_data:
        # station_order 키는 남아있는데 order 해시가 TTL로 만료된 엣지 케이스
        return jsonify({'ready': False, 'error': '이 조립대에 배정된 주문이 없습니다'}), 400

    # 배정만 됐을 뿐 부품은 아직 창고·벨트·AGV 위에 있다. 이 구간에 주문번호를 내주면
    # 디스플레이가 AGV보다 먼저 주문을 띄우고 사용자가 빈 조립대 앞에서 QR을 찍는다.
    # 400을 쓰는 건 프론트가 이 엔드포인트의 400을 이미 "아직 없음"으로 처리하기 때문.
    stage = order_data.get("stage")
    if is_before(stage, "arrived"):
        return jsonify({'ready': False, 'error': '아직 부품이 도착하지 않았습니다'}), 400
    if stage in TERMINAL:
        # 노쇼(unclaimed)는 AGV 가 폐기를 마칠 때까지 조립대를 잡고 있다 (services/archive.py).
        # 그동안 ready 로 내주면 디스플레이가 떠난 손님의 주문을 계속 띄우고, 완료 버튼이 눌리면
        # 폐기 전에 조립대가 풀린다. 끝난 주문은 '배정 없음' 과 같은 400 으로 내린다.
        return jsonify({'ready': False, 'error': '이미 종료된 주문입니다', 'stage': stage}), 400

    elapsed = _elapsed_since_arrival(order_data)
    body = {
        "ready": True,
        "order_id": order_id,
        "order_seq": order_data.get("order_seq"),
        "stage": stage,
        "board": order_data.get("board"),
        "keycap": order_data.get("keycap"),
        "colors": [c for c in (order_data.get("colors") or "").split(",") if c],
        "arrived_at": order_data.get("arrived_at"),
        "fault": order_data.get("fault") or None,
        "fault_section": order_data.get("fault_section") or None,
    }
    if elapsed is not None:
        # 프론트가 자체 타이머 대신 이 값을 써도 되도록 서버 기준 잔여 시간을 같이 준다.
        body["elapsed_sec"] = int(elapsed)
        body["unclaim_remain_sec"] = max(0, math.ceil(STATION_UNCLAIM_MIN_SEC - elapsed))
    return jsonify(body), 200

@bp.route('/station/<station_id>/status/debug', methods=['GET'])
def get_station_statu_debug(station_id):
    """
    조립대 배정 주문 조회 (AGV 도착 알림 후 디스플레이가 주문번호 띄울 때 사용)
    ---
    tags:
      - Station
    parameters:
      - in: path
        name: station_id
        type: string
        required: true
        example: 1
    responses:
      200:
        description: 조회 성공
        schema:
          type: object
          properties:
            order_id:
              type: string
              example: ord_a1b2c3d4
            order_seq:
              type: integer
              example: 12
      400:
        description: 이 조립대에 배정된 주문이 없음
        schema:
          type: object
          properties:
            error:
              type: string
              example: 이 조립대에 배정된 주문이 없습니다
    """
    station_id = str(station_id)
    order_id = r.get(f"{STATION_ORDER_PREFIX}{station_id}")
    if not order_id:
        return jsonify({'ready': False, 'error': '이 조립대에 배정된 주문이 없습니다'}), 400

    order_data = r.hgetall(f"order:{order_id}")
    if not order_data:
        # station_order 키는 남아있는데 order 해시가 TTL로 만료된 엣지 케이스
        return jsonify({'ready': False, 'error': '이 조립대에 배정된 주문이 없습니다'}), 400

    # 배정만 됐을 뿐 부품은 아직 창고·벨트·AGV 위에 있다. 이 구간에 주문번호를 내주면
    # 디스플레이가 AGV보다 먼저 주문을 띄우고 사용자가 빈 조립대 앞에서 QR을 찍는다.
    # 400을 쓰는 건 프론트가 이 엔드포인트의 400을 이미 "아직 없음"으로 처리하기 때문.
    stage = order_data.get("stage")

    elapsed = _elapsed_since_arrival(order_data)
    body = {
        "ready": True,
        "order_id": order_id,
        "order_seq": order_data.get("order_seq"),
        "stage": stage,
        "board": order_data.get("board"),
        "keycap": order_data.get("keycap"),
        "colors": [c for c in (order_data.get("colors") or "").split(",") if c],
        "arrived_at": order_data.get("arrived_at"),
        "fault": order_data.get("fault") or None,
        "fault_section": order_data.get("fault_section") or None,
    }
    if elapsed is not None:
        # 프론트가 자체 타이머 대신 이 값을 써도 되도록 서버 기준 잔여 시간을 같이 준다.
        body["elapsed_sec"] = int(elapsed)
        body["unclaim_remain_sec"] = max(0, math.ceil(STATION_UNCLAIM_MIN_SEC - elapsed))
    return jsonify(body), 200