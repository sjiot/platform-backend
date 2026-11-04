"""카트리지 36칸 — 저장 · 조회 · 부팅 시드 · 예약 · 배치 판정.

저장소는 `warehouse:stock` 해시 하나.  field = 카트리지 id, value = {"count", "status"}

    키캡 32칸  E_r · E_y · … · P_b     ({EISNTFJP} × {r y g b})
    보드  4칸  r · y · g · b

카트리지가 각자 CIN을 올리므로 통짜 JSON으로는 못 담는다. HSET이 필드 단위로 원자적이라
34개 콜백이 동시에 들어와도 서로 덮어쓰지 않는다. 조회 API에 나갈 중첩 형태는 읽을 때 조립한다.
"""
import json
from collections import Counter
from datetime import datetime

from config import (QUEUE_KEY,
                    STATION_ORDER_PREFIX,
                    WAREHOUSE_DRIVER_KEY,
                    WAREHOUSE_RESTOCK_KEY,
                    KEYCAP_STOCK_MAX_COUNT,
                    BOARD_STOCK_MAX_COUNT,
                    KST,
                    COLOR_LIST,
                    BOARD_LIST,
                    MBTI_AXES,
                    KEYCAP_SLOTS as _CFG_KEYCAP_SLOTS,
                    BOARD_SLOTS as _CFG_BOARD_SLOTS,
                    BOARD_SLOT_MAP as _CFG_BOARD_SLOT_MAP,
                    SUB_CNT_KEYCAP_PARENT,
                    SUB_CNT_BOARD_PARENT,
                   )
from infra.extensions import r
from infra.logger import logger
from infra.mobius import get_latest_con

STOCK_KEY = "warehouse:stock"
DRIVER_KEY = WAREHOUSE_DRIVER_KEY
RESTOCK_KEY = WAREHOUSE_RESTOCK_KEY
STATIONS = ("1", "2", "3")

# env(SUB_CNT_*)가 비어 있으면 규칙대로 생성
_LETTERS = "".join(a + b for a, b in MBTI_AXES)                       # EISNTFJP
KEYCAP_SLOTS = _CFG_KEYCAP_SLOTS or [f"{l}_{c}" for l in _LETTERS for c in COLOR_LIST]
BOARD_SLOTS = _CFG_BOARD_SLOTS or list(COLOR_LIST)
ALL_SLOTS = set(KEYCAP_SLOTS) | set(BOARD_SLOTS)

# 주문의 board는 'blue', 카트리지는 'b'. 명세 10-4에서 'b' 통일 요청 중 — 통일되면 이 표를 지운다.
BOARD_SLOT_MAP = _CFG_BOARD_SLOT_MAP or dict(zip(BOARD_LIST, COLOR_LIST))

# 그 칸을 쓰는 주문을 내보낼 수 없는 상태
BLOCKING_STATUS = {"disable", "empty"}

# 예약 구간 — 카트리지 count는 배출 순간에 줄므로 그 전까지만 잡는다 (명세 8-1)
KEYCAP_RESERVED = {"queued", "assigned", "keycap_mismatched"}
BOARD_RESERVED = KEYCAP_RESERVED | {"keycap_dispensed", "keycap_reached",
                                    "keycap_packed", "tray_reached", "board_mismatched"}


def is_board(slot):
    return "_" not in slot                      # 보드는 'r', 키캡은 'E_r'


# ───────────────────────────────────────────────── 선반(드라이버) 4개
#
# 주문의 MBTI 4글자는 선반 1~4 에서 각각 하나씩 나온다. config.MBTI_AXES 순서가 선반 번호와 같다.
#     keycap1 = E/I · keycap2 = S/N · keycap3 = T/F · keycap4 = J/P
# 표를 새로 하드코딩하지 않고 파생시킨다 — 두 곳에 두면 갈라진다.
SHELF_OF_LETTER = {ch: f"keycap{i + 1}" for i, ax in enumerate(MBTI_AXES) for ch in ax}
SHELVES = tuple(f"keycap{i + 1}" for i in range(len(MBTI_AXES)))


def shelf_of(slot):
    """'E_r' → 'keycap1'. 보드는 선반 개념이 없어 None."""
    if is_board(slot):
        return None
    return SHELF_OF_LETTER.get(slot.split("_", 1)[0])


def slots_of_shelf(shelf):
    """그 선반에 속한 칸 8개."""
    return [s for s in KEYCAP_SLOTS if shelf_of(s) == shelf]


def section_of(slot, shelf_only=False):
    """fault_section 문자열. 형식은 part[:scope[:slot]] — 왼쪽이 넓고 오른쪽이 좁다.

        section_of("E_r")                   → keycap_cartridge:keycap1:E_r
        section_of("E_r", shelf_only=True)  → keycap_cartridge:keycap1   (칸 생략 = 선반 전체)
        section_of("b")                     → board_cartridge:b
    """
    if is_board(slot):
        return f"board_cartridge:{slot}"
    shelf = shelf_of(slot) or "?"
    return f"keycap_cartridge:{shelf}" if shelf_only else f"keycap_cartridge:{shelf}:{slot}"


def mark_driver_failed(shelf, reason, slot=None, order_id=None):
    """선반 고장 등록. 이 선반의 8칸을 쓰는 주문은 배정에서 뒤로 밀린다 (blockers)."""
    r.hset(DRIVER_KEY, shelf, json.dumps({"reason": reason, "slot": slot,
                                          "order_id": order_id,
                                          "at": datetime.now(KST).isoformat(timespec="seconds")},
                                         ensure_ascii=False))


def clear_driver_failed(shelf):
    """그 선반의 칸에서 정상 status 가 올라왔다 = 드라이버가 살아났다. 지운 개수를 반환."""
    return r.hdel(DRIVER_KEY, shelf) if shelf else 0


def revive_slots(slots):
    """disable 로 막힌 칸만 idle 로 되돌린다. 되돌린 칸 목록을 반환.

    관리자가 fault 를 해제했다 = 그 설비를 고쳤다는 선언이다. 그런데 칸 status 는 창고가
    보낸 disable 그대로라, 이걸 안 되돌리면 그 칸을 쓰는 다음 주문이 blockers 에서 계속 막힌다.

    두 가지는 일부러 건드리지 않는다.
      count   창고가 보낸 마지막 값이 맞다. disable 과 함께 0 이 왔으면 count_0 으로
              계속 막히는 게 맞다 — 그건 채워야 풀리는 것이지 고쳐서 풀리는 게 아니다.
      empty   재고가 없는 것이지 고장이 아니다. idle 로 덮으면 빈 칸이 정상으로 보인다.
    """
    revived = []
    for slot in slots:
        s = get(slot)
        if s and s.get("status") == "disable":
            put(slot, s.get("count"), "idle")
            revived.append(slot)
    return revived


def driver_failed_shelves():
    """고장 난 선반 집합. 호출이 잦아 필드 이름만 읽는다."""
    return set(r.hkeys(DRIVER_KEY) or [])


def driver_failed_info():
    """관리자·조회용 — {shelf: {reason, slot, order_id, at}}."""
    out = {}
    for shelf, raw in (r.hgetall(DRIVER_KEY) or {}).items():
        try:
            out[shelf] = json.loads(raw)
        except (TypeError, ValueError):
            out[shelf] = {"raw": raw}
    return out


def max_count(slot):
    """그 칸의 최대 수량. 보드 보관대(3)와 키캡 카트리지(40)는 용량이 다르다."""
    return BOARD_STOCK_MAX_COUNT if is_board(slot) else KEYCAP_STOCK_MAX_COUNT


def container_path(slot):
    parent = SUB_CNT_BOARD_PARENT if is_board(slot) else SUB_CNT_KEYCAP_PARENT
    return f"cnt_warehouse/{parent}/cnt_{slot}"


# ───────────────────────────────────────────────── 저장 · 조회

def put(slot, count, status):
    """콜백 1건 반영. HSET은 필드 단위 원자적이라 락이 필요 없다."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    r.hset(STOCK_KEY, slot, json.dumps({"count": count, "status": status}))


def handle_stock_notification(con, slot):
    """cnt_*_status 구독 콜백에서 호출."""
    put(slot, con.get("count"), con.get("status"))


def get(slot):
    """{"count": n, "status": s} 또는 None(아직 한 번도 못 받음)."""
    raw = r.hget(STOCK_KEY, slot)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def all_states():
    out = {}
    for slot, raw in (r.hgetall(STOCK_KEY) or {}).items():
        try:
            out[slot] = json.loads(raw)
        except (ValueError, TypeError):
            continue
    return out


def get_stocks():
    """GET /stock — 문서 형태 그대로 카운트만. 한 칸도 못 받았으면 None.

        {"keycap": {"E_r": 50, "E_y": 48, ...}, "board": {"r": 30, ...}}

    Redis에는 칸마다 {"count", "status"}가 들어 있고, 응답에 쓸 형태는 여기서 조립한다.
    """
    states = all_states()
    if not states:
        return None
    out = {"keycap": {}, "board": {}}
    for slot, v in states.items():
        out["board" if is_board(slot) else "keycap"][slot] = int(v.get("count") or 0)
    return out


def get_status():
    """GET /stock/status — 같은 형태로 status만. 카트리지 idle 조회용.

        {"keycap": {"E_r": "idle", ...}, "board": {"r": "busy", ...}}

    한 번도 못 받은 칸은 "unknown"으로 채워서 36칸이 항상 다 나온다.
    """
    states = all_states()
    out = {"keycap": {}, "board": {}}
    for slot in KEYCAP_SLOTS:
        out["keycap"][slot] = (states.get(slot) or {}).get("status") or "unknown"
    for slot in BOARD_SLOTS:
        out["board"][slot] = (states.get(slot) or {}).get("status") or "unknown"
    return out


def get_out_of_stock():
    """GET /stock/out — 지금 주문받을 수 없는 항목. 멀쩡한 카테고리는 키 자체를 안 넣는다.

        {"keycap": ["E_r"], "board": ["blue"]}

    보드는 카트리지 코드('b')가 아니라 주문 표기('blue')로 돌려준다. 이 응답을 쓰는 쪽은
    키오스크고, 키오스크는 POST /order에 'blue'를 실으므로 같은 표기로 대조해야 한다.
    """
    states = all_states()
    if not states:
        return None
    code_to_board = {v: k for k, v in BOARD_SLOT_MAP.items()}
    dead = driver_failed_shelves()

    out = {}
    for slot, v in states.items():
        # 드라이버가 죽은 선반은 칸 status 가 idle 이어도 못 낸다 — 8칸 전부 품절로 내린다.
        if shelf_of(slot) in dead:
            out.setdefault("keycap", []).append(slot)
            continue
        if int(v.get("count") or 0) > 0 and v.get("status") not in BLOCKING_STATUS:
            continue
        if is_board(slot):
            out.setdefault("board", []).append(code_to_board.get(slot, slot))
        else:
            out.setdefault("keycap", []).append(slot)
    for v in out.values():
        v.sort()
    return out


# ───────────────────────────────────────────────── 관리자 채움 → 창고 반영 대기
#
# cnt_process 의 keycap_restock / board_restock 은 "count 가 0 인 칸" 이 아니라
# "관리자 콘솔에서 채웠는데 창고가 아직 반영하지 않은 칸" 이다. 창고가 0 을 보고했다고
# 바로 목록에 넣으면, 사람이 실제로 채우기 전에 창고가 자기 카운터를 되돌려 버린다.
#   POST /admin/stock/refill  → mark_restocked  → push_process (목록에 들어간다)
#   창고가 그 칸 count>0 보고 → clear_restocked → push_process (목록에서 빠진다)

def mark_restocked(slot):
    """관리자가 채웠다. 창고가 count>0 을 올릴 때까지 restock 목록에 남는다."""
    return r.sadd(RESTOCK_KEY, slot)


def clear_restocked(slot):
    """창고가 반영했다(또는 관리자가 0 으로 되돌렸다). 지운 개수(0/1)를 반환."""
    return r.srem(RESTOCK_KEY, slot)


def restocked_slots():
    return set(r.smembers(RESTOCK_KEY) or [])


def restock_list(kind):
    """cnt_process 에 실을 칸 — 관리자가 채웠고 창고가 아직 반영 전인 것.

    count 0 · empty 는 보지 않는다. 그건 /stock/out(키오스크 품절) 과 blockers 가 맡는다.
    정렬해서 돌려준다 — 순서가 흔들리면 내용이 같은데 push_process 지문이 달라진다.
    """
    want_board = (kind == "board")
    return sorted(slot for slot in restocked_slots() if is_board(slot) == want_board)


# ───────────────────────────────────────────────── 부팅 시드

def seed_from_mobius():
    """부팅 시 1회. 구독은 '앞으로 바뀌는 것'만 알려주므로 현재 값은 직접 읽어야 한다.

    되읽지 않기로 한 건 우리가 쓴 cnt_process 쪽이고, 이건 창고가 쓴 컨테이너라 읽어도 된다.
    36번 GET이라 몇 초 걸린다.
    """
    ok = miss = 0
    for slot in KEYCAP_SLOTS + BOARD_SLOTS:
        con = get_latest_con(container_path(slot))
        if con is None:
            miss += 1
            continue
        put(slot, con.get("count"), con.get("status"))
        ok += 1
    logger.info(f"[stock] 시드 {ok}/{len(ALL_SLOTS)} (미수신 {miss})")
    return ok, miss


# ───────────────────────────────────────────────── 주문 ↔ 카트리지

def _slots_from_hash(order, kind=None):
    """order 해시에서 카트리지 id를 뽑는다. colors는 콤마 문자열로 저장돼 있다."""
    board = order.get("board")
    boards = [BOARD_SLOT_MAP.get(board, board)] if board else []

    letters = order.get("keycap") or ""
    colors = [c for c in (order.get("colors") or "").split(",") if c]
    keycaps = [f"{ch}_{c}" for ch, c in zip(letters, colors)]

    if kind == "keycap":
        return keycaps
    if kind == "board":
        return boards
    return keycaps + boards


def parts_of(order_id, kind=None):
    """주문이 쓰는 카트리지 id.
        parts_of(oid)           → ['E_r', 'S_y', 'F_b', 'J_g', 'b']
        parts_of(oid, "keycap") → ['E_r', 'S_y', 'F_b', 'J_g']
    """
    return _slots_from_hash(r.hgetall(f"order:{order_id}") or {}, kind)


def slots_for(board, keycap, colors):
    """아직 주문이 만들어지기 전(POST /order 검증 단계)에 쓰는 형태."""
    return [f"{ch}_{c}" for ch, c in zip(keycap or "", colors or [])] + \
           [BOARD_SLOT_MAP.get(board, board)]


# ───────────────────────────────────────────────── 예약 (접수 판정)

def _live_order_ids():
    """큐 + 조립대에 배정된 주문. 최대 3 + 3 = 6건."""
    ids = list(r.lrange(QUEUE_KEY, 0, -1))
    for sid in STATIONS:
        oid = r.get(f"{STATION_ORDER_PREFIX}{sid}")
        if oid and oid not in ids:
            ids.append(oid)
    return ids


def reserved_map():
    """살아있는 주문들이 잡아둔 카트리지 수량.

    NOTE 접수 판정(can_accept)에서 빠졌다. 지금은 호출자가 없고,
    명세 8-1의 선제 restock(available = count - reserved < 5)을 구현할 때 쓴다.

    별도 카운터를 두지 않고 매번 센다 — 카운터는 주문이 fault로 죽거나 서버가 재시작될 때
    안 풀려서 실제 재고와 영구히 어긋난다 (명세 8-1).

    큐만 세면 안 된다. 배정된 주문은 큐에서 빠지는데 배출 전이면 count도 안 줄어서,
    새 주문이 같은 칸을 예약 없이 통과한다.
    """
    c = Counter()
    for oid in _live_order_ids():
        o = r.hgetall(f"order:{oid}")
        if not o:
            continue
        stage = o.get("stage")
        if stage in KEYCAP_RESERVED:
            c.update(_slots_from_hash(o, "keycap"))
        if stage in BOARD_RESERVED:
            c.update(_slots_from_hash(o, "board"))
    return c


def can_accept(board, keycap, colors):
    """POST /order 접수 판정. (가능?, 모자란 칸 목록)

    Redis에 저장된 재고 상태만 본다 — 예약분은 빼지 않는다.
    /stock/out(키오스크가 보는 품절 목록)과 기준을 같게 맞춘 것이다. 예약까지 빼면
    키오스크에 선택 가능하게 보이는 조합이 접수에서 409로 거절돼 기준이 둘로 갈린다.

    큐 안 주문끼리의 카트리지 경합은 접수에서 막지 않고 배정 단계(blockers)에서
    '대기'로 흡수한다 — 재고가 채워지면 on_stock_recovered가 그 주문을 다시 집는다.
    """
    short = []
    dead = driver_failed_shelves()
    for slot in slots_for(board, keycap, colors):
        if shelf_of(slot) in dead:
            short.append(slot)
            continue
        s = get(slot)
        if s is None or s.get("status") in BLOCKING_STATUS:
            short.append(slot)
        elif int(s.get("count") or 0) <= 0:
            short.append(slot)
    return (not short), sorted(set(short))


# ───────────────────────────────────────────────── 배치 판정

def blockers(order_id):
    """이 주문을 지금 못 내보내는 이유. 빈 리스트면 창고가 처리할 수 있다.

    예약을 보지 않는다 — 배치 시점엔 이 주문 자신이 살아있는 주문에 포함돼서
    자기 자신을 예약으로 세면 영원히 배치되지 않는다. 예약은 접수 판정에서만 쓴다.
    """
    out = []
    dead = driver_failed_shelves()
    for slot in parts_of(order_id):
        if shelf_of(slot) in dead:
            # 선반 고장은 칸을 채워도 안 풀린다. SKIP_BLOCKED 로 이 주문은 뒤로 밀리고
            # blocked_by 에 이유가 남는다 (GET /queue/status).
            out.append([slot, "driver_failed"])
            continue
        s = get(slot)
        if s is None:
            out.append([slot, "unknown"])            # 시드도 콜백도 못 받은 칸 — 모르면 막는다
        elif s.get("status") in BLOCKING_STATUS:
            out.append([slot, s.get("status")])
        elif int(s.get("count") or 0) <= 0:
            out.append([slot, "count_0"])
    return out
