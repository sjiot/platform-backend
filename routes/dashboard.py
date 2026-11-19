import json
import re
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

from infra.logger import logger
from infra.extensions import r
from config import KST
from services.dashboard import (today_key,
                                get_exhibition_dates,
                                get_completed_archive_orders,
                                get_order_times,
                               )
from services import agv_trail

bp = Blueprint("dashboard",__name__, url_prefix="/dashboard")

@bp.route("/stocks", methods=["GET"])
def get_dashboard_stocks():
    """
    대시보드 재고 현황 조회
    ---
    tags:
      - Dashboard
    summary: 대시보드 재고 현황 조회
    responses:
      200:
        description: 재고 현황 조회 성공
      400:
        description: 재고 현황 조회 실패
    """

    try:

        raw_stock = r.hgetall("warehouse:stock")
        stocks = {}

        for cartridge_id, value in raw_stock.items():
            stock_data = json.loads(value)

            stocks[cartridge_id] = {
                "count": stock_data["count"],
                "status": stock_data["status"]
            }

        return jsonify({
            "success": True,
            "stocks": stocks
        }), 200

    except Exception as e:
        logger.error(f"[dashboard/stocks ERROR], {e}")

    return jsonify({
        "success": False,
        "error": "재고 정보를 조회하지 못했습니다."
    }), 400

@bp.route("/orders/stats", methods=["GET"])
def get_order_summary():
    """
    대시보드 주문 현황 조회
    ---
    tags:
      - Dashboard
    summary: 대시보드 주문 건수 조회
    description: |
      대시보드 하단의 주문 건수 정보를 반환합니다.
      반환 정보:
      - total_orders
        - 전시 기간 전체 주문 건수
      - today_orders
        - 오늘 생성된 주문 건수
      - waiting_orders
        - 현재 대기열에 있는 주문 건수
      - completed_today
        - 오늘 정상 완료된 주문 건수
      - yesterday_same_time_orders
        - 어제 0시부터 어제 같은 시각(분 단위)까지 접수된 주문 건수 (증감 비교용)
        - 계산에 실패하면 null
    responses:
      200:
        description: 주문 현황 조회 성공
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            total_orders:
              type: integer
              description: 전시 기간 전체 주문 건수
              example: 59
            today_orders:
              type: integer
              description: 오늘 생성된 주문 건수
              example: 10
            waiting_orders:
              type: integer
              description: 현재 대기 주문 건수
              example: 4
            completed_today:
              type: integer
              description: 오늘 정상 완료된 주문 건수
              example: 7
            yesterday_same_time_orders:
              type: integer
              x-nullable: true
              description: 어제 같은 시각까지 접수된 주문 건수 (계산 실패 시 null)
              example: 17
      400:
        description: 주문 현황 조회 실패
    """
    try:
        today = today_key()

        #  전시회 전체 주문 건수
        # order:counter:{YYYYMMDD}
        total_orders = 0

        for date in get_exhibition_dates():
            count = r.get(f"order:counter:{date}")

            total_orders += int(count or 0)

        # 오늘 주문 건수
        today_orders = int(r.get(f"order:counter:{today}") or 0)

        # 현재 대기 주문 건수
        waiting_orders = r.llen("order:queue")

        # 오늘 정상 완료 주문 건수
        # orders:archive:{today}에서 reason이 없는 주문만 계산
        completed_today = 0
        raw_orders = r.hgetall(f"orders:archive:{today}")

        for order_id, raw_order in raw_orders.items():
            try:
                order = json.loads(raw_order)

            except (json.JSONDecodeError, TypeError):
                logger.error(f"[dashboard/orders/stats] 잘못된 archive 데이터: {order_id}")

                continue

            # reason이 없으면 정상 완료
            if not order.get("reason"):
                completed_today += 1

        # 어제 같은 시각까지 접수된 주문 건수 — 대시보드 '주문 건수 증감'
        # get_order_times 와 같은 기준 (아카이브 + 진행 중, KST, 정상/비정상 종료 구분 없이 접수 기준)
        # 실패해도 다른 값은 그대로 응답한다 → 프론트는 null 이면 '—'
        yesterday_same_time_orders = None

        try:
            yday = datetime.now(KST) - timedelta(days=1)
            _, y_times = get_order_times(yday.strftime("%Y%m%d"))
            cut = yday.strftime("%H:%M")
            yesterday_same_time_orders = sum(1 for t in y_times if t <= cut)

        except Exception as e:
            logger.error(f"[dashboard/orders/stats] 어제 같은 시각 주문 수 계산 실패: {e}")

        return jsonify({
            "success": True,
            "total_orders": total_orders,
            "today_orders": today_orders,
            "waiting_orders": waiting_orders,
            "completed_today": completed_today,
            "yesterday_same_time_orders": yesterday_same_time_orders
        }), 200

    except Exception as e:
        logger.error(f"[dashboard/orders/stats ERROR] error: {e}")

        return jsonify({
            "success": False,
            "error": "주문 현황을 조회하지 못했습니다."
        }), 400

@bp.route("/orders/timeline", methods=["GET"])
def get_order_timeline():
    """
    대시보드 시간별 주문 추이 조회
    ---
    tags:
      - Dashboard
    summary: 그 날짜에 접수된 주문의 접수 시각 리스트
    description: |
      date 날짜(KST)에 들어온 모든 주문의 접수 시각(HH:MM)을 오름차순으로 반환합니다.
      date 를 생략하면 오늘입니다.
      개수 집계(10분 · 1시간 단위, 어제 같은 시각 대비 증감 등)는 프론트에서 합니다.

      조회 대상
      - order:{order_id} — 진행 중 주문 (대기열 + 조립대 + 창고)
      - orders:archive:{date} — 그 날짜에 종료된 주문

      규칙
      - 같은 분에 여러 건이 들어오면 그 수만큼 같은 시각이 들어갑니다
      - 정상 완료 / unclaimed / timeout / admin_abort 구분 없이 모두 포함 (접수 시각 기준)
      - created_at(2026-10-03T19:55:38.131711+09:00)을 KST 기준 HH:MM(19:55)으로 자릅니다
      - 차트는 주문이 없는 구간을 프론트에서 0으로 채워야 합니다

      어제 같은 시각 대비 증감 (프론트)
      - 오늘 = 오늘 times 길이
      - 어제 같은 시각 = 어제 times 중 지금 KST HH:MM 이하인 개수
      - 어제 목록은 하루 동안 바뀌지 않으므로 날짜가 바뀔 때만 다시 받으면 됩니다
    parameters:
      - name: date
        in: query
        type: string
        required: false
        description: 조회 날짜 (KST, YYYYMMDD). 생략하면 오늘
        example: "20261002"
    responses:
      200:
        description: 시간별 주문 추이 조회 성공
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            date:
              type: string
              description: 조회 날짜 (KST, YYYYMMDD)
              example: "20261003"
            count:
              type: integer
              description: 그 날짜에 접수된 주문 건수 (times 길이)
              example: 4
            times:
              type: array
              description: 주문 접수 시각 (HH:MM, 오름차순, 같은 시각 중복 포함)
              items:
                type: string
              example: ["10:02", "13:40", "19:55", "19:55"]
      400:
        description: date 형식 오류 또는 조회 실패
    """
    date = request.args.get("date") or None

    if date is not None and not _valid_date(date):
        return jsonify({
            "success": False,
            "error": "date 는 YYYYMMDD 형식이어야 합니다.",
            "got": date
        }), 400

    try:
        date, times = get_order_times(date)

        return jsonify({
            "success": True,
            "date": date,
            "count": len(times),
            "times": times
        }), 200

    except Exception as e:
        logger.error(f"[dashboard/orders/timeline ERROR] {e}")

        return jsonify({
            "success": False,
            "error": "시간별 주문 추이를 조회하지 못했습니다."
        }), 400


def _valid_date(value):
    """YYYYMMDD 8자리이고 실제 있는 날짜인가. strptime 만 쓰면 '2026101' 같은 7자리도 통과한다."""
    if not re.fullmatch(r"\d{8}", value):
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True

@bp.route("/statistics", methods=["GET"])
def get_dashboard_statistics():
    """
    대시보드 주문 통계 조회
    ---
    tags:
      - Dashboard
    summary: 완료 주문 기반 대시보드 통계 조회
    description: |
      전시 기간의 orders:archive:{YYYYMMDD} 데이터를
      직접 조회하여 정상 완료된 주문의 통계를 계산합니다.

      reason이 존재하는 unclaimed / timeout 주문은
      통계에서 제외합니다.

      반환 정보:
      - completed_orders
        - 전시 기간 정상 완료 주문 건수
      - mbti_stats
        - MBTI별 완료 주문 수
        - 비율
        - 순위
      - keycap_color_stats
        - 키캡 색상별 사용 개수
        - 색상별 사용 비율
      - top_keycap_combo
        - 가장 많이 선택된 키캡 색상 조합
        - 해당 조합의 주문 수
    responses:
      200:
        description: 대시보드 통계 조회 성공
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true

            completed_orders:
              type: integer
              description: 전시 기간 정상 완료 주문 건수
              example: 10
            mbti_stats:
              type: array
              description: MBTI별 완료 주문 통계
              items:
                type: object
                properties:
                  rank:
                    type: integer
                    example: 1
                  mbti:
                    type: string
                    example: ENTP
                  count:
                    type: integer
                    example: 5
                  ratio:
                    type: number
                    format: float
                    example: 50.0
            keycap_color_stats:
              type: object
              description: 키캡 색상별 사용 통계
            top_keycap_combo:
              type: object
              nullable: true
              description: 가장 많이 선택된 키캡 색상 조합
      400:
        description: 대시보드 통계 조회 실패
    """
    try:
        # 전시 기간 정상 완료 주문 가져오기
        orders = get_completed_archive_orders()
        completed_orders = len(orders)

        # MBTI 통계
        mbti_counts = {}

        for order in orders:
            mbti = order.get("keycap")

            if not mbti:
                continue

            mbti_counts[mbti] = (mbti_counts.get(mbti,0) + 1)

        # 주문 수 기준 내림차순
        sorted_mbti = sorted(
            mbti_counts.items(),
            key=lambda x: x[1],
            reverse=True
        )

        mbti_stats = []

        for rank, (mbti, count) in enumerate(sorted_mbti,start=1):
            ratio = (
                round(count / completed_orders * 100, 2)
                if completed_orders > 0 else 0
            )

            mbti_stats.append({
                "rank": rank,
                "mbti": mbti,
                "count": count,
                "ratio": ratio
            })

        # 키캡 색상별 통계
        color_counts = {}

        for order in orders:
            colors = order.get("colors", [])

            if not isinstance(colors, list):
                continue

            for color in colors:
                color_counts[color] = (color_counts.get(color, 0) + 1)

        # 전체 사용 키캡 개수
        total_keycaps = sum(color_counts.values())
        keycap_color_stats = {}

        for color, count in color_counts.items():
            ratio = (
                round(count / total_keycaps * 100, 2)
                if total_keycaps > 0 else 0
            )

            keycap_color_stats[color] = {
                "count": count,
                "ratio": ratio
            }

        # 키캡 색상 조합 통계
        combo_counts = {}

        for order in orders:
            colors = order.get("colors", [])

            if (not isinstance(colors, list) or not colors):
                continue

            combo = "-".join(colors)

            combo_counts[combo] = (combo_counts.get(combo, 0) + 1)

        # 가장 많이 선택된 조합
        top_keycap_combo = None

        if combo_counts:
            combo, count = max(
                combo_counts.items(),
                key=lambda x: x[1]
            )

            top_keycap_combo = {
                "colors": combo.split("-"),
                "count": count
            }

        return jsonify({
            "success": True,
            "completed_orders": completed_orders,
            "mbti_stats": mbti_stats,
            "keycap_color_stats": keycap_color_stats,
            "top_keycap_combo": top_keycap_combo
        }), 200

    except Exception as e:
        logger.error(f"[dashboard/statistics ERROR] {e}")

        return jsonify({
            "success": False,
            "error": "통계 정보를 조회하지 못했습니다."
        }), 400

@bp.route("/station", methods=["GET"])
def get_dashboard_station():
    """
    조립대 현황 조회
    ---
    tags:
      - Dashboard
    summary: 조립대별 현재 주문 및 진행 상태 조회
    description: |
      조립대 1, 2, 3의 현재 주문 정보를 조회합니다.
      각 조립대의 `station:current_order:{station_id}`에서
      현재 배정된 `order_id`를 조회한 뒤,
      `order:{order_id}`에서 `order_seq`와 `stage`를 조회합니다.
      현재 배정된 주문이 없는 조립대는
      `order_seq`와 `stage`가 null로 반환됩니다.
    responses:
      200:
        description: 조립대 현황 조회 성공
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            stations:
              type: object
              properties:
                "1":
                  type: object
                  properties:
                    order_seq:
                      type: integer
                      nullable: true
                      example: 4
                    stage:
                      type: string
                      nullable: true
                      example: queued
                "2":
                  type: object
                  properties:
                    order_seq:
                      type: integer
                      nullable: true
                      example: 5
                    stage:
                      type: string
                      nullable: true
                      example: assembling
                "3":
                  type: object
                  properties:
                    order_seq:
                      type: integer
                      nullable: true
                      example: null
                    stage:
                      type: string
                      nullable: true
                      example: null
        examples:
          application/json:
            success: true
            stations:
              "1":
                order_seq: 4
                stage: queued
              "2":
                order_seq: 5
                stage: assembling
              "3":
                order_seq: null
                stage: null
      400:
        description: 조립대 현황 조회 실패
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: false
            error:
              type: string
              example: 조립대 정보를 조회하지 못했습니다.
    """
    try:
        stations = {}

        for station_id in ["1", "2", "3"]:
            order_id = r.get(f"station:current_order:{station_id}")

            # 현재 주문이 없는 경우
            if not order_id:
                stations[station_id] = {
                    "order_seq": None,
                    "stage": None
                }
                continue

            # 2. order:{order_id}에서 order_seq, stage 조회
            order_data = r.hmget(
                f"order:{order_id}",
                "order_seq",
                "stage"
            )

            order_seq, stage = order_data

            stations[station_id] = {
                "order_seq": int(order_seq) if order_seq else None,
                "stage": stage
            }

        return jsonify({
            "success": True,
            "stations": stations
        }), 200

    except Exception as e:
        logger.error(f"[dashboard/station ERROR] {e}")

        return jsonify({
            "success": False,
            "error": "조립대 정보를 조회하지 못했습니다."
        }), 400

@bp.route("/mark/movement", methods=["GET"])
def get_dashboard_agv_coord():
    """
    AGV 이동경로 조회
    ---
    tags:
      - Dashboard
    summary: AGV 현재 위치와 새로 생긴 좌표 조회
    description: |
      콜백(cnt_agv)이 쌓은 좌표 중 since 이후의 점만 반환합니다.
      프론트는 받은 점을 ts 기준으로 폴링 주기만큼 늦춰 재생하면 끊기지 않습니다.

      구간은 장소 도착(agv:status 기준)에서 끊깁니다.
      - arrived → checkpoint "station:{id}"
      - parked → "parking"
      - discarded → "discard"

      프론트 규칙
      - resync=true → 그린 경로를 지우고 points 로 다시 그림
      - leg 가 바뀐 점이 오면 → 경로를 지우고 직전 도착점에서 새로 그림
      - checkpoint 가 붙은 점까지 재생하면 거기서 멈춤
      - current 가 null → 위치 모름(AGV 초기화 직후 등) → 마커 숨김
    parameters:
      - name: since
        in: query
        type: integer
        required: false
        default: 0
        description: 마지막으로 받은 점의 seq. 첫 로드는 0
    responses:
      200:
        description: 조회 성공
        examples:
          application/json:
            success: true
            resync: false
            current: {seq: 13, leg: 3, coord: [1, 4], ts: 1759280001500, checkpoint: null}
            points:
              - {seq: 12, leg: 3, coord: [1, 3], ts: 1759280000000, checkpoint: null}
              - {seq: 13, leg: 3, coord: [1, 4], ts: 1759280001500, checkpoint: null}
      400:
        description: 조회 실패
    """
    try:
        since = request.args.get("since", default=0, type=int) or 0
        points = agv_trail.trail()
        current = agv_trail.current()
        latest = points[-1]["seq"] if points else 0

        # 첫 로드 · Redis 초기화(since 가 최신보다 큼) · 오래 비운 탭(목록 밖으로 밀려남)
        resync = since <= 0 or since > latest or points[0]["seq"] > since + 1

        if resync:
            if not current:
                new_points = []                  # 위치 모름 -> 마커 숨김
            elif current.get("checkpoint"):
                new_points = [current]           # 장소에 서 있음 -> 점 하나
            else:
                new_points = points              # agv:trail 엔 현재 구간(+출발점)만 있다
        else:
            new_points = [p for p in points if p["seq"] > since]

        return jsonify({
            "success": True,
            "current": current,
            "points": new_points,
            "resync": resync,
        }), 200

    except Exception as e:
        logger.error(f"[dashboard/mark/movement ERROR] {e}")

        return jsonify({
            "success": False,
            "error": "AGV 위치를 조회하지 못했습니다."
        }), 400
