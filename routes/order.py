import uuid
import json
from datetime import datetime
import io
from functools import wraps
import qrcode
from flask import Blueprint, jsonify, request, send_file

from config import (QUEUE_KEY,
                    MAX_QUEUE_LEN,
                    ORDER_PAGE_BASE,
                    COLOR_LIST,
                    BOARD_LIST,
                    KST,
                    PAPER_KEY,
                   )

from infra.logger import logger

from infra.extensions import r
from infra.keys import (_order_counter_key,
                        _touch_order,
                        _ensure_initial_state,
                      )

from services.order_service import _is_valid_mbti
from services.dispatch import try_assign_next
from services.line import require_open, is_open as line_is_open
from services.process import active_order_ids

bp = Blueprint('order', __name__)


def require_no_fault(view):
    """진행 중 주문에 fault 가 있으면 해제될 때까지 POST /order 를 받지 않는다.

    Idempotency-Key 를 잡기 전에 걸러야 한다. 라우트 함수 안에서 거르면 키가 processing 으로
    남아, fault 를 해제한 뒤 같은 키로 재시도해도 5분 동안 409 를 받는다.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if any(r.hget(f"order:{oid}", "fault") for oid in active_order_ids()):
            return jsonify({'error': '설비 점검 중입니다. 잠시 후 다시 시도해주세요', 'fault': True}), 409
        return view(*args, **kwargs)
    return wrapper


@bp.route('/queue/status', methods=['GET'])
def queue_status():
    """
    현재 대기열 상태 조회
    ---
    tags:
      - Queue
    responses:
      200:
        description: 대기열 상태
        schema:
          type: object
          properties:
            queue_length:
              type: integer
              example: 2
            max_queue_len:
              type: integer
              example: 3
            full:
              type: boolean
              example: false
            open:
              type: boolean
              description: 주문 접수 중인지. false 면 마감 — POST /order 가 503 을 준다
              example: true
            orders:
              type: array
              description: 대기 중인 주문 목록 (앞이 먼저 배정될 순서)
              items:
                type: object
              example: [{"order_id": "ord_a1b2c3d4", "order_seq": 7, "position": 1,
                         "keycap": "ENTP", "colors": ["b","b","r","g"], "board": "blue",
                         "blocked_by": [["P_b", "count_0"]]}]
    """
    queue = r.lrange(QUEUE_KEY, 0, -1)
    orders = []
    for i, oid in enumerate(queue, 1):
        od = r.hgetall(f"order:{oid}")
        if not od:
            continue                       # TTL 로 사라진 주문. 배정 때 정리된다
        # blocked_by 는 재고로 막혀 대기 중이라는 뜻이다 (services/dispatch.try_assign_next).
        # 관리자 페이지가 "왜 안 나가는지"를 이걸로 표시한다.
        try:
            blocked = json.loads(od["blocked_by"]) if od.get("blocked_by") else None
        except (ValueError, TypeError):
            blocked = None
        orders.append({
            "order_id": oid,
            "order_seq": int(od["order_seq"]) if od.get("order_seq") else None,
            "position": i,
            "keycap": od.get("keycap"),
            "colors": [c for c in (od.get("colors") or "").split(",") if c],
            "board": od.get("board"),
            "blocked_by": blocked,
        })

    return jsonify({
        'queue_length': len(queue),
        'max_queue_len': MAX_QUEUE_LEN,
        'full': len(queue) >= MAX_QUEUE_LEN,
        'open': line_is_open(),
        'orders': orders,
    })

@bp.route('/order', methods=['POST'])
@require_open
@require_no_fault
def post_order_list():
    """
    키캡 주문 생성
    ---
    tags:
      - Order
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            board:
              type: string
              example: "blue"
            keycap:
              type: string
              example: "ESFJ"
            colors:
              type: array
              items:
                type: string
              example: ["r", "y", "b", "g"]
    responses:
      200:
        description: 주문 생성 성공
      400:
        description: 요청 값 오류
      409:
        description: 대기열 초과 또는 진행 중 주문의 fault 로 접수 중단 (재시도 필요). fault 면 응답 body 에 "fault" = true
      503:
        description: 주문 접수 마감 (영업 상태 closed). 응답 body 에 "line" = "closed"
    """
    idem_key = request.headers.get("Idempotency-Key")

    if idem_key:
        claimed = r.set(f"idempotency:{idem_key}", "processing", nx=True, ex=300)
        if not claimed:
            # 이미 처리 중이거나 처리 완료 → 잠깐 대기 후 재조회, 또는 409 반환
            cached = r.get(f"idempotency:{idem_key}")
            if cached and cached != "processing":
                cached_response = json.loads(cached)
                return jsonify(cached_response["body"]), cached_response["status"]
            return jsonify({'error': '동일 요청 처리 중입니다. 잠시 후 재시도해주세요'}), 409
            
    data = request.get_json()

    board = data.get("board")
    keycap = data.get("keycap")
    colors = data.get("colors")

    try:
        if board not in BOARD_LIST:
            return jsonify({'error': '지원하지 않는 본판 색상'}), 400
        if not isinstance(keycap, str) or len(keycap) != 4:
            return jsonify({'error': '키캡은 4글자여야 합니다'}), 400
        if not isinstance(colors, list) or len(colors) != 4:
            return jsonify({'error': '색상 4개를 선택해주세요'}), 400
        if not _is_valid_mbti(keycap):
            return jsonify({'error': '잘못된 알파벳 압력(MBTI 아님)'}), 400
        if not all(c in COLOR_LIST for c in colors):
            return jsonify({'error': '잘못된 색상 선택'}), 400

        _ensure_initial_state()

        # 대기열 캡 체크: 이미 3개 대기 중이면 더 받지 않음
        if r.llen(QUEUE_KEY) >= MAX_QUEUE_LEN:
            return jsonify({'error': '대기열이 가득 찼습니다. 잠시 후 다시 시도해주세요'}), 409

        # 재고 판정은 여기서 하지 않는다. 커스텀을 다 끝낸 사용자를 접수 단계에서
        # 되돌리지 않기 위해서다 — 키오스크가 /stock/out 으로 품절 조합을 이미 막는다.
        # 못 내보내는 주문은 배정 단계(stock.blockers)에서 '대기'로 흡수한다.

        order_id = f"ord_{uuid.uuid4().hex[:8]}"
        order_seq = r.incr(_order_counter_key())
        r.hincrby(PAPER_KEY, "used", 1)          # 영수증 1매 — 잔량은 GET /admin/paper

        # 일단 무조건 큐에 넣고 상태 queued 생성
        r.rpush(QUEUE_KEY, order_id)
        r.hset(f"order:{order_id}", mapping={
            "board": board,
            "keycap": keycap,
            "colors": ",".join(colors),
            "stage": "queued",
            "order_seq": order_seq,
            "created_at": datetime.now(KST).isoformat()
        })
        _touch_order(order_id)          # 주문 스코프 키 6개를 한 묶음으로 (24시간)

        # warehouse/station 여유가 있으면 방금 넣은 주문이 바로 배정될 수 있음
        assigned_order_id = try_assign_next()
        logger.debug(f"배정된 order_id: {assigned_order_id}")

        order_data = r.hgetall(f"order:{order_id}")
        stage = order_data.get("stage")

        if stage == "assigned":
            position_in_queue = None
            station_id = order_data.get("station_id")
        else:
            queue = r.lrange(QUEUE_KEY, 0, -1)
            position_in_queue = queue.index(order_id) + 1 if order_id in queue else None
            station_id = None

        order_status = {
            "order_id": order_id,
            "stage": stage,
            "position_in_queue": position_in_queue,
            "order_seq": order_seq,
            "station_id": str(station_id) if station_id else None
        }

        response_body = {'order_status': order_status}

        if idem_key:
            r.set(f"idempotency:{idem_key}", json.dumps({"status": 200, "body": response_body}), ex=300)

        return jsonify(response_body), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/order/<order_id>/status', methods=['GET'])
def get_order_status(order_id):
    """
    주문 상태 조회
    ---
    tags:
      - Order
    parameters:
      - in: path
        name: order_id
        type: string
        required: true
        example: ord_a1b2c3d4
    responses:
      200:
        description: 조회 성공
        schema:
          type: object
          properties:
            order_id:
              type: string
              example: ord_a1b2c3d4
            stage:
              type: string
              enum: [queued, assigned, dispensed, loaded, arrived, verified, expired, cancelled, completed]
              example: assigned
            station_id:
              type: string
              example: 1
            position_in_queue:
              type: integer
              example: 2
      404:
        description: 존재하지 않는 주문
        schema:
          type: object
          properties:
            error:
              type: string
              example: 존재하지 않는 주문입니다
    """
    order_data = r.hgetall(f"order:{order_id}")
    if not order_data:
        return jsonify({'error': '존재하지 않는 주문입니다'}), 404

    stage = order_data.get("stage")
    station_id = order_data.get("station_id")
    position_in_queue = None

    if stage == "queued":
        queue = r.lrange(QUEUE_KEY, 0, -1)
        if order_id in queue:
            position_in_queue = queue.index(order_id) + 1

    return jsonify({
        "order_id": order_id,
        "stage": stage,
        "station_id": station_id if station_id else None,
        'board': order_data.get("board"),
        'keycap': order_data.get("keycap"),
        'colors': order_data.get("colors", "").split(","),
        'order_seq' : order_data.get("order_seq"),
        "position_in_queue": position_in_queue
    }), 200

@bp.route('/order/<order_id>/qr', methods=['GET'])
def get_order_qr(order_id):
    """
    주문 상태 페이지 QR 코드 생성
    ---
    tags:
      - Order
    parameters:
      - in: path
        name: order_id
        type: string
        required: true
    responses:
      200:
        description: QR 코드 PNG 이미지
      404:
        description: 존재하지 않는 주문
    """
    if not r.exists(f"order:{order_id}"):
        return jsonify({'error': '존재하지 않는 주문입니다'}), 404

    url = f"{ORDER_PAGE_BASE}/station/{order_id}/start"

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")