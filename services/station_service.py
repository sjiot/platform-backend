"""조립대 종료 경로. 배정 자체는 services/dispatch.py 가 맡는다."""
import math
from datetime import datetime

from config import (ORDER_TTL,
                    STATION_KEY,
                    STATION_ORDER_PREFIX,
                    STATION_VERIFIED_PREFIX,
                    STATION_CALL_PREFIX,
                    KST,
                    STATION_TIMEOUT_SEC,
                    STATION_UNCLAIM_MIN_SEC,
                   )
from infra.extensions import r
from infra.keys import _touch_order
from infra.logger import logger

from services import watchdog
from services.archive import finish_order
from services.process import set_stage, is_before, is_after

STATIONS = ("1", "2", "3")


def start_order(order_id, station_id):
    """QR 인증 / 관리자 대신 인증 공통 경로. (응답 본문, HTTP 상태) 를 돌려준다."""
    req_station_id = str(station_id)

    order_data = r.hgetall(f"order:{order_id}")
    if not order_data:
        return {'error': '존재하지 않거나 만료된 주문입니다'}, 404

    assigned_station_id = order_data.get("station_id")
    if not assigned_station_id:
        return {'error': '아직 조립대에 배정되지 않은 주문입니다'}, 400
    assigned_station_id = str(assigned_station_id)
    if req_station_id != assigned_station_id:
        return {'error': '이 조립대에 배정된 주문이 아닙니다', 'assigned_station_id': assigned_station_id}, 403
    if r.get(f"{STATION_ORDER_PREFIX}{req_station_id}") != order_id:
        return {'error': '이미 종료되었거나 이 조립대의 현재 주문이 아닙니다'}, 409

    # 인증 가능한 지점은 arrived 하나뿐이다. STAGE_ORDER 순번으로 양쪽을 다 막는다.
    #   앞  — AGV가 아직 조립대에 없다. 통과시키면 이동 중인 AGV에 하차 명령이 나간다.
    #   뒤  — unclaimed·verified·received·completed. 특히 received에서 다시 찍으면
    #         stage가 verified로 역행하고 AGV가 하차 명령을 두 번 받는다.
    stage = order_data.get("stage")
    if is_before(stage, "arrived"):
        return {'error': '아직 부품이 도착하지 않았습니다', 'stage': stage}, 409
    if is_after(stage, "arrived"):
        return {'error': '이미 처리된 주문입니다', 'stage': stage}, 409

    r.hset(STATION_KEY, assigned_station_id, "busy")   # 배정 때 이미 busy. 방어적 재확인
    r.set(f"{STATION_VERIFIED_PREFIX}{assigned_station_id}", datetime.now(KST).isoformat())
    set_stage(order_id, "verified")
    _touch_order(order_id)
    r.expire(f"{STATION_VERIFIED_PREFIX}{assigned_station_id}", ORDER_TTL)
    # cnt_station은 배정 시점에만 올린다 — 여기서 push하면 같은 배정에 CIN이 하나 더 쌓인다

    return {
        'ok': True,
        'order_id': order_id,
        'station_id': assigned_station_id,
        'board': order_data.get("board"),
        'keycap': order_data.get("keycap"),
        'colors': order_data.get("colors", "").split(",")
    }, 200


def _complete_station(station_id, reason=None):
    """완료 버튼 / 조립 타임아웃 공통 경로.

    아카이브·집계·자원 해제는 services/archive.finish_order 가 전부 맡는다.
    cnt_station은 배정 시점에만 올린다 — 배정이 되면 _assign이 올리고,
    안 되면 조립대가 빈 채로 남으므로 다음 배정 때 한 번에 반영된다.
    """
    station_id = str(station_id)
    order_id = r.get(f"{STATION_ORDER_PREFIX}{station_id}")
    if not order_id:
        return None                      # 조기 반환. 아무것도 건드리지 않는다.
    return finish_order(order_id, "completed", reason=reason)


def elapsed_since_arrival(order_data):
    """arrived_at 이후 경과 초. 기록이 없거나 깨졌으면 None."""
    raw = order_data.get("arrived_at")
    if not raw:
        return None
    try:
        return (datetime.now(KST) - datetime.fromisoformat(raw)).total_seconds()
    except ValueError:
        logger.warning(f"[station] arrived_at 파싱 실패 {raw!r}")
        return None


def unclaim_guard(order_data):
    """노쇼 처리해도 되는 시점인가. (ok, elapsed_sec, remain_sec)

    3분 카운트는 프론트가 돌리지만 그건 클라이언트 값이다. 그대로 믿으면 도착 직후에도
    요청 하나로 노쇼 처리가 되므로 서버가 arrived_at 으로 다시 잰다.
    arrived_at 이 없으면(서버 재시작 등) 막지 않는다 — 막으면 조립대가 영영 안 비고,
    그 경우는 워치독 arrived(300초)가 어차피 정리한다.
    """
    elapsed = elapsed_since_arrival(order_data)
    if elapsed is None:
        return True, None, 0
    if elapsed < STATION_UNCLAIM_MIN_SEC:
        return False, int(elapsed), math.ceil(STATION_UNCLAIM_MIN_SEC - elapsed)
    return True, int(elapsed), 0


def check_station_timeouts():
    """QR 인증 후 STATION_TIMEOUT_SEC를 넘긴 조립대를 자동 완료 처리.

    order:deadlines 워치독과 겹치는 경로다. received→completed(600초)를 워치독이 이미 보므로,
    이건 verified_at 기준 백업으로만 남긴다.
    """
    now = datetime.now(KST)
    for key in r.scan_iter(f"{STATION_VERIFIED_PREFIX}*"):
        station_id = key.replace(STATION_VERIFIED_PREFIX, "")
        raw = r.get(key)
        if not raw:
            continue
        try:
            verified_at = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning(f"[timeout] verified_at 파싱 실패 (station={station_id}) {raw!r}")
            continue
        if (now - verified_at).total_seconds() > STATION_TIMEOUT_SEC:
            logger.info(f"[timeout] station {station_id} 조립 타임아웃 → completed")
            _complete_station(station_id, reason="timeout")


# ───────────────────────────────────────────────── 관리자 호출
#
# 호출 상태는 **조립대 소유**다 (station:call:{sid}). 주문이 아니라 조립대에 두는 이유:
#   - 관리자가 묻는 건 "몇 번 조립대로 가야 하나"다. 키 3개만 읽으면 끝난다.
#   - 부품이 안 왔다·화면이 멈췄다처럼 **주문 없이도 눌릴 수 있는 호출**이 존재한다.
#   - 종료된 주문 해시는 ORDER_DONE_TTL(600s)로 줄어 사라지는데, 노쇼·fault 로 끝났는데
#     사람은 아직 조립대 앞에 서 있는 상황이 호출의 주 용도다. order 에 두면 같이 사라진다.
# 누적 횟수만 order:{id}.call_count 에 남겨 아카이브로 넘긴다 (이력·집계용).


def _call_key(station_id):
    return f"{STATION_CALL_PREFIX}{station_id}"


def _waited_sec(called_at):
    if not called_at:
        return None
    try:
        return int((datetime.now(KST) - datetime.fromisoformat(called_at)).total_seconds())
    except ValueError:
        logger.warning(f"[call] called_at 파싱 실패 {called_at!r}")
        return None


def call_state(station_id):
    """이 조립대의 현재 호출. 없으면 None.

    호출 뒤 주문이 넘어갔으면(종료 → 다음 배정) 그 호출은 지난 주문의 것이므로 지운다.
    남겨두면 다음 사용자가 부른 것처럼 관리자 화면에 뜬다.
    주문 없이 걸린 호출(order_id 없음)은 관리자가 해결을 누를 때까지 유지한다.
    """
    sid = str(station_id)
    cur = r.hgetall(_call_key(sid))
    if not cur:
        return None

    oid = cur.get("order_id") or None
    if oid and r.get(f"{STATION_ORDER_PREFIX}{sid}") != oid:
        r.delete(_call_key(sid))
        logger.info(f"[call] station {sid} 호출 정리 — {oid} 는 이 조립대의 현재 주문이 아니다")
        return None

    od = r.hgetall(f"order:{oid}") if oid else {}
    return {
        "station_id": sid,
        "order_id": oid,
        "order_seq": od.get("order_seq"),
        "stage": od.get("stage"),
        "fault": od.get("fault") or None,
        "called_at": cur.get("called_at"),
        "waited_sec": _waited_sec(cur.get("called_at")),
        "count": int(cur.get("count") or 1),
    }


def open_call(station_id):
    """호출 접수. (state, already_open, extended_sec)

    already_open 이면 called_at 은 갱신하지 않는다 — 갱신하면 오래 기다린 사람이
    관리자 화면에서 뒤로 밀린다. 재호출은 count 만 올리고 마감을 한 번 더 밀어준다.
    """
    sid = str(station_id)
    ckey = _call_key(sid)
    order_id = r.get(f"{STATION_ORDER_PREFIX}{sid}") or None

    prev = call_state(sid)                      # 지난 주문의 호출은 여기서 정리된다
    already = prev is not None

    if already:
        r.hincrby(ckey, "count", 1)
    else:
        r.hset(ckey, mapping={"order_id": order_id or "",
                              "called_at": datetime.now(KST).isoformat(timespec="seconds"),
                              "count": 1})

    extended = 0
    if order_id:
        # call_count 를 먼저 올린다 — watchdog._call_bonus 가 상한 판정에 이 값을 본다.
        n = r.hincrby(f"order:{order_id}", "call_count", 1)
        stage = r.hget(f"order:{order_id}", "stage")
        extended = watchdog.extend_for_call(order_id, stage)
        logger.info(f"[call] station {sid} 호출 {n}회 — {order_id} stage={stage} +{extended}s")
    else:
        logger.info(f"[call] station {sid} 호출 — 배정된 주문 없음")

    return call_state(sid), already, extended


def close_call(station_id):
    """관리자 해결. 정리된 호출 상태를 반환하고, 호출 중이 아니었으면 None.

    해결 시점부터 기본 마감을 다시 잰다 — 조치가 끝났어도 머신이 다시 움직일 시간은 필요하다.
    단 STAGE_TIMEOUT 에 없는 stage(종료·unclaimed)는 건드리지 않는다. unclaimed 는
    archive.finish_order 가 직접 건 폐기 마감을 들고 있어서, arm() 을 부르면 그게 지워지고
    조립대가 영구 점유된다.
    """
    sid = str(station_id)
    state = call_state(sid)
    if state is None:
        return None
    r.delete(_call_key(sid))

    oid = state.get("order_id")
    stage = state.get("stage")
    if oid and stage in watchdog.STAGE_TIMEOUT:
        watchdog.arm(oid, stage)               # 호출 보너스가 빠진 기본 마감
    logger.info(f"[call] station {sid} 호출 해결 — {oid or '주문 없음'} "
                f"대기 {state.get('waited_sec')}s ({state.get('count')}회)")
    return state


def open_calls():
    """열려 있는 호출 전부. 오래 기다린 순."""
    out = [c for c in (call_state(sid) for sid in STATIONS) if c]
    return sorted(out, key=lambda c: c.get("called_at") or "")


def clear_call(station_id):
    """조립대 해제·리셋 경로에서 호출 상태를 지운다 (로그도 마감 재장전도 없다)."""
    return r.delete(_call_key(str(station_id)))
