"""재기동·사고 복구 — Redis 를 알려진 상태로 되돌린다.

Redis 는 배포로 지워지지 않는다. 그래서 이건 **부팅 훅이 아니라 명시적으로 부르는 것**이다.
Cloud Run 은 트래픽에 따라 인스턴스를 새로 띄우므로, 부팅에 걸어두면 운영 중에 인스턴스가
하나 늘어날 때마다 진행 중인 주문이 날아간다.

두 모드.

    zero    전부 초기값. 재고는 칸별 최대치(키캡 40 · 보드 3), 상태는 idle, 주문은 전부 삭제.
            전시회 첫날 시작 전처럼 '아무것도 진행 중이 아니어야 할 때'.

    mobius  zero 를 돌린 뒤 Mobius 최신값으로 덮어쓴다. 머신이 실제로 어디 있는지를
            되찾는다 — 사고 후 복구, 2일차 시작 전.

읽는 것은 **구독을 건 컨테이너뿐**이다. cnt_order·cnt_station 은 우리가 쓴 기록이라 안 읽는다.

**리셋은 모드와 상관없이 영업 상태를 closed 로 둔다** (services/line.py). 지우는 도중에 주문이
끼어들지 않게 맨 먼저 닫고, 다시 여는 건 사람이 스냅샷(GET /admin/debug/snapshot)으로
상태를 확인한 뒤 POST /admin/line 으로 한다.

[ 복구가 안 되는 것 — 알고 쓰는 것과 모르고 당하는 것은 다르다 ]

  order:queue   대기열은 Mobius 어디에도 없다. cnt_process 는 조립대에 올라간 주문만
                싣는다. keep_queue=True 로 Redis 의 큐를 보존하는 길밖에 없다.

  arrived_at    cnt_process con 에 없다. 복구 시각으로 다시 잡는다 — 노쇼 3분이
                그 시점부터 다시 흐른다. 보수적인 쪽이다.

  created_at    마찬가지로 없다. 복구 시각으로 넣는다. 아카이브의 소요시간이 짧게 찍힌다.

  collector     '키캡 4개 중 몇 개가 나왔나'는 stage 만으로 알 수 없다.
                assigned·keycap_mismatched 는 cnt_keycap_dispense 최근 CIN 을 대조해
                정확히 복구한다. 그보다 깊은 창고 구간은 근거가 없어 복구하지 않고
                fault="restart_unknown" 으로 세운다 — 잘못 짐작하면 배출이 중복되고
                로봇암이 빈 트레이에 키캡을 넣는다. 사람이 보고 푸는 게 맞다.
                (실측상 창고 구간 체류는 assigned 가 ~18초로 가장 길고 나머지는 1~6초다.)
"""
import json
import time
from datetime import datetime

from config import (AGV_KEY,
                    AGV_STATUS_KEY,
                    AGV_FAULT_KEY,
                    ARCHIVE_PREFIX,
                    ORDER_COUNTER_KEY,
                    DEADLINE_KEY,
                    KST,
                    QUEUE_KEY,
                    STAGE_ORDER,
                    STATION_KEY,
                    STATION_ORDER_PREFIX,
                    STATION_VERIFIED_PREFIX,
                    STATION_CALL_PREFIX,
                    WAREHOUSE_KEY,
                    WAREHOUSE_ORDER_KEY,
                    WAREHOUSE_DRIVER_KEY,
                    WAREHOUSE_RESTOCK_KEY,
                    WAREHOUSE_FAULT_KEY,
                    KEYCAP_STOCK_MAX_COUNT,
                    BOARD_STOCK_MAX_COUNT,
                   )
from infra.extensions import r
from infra.keys import _touch_order
from infra.logger import logger
from infra.mobius import get_latest_con, get_recent_cons

from services import collector, stock, watchdog

STATIONS = ("1", "2", "3")

# 창고가 주문을 물고 있는 구간. pickup_reached 에서 release_warehouse 가 푼다.
_i = STAGE_ORDER.index
WAREHOUSE_STAGES = set(STAGE_ORDER[_i("assigned"):_i("pickup_reached")])

# 그중 배출 CIN 대조로 정확히 복구되는 stage. 나머지는 fault 로 세운다.
RECOVERABLE_STAGES = {"assigned", "keycap_mismatched"}

# 배출 CIN 을 몇 건까지 훑을지. 주문당 키캡 4건이라 8건이면 최근 2주문치다.
# 창고는 1대라 진행 중 주문은 1건뿐이므로 충분하고, 개별 GET 이 건당 0.11초라 값도 싸다.
DISPENSE_LOOKBACK = 8

# 삭제 대상 — 상태·진행 키. 아카이브·집계·일련번호는 건드리지 않는다.
# process:seq 는 **지우지 않는다.** 머신은 "seq 가 안 오르면 무시" 로 재전송을 거르는데,
# seq 를 1부터 다시 시작하면 그 필터에 걸려 복구 뒤 모든 스냅샷이 버려진다.
# 내용 지문(process:last)만 지워서 복구 직후 한 번은 반드시 나가게 한다.
_STATE_KEYS = [WAREHOUSE_KEY, WAREHOUSE_ORDER_KEY, WAREHOUSE_DRIVER_KEY, WAREHOUSE_RESTOCK_KEY, WAREHOUSE_FAULT_KEY,
               STATION_KEY, AGV_KEY, AGV_STATUS_KEY, AGV_FAULT_KEY,
               DEADLINE_KEY, "process:last",
               "robot:status",                       # 안 쓰는 옛 키. 남아 있으면 같이 정리
               "stock:seed:lock", "watchdog:lock"]
_STATE_KEYS += [f"{STATION_ORDER_PREFIX}{s}" for s in STATIONS]
_STATE_KEYS += [f"{STATION_VERIFIED_PREFIX}{s}" for s in STATIONS]
_STATE_KEYS += [f"{STATION_CALL_PREFIX}{s}" for s in STATIONS]

_ORDER_PATTERNS = ["order:ord_*", "order:*:pending", "order:*:wrong", "order:*:gate",
                   "idempotency:*"]

# mode=hard 에서만 지우는 것 — 90일 보관 기록이다.
# "stats:*" 는 폐기된 집계 키다 (2026-09-18). 새로 쓰는 코드는 없지만 실 Redis 에
# 예전 것이 남아 있어서, hard 리셋이 같이 걷어가도록 패턴만 남긴다. 한 번 돌리면 없어진다.
_ARCHIVE_PATTERNS = [f"{ARCHIVE_PREFIX}*", "stats:*", f"{ORDER_COUNTER_KEY}*"]

# 이 서비스가 쓰는 키의 prefix 전부. hard 모드가 '모르는 키' 를 가려낼 때 기준이 된다.
# 실 Redis 는 배포본과 같은 DB 라 FLUSHDB 를 쓰면 남의 키까지 날아간다.
_KNOWN_PREFIXES = ("order:", "station:", "warehouse:", "agv:", "process:",
                   "idempotency:", ARCHIVE_PREFIX, "stats:",   # stats: 는 레거시
                   "line:",                                    # 영업 상태 (services/line.py)
                   "kiosk:",                                   # 영수증 용지 kiosk:paper — 리셋해도 안 지움
                   "robot:status", "watchdog:lock", "stock:seed:lock")

KEEP_NOTE = ["orders:archive:*", "order:counter:*"]
KEEP_NOTE_HARD = ["process:seq (reset_seq 로만 삭제)"]


# ───────────────────────────────────────────────────────────── 삭제
def _unknown_keys(limit=200):
    """이 서비스가 쓰지 않는 키. hard 모드가 '전부 지웠다' 고 말하기 전에 확인한다."""
    out = []
    for key in r.scan_iter("*", count=500):
        if not key.startswith(_KNOWN_PREFIXES):
            out.append(key)
            if len(out) >= limit:
                break
    return sorted(out)


def _wipe(keep_queue=False, hard=False, reset_seq=False, flush_unknown=False):
    """상태·주문 키를 지운다. keep_queue 면 큐와 그 큐에 실린 주문 해시는 남긴다.

    hard=True 면 아카이브·집계·일련번호까지 지운다 (90일 기록이 사라진다).
    reset_seq=True 면 process:seq 까지 — 머신의 'seq 안 오르면 무시' 필터가
    깨지므로 머신도 같이 재시작해야 한다. 그래서 기본값이 False 다.
    """
    keep_ids = set(r.lrange(QUEUE_KEY, 0, -1)) if keep_queue else set()
    keep_keys = set()
    for oid in keep_ids:
        keep_keys.update([f"order:{oid}",
                          *[f"order:{oid}:{s}:{k}"
                            for s in ("keycap", "board", "keycap_pack")
                            for k in ("pending", "wrong")],
                          f"order:{oid}:gate"])

    targets = [k for k in _STATE_KEYS if k not in keep_keys]
    if not keep_queue:
        targets.append(QUEUE_KEY)

    patterns = list(_ORDER_PATTERNS)
    counts = {}
    if hard:
        patterns += _ARCHIVE_PATTERNS
        for pat in _ARCHIVE_PATTERNS:
            counts[pat] = len(list(r.scan_iter(pat, count=500)))
    if reset_seq:
        targets.append("process:seq")

    for pat in patterns:
        for key in r.scan_iter(pat, count=500):
            if key not in keep_keys:
                targets.append(key)

    unknown = _unknown_keys() if hard else []
    if hard and flush_unknown and unknown:
        targets += unknown

    deleted = 0
    for i in range(0, len(targets), 200):
        deleted += r.delete(*targets[i:i + 200])

    logger.info(f"[reset] 키 {deleted}개 삭제"
                + (" · hard(아카이브·집계·일련번호 포함)" if hard else "")
                + (" · process:seq 리셋" if reset_seq else "")
                + (f" · 큐 {len(keep_ids)}건 보존" if keep_queue else " · 큐 삭제"))

    out = {"deleted": deleted, "kept_queue": sorted(keep_ids)}
    if hard:
        out["archive"] = counts.get(f"{ARCHIVE_PREFIX}*", 0)
        out["counters"] = counts.get(f"{ORDER_COUNTER_KEY}*", 0)
        out["unknown_keys"] = [] if flush_unknown else unknown
        if unknown and not flush_unknown:
            out["note"] = ("이 서비스가 쓰지 않는 키다. 배포본과 같은 DB 라 건드리지 않았다. "
                           "지우려면 flush_unknown:true")
    return out


# ───────────────────────────────────────────────────────────── 초기값
def _seed_zero():
    """재고는 max, 나머지는 idle. '아무것도 진행 중이 아닌' 상태."""
    slots = list(stock.KEYCAP_SLOTS) + list(stock.BOARD_SLOTS)
    for slot in slots:
        stock.put(slot, stock.max_count(slot), "idle")   # 키캡 40 · 보드 3

    r.set(WAREHOUSE_KEY, "idle")
    r.hset(STATION_KEY, mapping={s: "idle" for s in STATIONS})
    r.set(AGV_KEY, "idle")

    logger.info(f"[reset] 초기값 — 키캡 {len(stock.KEYCAP_SLOTS)}칸={KEYCAP_STOCK_MAX_COUNT} · "
                f"보드 {len(stock.BOARD_SLOTS)}칸={BOARD_STOCK_MAX_COUNT} · 전 설비 idle")
    return {"stock_slots": len(slots),
            "keycap_count": KEYCAP_STOCK_MAX_COUNT,
            "board_count": BOARD_STOCK_MAX_COUNT}


# ───────────────────────────────────────────────────────── Mobius 복구
def _restore_stock():
    ok, miss = stock.seed_from_mobius()
    logger.info(f"[reset] 재고 복구 {ok}칸 (미수신 {miss}칸은 max 유지)")
    return {"ok": ok, "miss": miss}


def _restore_agv():
    """cnt_agv 최신 보고. parked 여야 idle 이다 (callbacks/agv.py 와 같은 규칙)."""
    con = get_latest_con("cnt_agv") or {}
    status = str(con.get("status") or "")
    if not status:
        logger.warning("[reset] cnt_agv 보고 없음 — idle 로 둔다")
        return {"status": None, "occupancy": "idle"}
    r.set(AGV_STATUS_KEY, status)
    occ = "idle" if status == "parked" else "busy"
    r.set(AGV_KEY, occ)
    logger.info(f"[reset] AGV 복구 status={status} occupancy={occ}")
    return {"status": status, "occupancy": occ}


def _dispensed_keycaps(order_id):
    """cnt_keycap_dispense 최근 CIN 에서 이 주문이 이미 뽑은 키캡 목록.

    반환 순서가 최신→과거라 그대로 세면 된다 (infra/mobius.get_recent_cons).
    """
    got = []
    for con in get_recent_cons("cnt_warehouse/cnt_keycap_dispense", DISPENSE_LOOKBACK):
        if con.get("order_id") == order_id and con.get("keycap"):
            got.append(con["keycap"])
    return got


def _restore_collector(order_id, stage, tag="reset", remove_only=False):
    """배출 CIN 을 대조해 미배출 목록을 다시 깐다.

    반환은 **미배출 목록(list)**, 대조할 근거가 없으면 None.
    빈 리스트는 '4개 다 나왔다'는 유효한 결과다 — falsy 라고 실패로 읽으면 안 된다.

    기준선(base)이 경로마다 다르다.

      재기동(/admin/reset)  기대 4개.  주문 키가 통째로 날아간 뒤라 처음부터 다시 깐다.
      fault 해제            **현재 pending**.  Mobius 가 확인해 준 것을 빼기만 하고,
                            이미 소진된 것을 되살리지 않는다 (remove_only=True).

    fault 해제에서 기대 4개를 기준으로 삼으면 안 되는 이유 — 콜백은 fault 중에도
    collector 를 갱신하므로 Redis 쪽이 더 정확한데, Mobius 를 못 읽거나 lookback(8건)을
    넘기면 제대로 소진된 pending 을 통째로 4개로 되돌려 놓는다 (실측).
    Mobius 조회가 실패하면 dispensed 가 비어 remaining == base 가 되어 아무것도 안 건드린다.
    """
    if stage not in RECOVERABLE_STAGES:
        return None

    expected = list(stock.parts_of(order_id, "keycap"))       # 4개
    base = collector.remaining(order_id, "keycap") if remove_only else list(expected)
    if remove_only and not base:
        logger.info(f"[{tag}] {order_id} pending 이 비어 있다 — 재동기화 생략 (기대 {expected})")
        return []

    dispensed = _dispensed_keycaps(order_id)

    remaining = list(base)
    for k in dispensed:
        if k in remaining:
            remaining.remove(k)                                # 중복 배출도 1건씩만 상쇄

    if remove_only and remaining == base:
        logger.info(f"[{tag}] {order_id} 미배출 {remaining} 그대로 (배출확인 {dispensed})")
        return remaining

    collector.arm(order_id, "keycap", remaining)
    if not remove_only:
        collector.reset_gate(order_id)      # 재기동 때만. 해제 경로에서 남의 플래그를 지우지 않는다
    logger.info(f"[{tag}] {order_id} 미배출 복구 {remaining} "
                f"(기준 {base} · 배출확인 {dispensed})")
    return remaining


def _restore_process():
    """cnt_process 최신 스냅샷으로 진행 중 주문·조립대·창고를 되살린다."""
    snap = get_latest_con("cnt_process")
    if not snap:
        logger.warning("[reset] cnt_process 를 못 읽었다 — 진행 중 주문 없음으로 둔다")
        return {"seq": None, "orders": [], "faulted": []}

    now = datetime.now(KST).isoformat()
    orders, faulted = [], []

    for oid, o in (snap.get("orders") or {}).items():
        if not isinstance(o, dict):         # {"open": true} — 진행 주문 없음 표시 (process._build_body)
            continue
        stage = o.get("status")
        sid = str(o.get("station_id") or "")
        if not stage or stage not in STAGE_ORDER:
            logger.warning(f"[reset] {oid} 알 수 없는 status={stage} — 건너뜀")
            continue

        mapping = {
            "stage": stage,
            "order_seq": str(o.get("order_seq") or 0),
            "board": o.get("board") or "",
            "keycap": o.get("keycap") or "",
            "colors": ",".join(o.get("colors") or []),
            "created_at": now,                 # 원본이 없다. 복구 시각으로 둔다
        }
        if sid:
            mapping["station_id"] = sid
        if o.get("fault"):
            mapping["fault"] = o["fault"]
            if o.get("fault_section"):
                mapping["fault_section"] = o["fault_section"]
        # 노쇼 타이머 기준. 원본이 없으므로 지금부터 다시 센다 (보수적).
        if STAGE_ORDER.index(stage) >= STAGE_ORDER.index("arrived"):
            mapping["arrived_at"] = now

        r.hset(f"order:{oid}", mapping=mapping)
        _touch_order(oid)

        if sid:
            r.set(f"{STATION_ORDER_PREFIX}{sid}", oid)
            r.hset(STATION_KEY, sid, "busy")

        if stage in WAREHOUSE_STAGES:
            r.set(WAREHOUSE_KEY, "busy")
            r.set(WAREHOUSE_ORDER_KEY, oid)
            if _restore_collector(oid, stage) is None:
                # 근거 없이 짐작하면 배출이 중복된다. 세워놓고 사람이 보게 한다.
                r.hset(f"order:{oid}", mapping={"fault": "restart_unknown",
                                                "fault_section": "keycap_cartridge"})
                faulted.append(oid)
                logger.error(f"[reset] {oid} stage={stage} — 진행분을 알 수 없어 "
                             f"fault=restart_unknown (관리자 확인 후 해제)")

        # 마감 재장전. fault 가 붙었으면 걸지 않는다 (set_fault 와 같은 규칙).
        if not r.hget(f"order:{oid}", "fault"):
            watchdog.arm(oid, stage)

        orders.append({"order_id": oid, "stage": stage, "station_id": sid or None})

    # seq 는 뒤로 가면 안 된다. Mobius 가 기록의 진실이지만, 혹시 Redis 쪽이 더 앞서
    # 있으면(마지막 push 직후 CIN 생성이 실패한 경우) 큰 값을 쓴다.
    seq = snap.get("seq")
    if isinstance(seq, int):
        cur = int(r.get("process:seq") or 0)
        r.set("process:seq", max(seq, cur))
    logger.info(f"[reset] cnt_process 복구 seq={seq} 주문 {len(orders)}건 "
                f"(fault {len(faulted)}건)")
    return {"seq": seq, "orders": orders, "faulted": faulted}


# ───────────────────────────────────────────────────────────── 진입점
def reset(mode="zero", keep_queue=False, reset_seq=False, flush_unknown=False):
    """mode = "zero" | "mobius" | "hard". 결과 보고서를 dict 로 돌려준다.

        zero    상태·주문 키만. 아카이브·집계·일련번호는 남는다
        mobius  zero 뒤 Mobius 최신값으로 복구
        hard    zero + 아카이브·집계·일련번호까지. 90일 기록이 사라진다
    """
    if mode not in ("zero", "mobius", "hard"):
        raise ValueError(f"알 수 없는 mode: {mode}")

    from services.process import push_process
    from services import line

    hard = mode == "hard"
    t0 = time.time()
    report = {"mode": mode, "keep_queue": bool(keep_queue),
              "kept": KEEP_NOTE_HARD if hard else KEEP_NOTE}
    logger.warning(f"[reset] 시작 mode={mode} keep_queue={keep_queue} "
                   f"reset_seq={reset_seq} flush_unknown={flush_unknown}")

    # 맨 먼저 닫는다 — 지우는 도중에 POST /order 가 끼어들면 반쯤 지워진 상태에 주문이 얹힌다
    report["line"] = {"previous": line.set_status(line.CLOSED), "now": line.CLOSED}

    report["wipe"] = _wipe(keep_queue=keep_queue, hard=hard,
                           reset_seq=reset_seq, flush_unknown=flush_unknown)
    report["seed"] = _seed_zero()

    if reset_seq:
        report["warning"] = ("process:seq 를 리셋했다. 창고·AGV 머신은 'seq 가 안 오르면 무시' 로 "
                             "재전송을 거르므로, 머신도 같이 재시작해야 새 스냅샷을 받는다.")

    if mode == "mobius":
        report["stock"] = _restore_stock()
        report["agv"] = _restore_agv()
        report["process"] = _restore_process()

    # 머신에 새 스냅샷을 알린다. zero 면 '진행 중 주문 없음'이 나간다.
    # process:last 를 지웠으므로 내용이 같아도 반드시 한 번은 나간다.
    # reset=True — open 키 없는 빈 orders 가 머신에게 '리셋'이다 (process._build_body).
    report["pushed"] = push_process(reset=True)

    # 큐를 보존했으면 배정을 다시 시도한다 — 자리가 비어 있을 수 있다.
    if keep_queue:
        from services.dispatch import try_assign_next
        report["assigned"] = try_assign_next()

    report["process_seq"] = r.get("process:seq")
    report["elapsed_sec"] = round(time.time() - t0, 2)
    logger.warning(f"[reset] 완료 {json.dumps(report, ensure_ascii=False)}")
    return report
