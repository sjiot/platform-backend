"""디버깅 스냅샷 — Redis 에 지금 무엇이 있고, 그게 서로 맞는 상태인지를 JSON 하나로.

redis-cli 로 키를 하나씩 열어보고 Mobius 사이트에서 cnt_process 를 확인하던 걸 대신한다.

    GET /admin/debug/snapshot            Redis 만
    GET /admin/debug/snapshot?mobius=1   + Mobius cnt_process·cnt_agv 최신값과 대조 (GET 2건)

로컬에서 단계마다 저장하고 직전과 비교하려면 debug/snapshot.py 를 쓴다.

**읽기 전용이다.** station_service.call_state() 처럼 읽으면서 정리(삭제)하는 함수는 부르지 않는다.
스냅샷을 뜨는 행위가 상태를 바꾸면 디버깅 도구로 쓸 수 없다.

[ 읽는 법 ]
  violations   services/invariants.check() 결과. 빈 리스트면 Redis 상태끼리 서로 맞다.
               리셋 직후엔 이것만 보면 된다.
  mobius       Redis 가 마지막으로 보낸 스냅샷(process:last)과 머신이 보는 cnt_process 최신 CIN 비교.
               warning 이 있으면 둘이 다르다 — 머신은 Redis 와 다른 걸 보고 있다.
  ok           violations 가 없고 mobius.warning 도 없을 때 true.

[ 비용 ]
  주문 수에 비례해 Redis 왕복이 늘어난다 (주문 1건당 10번 남짓). gunicorn sync 워커 1개라 이 요청이
  도는 동안 콜백이 줄을 선다. 운영 중에 연타하지 말고, watch 는 2초 이상 간격으로.
"""
import json
import time
from datetime import datetime

from config import (AGV_COORD_KEY,
                    AGV_KEY,
                    AGV_STATUS_KEY,
                    DEADLINE_KEY,
                    KST,
                    QUEUE_KEY,
                    STATION_CALL_PREFIX,
                    STATION_KEY,
                    STATION_ORDER_PREFIX,
                    STATION_VERIFIED_PREFIX,
                    WAREHOUSE_DRIVER_KEY,
                    WAREHOUSE_KEY,
                    WAREHOUSE_ORDER_KEY,
                   )
from infra.extensions import r
from services import collector, invariants, line, stock

STATIONS = ("1", "2", "3")
COLLECTOR_SLOTS = ("keycap", "board", "keycap_pack")


def _json(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _iso(ts):
    return datetime.fromtimestamp(ts, KST).isoformat(timespec="seconds")


def _order_ids():
    # invariants._order_ids 와 같은 규칙 — order:ord_xxxx 만 (콜론 1개)
    return [k.removeprefix("order:") for k in r.scan_iter("order:ord_*", count=500)
            if k.count(":") == 1]


def _stations():
    occ = r.hgetall(STATION_KEY)
    out = {}
    for sid in STATIONS:
        out[sid] = {
            "occupancy": occ.get(sid),
            "current_order": r.get(f"{STATION_ORDER_PREFIX}{sid}"),
            "verified_at": r.get(f"{STATION_VERIFIED_PREFIX}{sid}"),
            # call_state() 는 지난 호출을 지우므로 쓰지 않는다. 원본 해시를 그대로 보인다
            "call": r.hgetall(f"{STATION_CALL_PREFIX}{sid}") or None,
        }
    return out


def _orders(now, live):
    """order:ord_* 전부. 끝난 주문도 TTL(10분) 동안 남아 있으므로 같이 보인다 — live 로 구분."""
    out = {}
    for oid in _order_ids():
        o = r.hgetall(f"order:{oid}")
        if not o:
            continue                                  # SCAN 과 HGETALL 사이에 만료됐다
        entry = dict(o)
        entry["live"] = oid in live

        col = {}
        for slot in COLLECTOR_SLOTS:
            pend, wrong = collector.remaining(oid, slot), collector.wrong(oid, slot)
            if pend or wrong:
                col[slot] = {"pending": pend, "wrong": wrong}
        if col:
            entry["collector"] = col

        gate = r.hgetall(f"order:{oid}:gate")
        if gate:
            entry["gate"] = sorted(gate)

        score = r.zscore(DEADLINE_KEY, oid)
        if score is not None:
            entry["deadline"] = {"at": _iso(score), "left_sec": round(score - now)}
        out[oid] = entry
    return out


def _orphan_deadlines(orders):
    """주문 해시는 없는데 마감만 남은 것. 워치독이 깨어나 없는 주문을 처리하려 든다."""
    return sorted(m for m in r.zrange(DEADLINE_KEY, 0, -1) if m not in orders)


def _stock():
    states = stock.all_states()
    by_status = {}
    for v in states.values():
        st = v.get("status")
        by_status[st] = by_status.get(st, 0) + 1
    return {
        "by_status": by_status,
        # 칸 하나가 한 줄 — 직전 스냅샷과 비교할 때 배출로 줄어든 칸만 바로 보인다
        "slots": {slot: f"{v.get('count')}/{v.get('status')}" for slot, v in sorted(states.items())},
    }


def _process():
    seq = r.get("process:seq")
    return {"seq": int(seq) if seq and seq.isdigit() else seq,
            "last": _json(r.get("process:last"))}


def _mobius(process):
    """머신이 실제로 보는 것과 Redis 가 보냈다고 믿는 것을 맞춰 본다."""
    from infra.mobius import get_latest_con

    out = {}
    con = get_latest_con("cnt_process")
    if con is None:
        out["cnt_process"] = None
        out["warning"] = "cnt_process 조회 실패 — Mobius 접속·인증을 확인"
    else:
        body = {k: v for k, v in con.items() if k not in ("seq", "updated_at")}
        out["cnt_process"] = {
            "seq": con.get("seq"),
            "updated_at": con.get("updated_at"),
            "orders": {oid: {"status": o.get("status"), "fault": o.get("fault")}
                       for oid, o in (con.get("orders") or {}).items()
                       if isinstance(o, dict)},          # {"open": true} 는 주문이 아니다
        }
        rseq, mseq = process["seq"], con.get("seq")
        out["seq_match"] = rseq == mseq
        out["content_match"] = (body == process["last"]) if process["last"] is not None else None

        if isinstance(rseq, int) and isinstance(mseq, int) and rseq > mseq:
            out["warning"] = ("Redis seq 가 Mobius 보다 앞선다 — 마지막 push(create_cin)가 실패했을 수 있다. "
                              "머신은 아직 옛 스냅샷을 보고 있다")
        elif isinstance(rseq, int) and isinstance(mseq, int) and rseq < mseq:
            out["warning"] = ("Mobius seq 가 Redis 보다 앞선다 — 다른 백엔드(로컬·배포)가 같은 cnt_process 에 "
                              "쓰고 있거나 Redis 가 리셋 전 값이다")
        elif out["content_match"] is False:
            out["warning"] = "seq 는 같은데 내용이 다르다 — 같은 seq 로 다른 내용이 올라갔다"

    out["cnt_agv"] = get_latest_con("cnt_agv")
    return out


def build(include_mobius=False):
    t0 = time.time()
    queue = r.lrange(QUEUE_KEY, 0, -1)
    stations = _stations()
    wh_order = r.get(WAREHOUSE_ORDER_KEY)
    live = set(queue) | {s["current_order"] for s in stations.values() if s["current_order"]}
    if wh_order:
        live.add(wh_order)

    orders = _orders(t0, live)
    process = _process()
    snap = {
        "at": datetime.now(KST).isoformat(timespec="seconds"),
        "line": line.summary(),
        "queue": queue,
        "warehouse": {
            "occupancy": r.get(WAREHOUSE_KEY),
            "current_order": wh_order,
            "driver_failed": r.hgetall(WAREHOUSE_DRIVER_KEY) or None,
        },
        "stations": stations,
        "agv": {
            "occupancy": r.get(AGV_KEY),
            "status": r.get(AGV_STATUS_KEY),
            "coord": _json(r.get(AGV_COORD_KEY)),
        },
        "orders": orders,
        "orphan_deadlines": _orphan_deadlines(orders),
        "process": process,
        "stock": _stock(),
        "violations": invariants.check(r),
    }
    if include_mobius:
        snap["mobius"] = _mobius(process)

    snap["ok"] = not snap["violations"] and not (include_mobius and snap["mobius"].get("warning"))
    snap["elapsed_ms"] = round((time.time() - t0) * 1000)
    return snap
