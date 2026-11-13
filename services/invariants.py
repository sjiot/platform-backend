"""불변식 — '무슨 일이 있어도 이래야 한다'.

시나리오 테스트는 **내가 생각한 경우**만 잡는다. 불변식은 생각 못 한 경우를 잡는다.
상태를 건드린 뒤 assert_ok(r) 를 부르면 전체 Redis 를 훑어 깨진 것을 모아 준다.

두 곳에서 쓴다 (그래서 tests/ 가 아니라 services/ 에 있다).
    테스트            assert_ok(rds) · Monitor — fake Redis
    디버그 스냅샷     check(r) — **실 Redis**. GET /admin/debug/snapshot 의 violations (services/snapshot.py)
실 Redis 에서도 돌므로 읽기만 하고, KEYS 대신 SCAN 을 쓴다.

새 불변식을 추가할 때는 "이게 깨지면 라인이 어떻게 되는가"를 주석으로 남길 것.
근거 없는 불변식은 리팩터링을 막기만 한다.
"""
from config import (AGV_KEY, DEADLINE_KEY, STAGE_ORDER, STATION_KEY,
                    STATION_ORDER_PREFIX, WAREHOUSE_KEY, WAREHOUSE_ORDER_KEY)
from services import collector, stock, watchdog
from services.process import rank

TERMINAL = {"completed", "unclaimed"}
STATIONS = ("1", "2", "3")


def _order_ids(r):
    # order:ord_xxxx 만. 같은 패턴에 걸리는 order:ord_xxxx:keycap:pending 등은 콜론 수로 거른다
    # (주문 id 는 uuid hex 라 콜론이 없다). 실 Redis 에서 KEYS 는 서버를 멈추므로 SCAN.
    return [k.removeprefix("order:") for k in r.scan_iter("order:ord_*", count=500)
            if k.count(":") == 1]


def check(r):
    """깨진 불변식 목록. 빈 리스트면 정상."""
    bad = []

    def fail(msg):
        bad.append(msg)

    # ── 주문 단위 ───────────────────────────────────────────
    for oid in _order_ids(r):
        o = r.hgetall(f"order:{oid}")
        stage, fault = o.get("stage"), o.get("fault")

        # 1. 모르는 stage 는 프론트·머신 매핑을 통째로 깬다
        if stage not in STAGE_ORDER:
            fail(f"{oid} 알 수 없는 stage={stage!r}")

        # 2. 모든 fault 에는 section 이 붙는다 (fault_정리 5장). 없으면 프론트가 어디를
        #    칠할지 모른다. set_fault 가 section=None 이면 hdel 하므로 둘은 한 묶음이다
        if fault and not o.get("fault_section"):
            fail(f"{oid} fault={fault} 인데 fault_section 이 없다")

        # 3. pending 은 기대 목록의 부분집합이다. 없던 칸이 생기면 재출고 명령이
        #    엉뚱한 부품을 부른다
        expected = list(stock.parts_of(oid, "keycap"))
        pend = collector.remaining(oid, "keycap")
        leftover = list(expected)
        for s in pend:
            if s in leftover:
                leftover.remove(s)
            else:
                fail(f"{oid} pending 에 기대에 없는 칸 {s} (기대 {expected})")

        # 4. fault 는 시계를 멈춘다 (set_fault 가 disarm). 남아 있으면 조치 중에
        #    stage_timeout 이 덧씌워진다
        if fault and r.zscore(DEADLINE_KEY, oid) is not None:
            fail(f"{oid} fault={fault} 인데 마감이 살아 있다")

        # 5. 반대로 진행 중인데 마감이 없으면 머신이 죽어도 아무도 모른다
        if not fault and stage in watchdog.STAGE_TIMEOUT and r.zscore(DEADLINE_KEY, oid) is None:
            fail(f"{oid} stage={stage} 인데 마감이 없다")

        # 6. 끝난 주문이 collector 를 물고 있으면 다음 주문 판정을 오염시킨다
        if stage in TERMINAL and (pend or collector.wrong(oid, "keycap")):
            fail(f"{oid} 종료({stage})인데 collector 가 남아 있다")

    # ── 창고 ───────────────────────────────────────────────
    cur = r.get(WAREHOUSE_ORDER_KEY)
    occ = r.get(WAREHOUSE_KEY)
    # 7. 둘이 어긋나면 배정 게이트(dispatch.try_assign_next)가 영구 교착되거나
    #    반대로 트레이가 벨트에 있는데 다음 주문이 들어온다
    if cur and occ != "busy":
        fail(f"warehouse:current_order={cur} 인데 occupancy={occ!r}")
    if not cur and occ == "busy":
        fail("warehouse:occupancy=busy 인데 current_order 가 없다")
    if cur and not r.exists(f"order:{cur}"):
        fail(f"warehouse:current_order={cur} 인데 그 주문 해시가 없다")

    # 7-1. 픽업대 트레이는 1개뿐이다. 둘이면 배정 게이트(dispatch.pickup_occupied)가
    #      뚫린 것이고, AGV 가 어느 트레이를 실을지 모른다 (callbacks/agv.py 대상 모호)
    at_pickup = [r.get(f"{STATION_ORDER_PREFIX}{sid}") for sid in STATIONS]
    at_pickup = [oid for oid in at_pickup
                 if oid and r.hget(f"order:{oid}", "stage") == "pickup_reached"]
    if len(at_pickup) > 1:
        fail(f"픽업대에 주문이 {len(at_pickup)}건 — {at_pickup}")

    # 8. 선반 차단 키에 모르는 값이 들어가면 blockers 가 영원히 못 푼다
    for shelf in (r.hkeys(stock.DRIVER_KEY) or []):
        if shelf not in stock.SHELVES:
            fail(f"driver_failed 에 모르는 선반 {shelf!r}")

    # ── 조립대 ─────────────────────────────────────────────
    for sid in STATIONS:
        oid = r.get(f"{STATION_ORDER_PREFIX}{sid}")
        busy = r.hget(STATION_KEY, sid)
        # 9. 어긋나면 완료 직후 늦게 온 요청이 다음 주문의 배정을 지운다 (ERD 6-6)
        if oid and busy != "busy":
            fail(f"station:current_order:{sid}={oid} 인데 occupancy={busy!r}")
        if oid and r.hget(f"order:{oid}", "station_id") != sid:
            fail(f"{oid} 의 station_id 가 {sid} 와 다르다")

    # 10. 재고 status 는 정해진 값만
    for slot, v in stock.all_states().items():
        if v.get("status") not in ("idle", "busy", "empty", "disable"):
            fail(f"{slot} status={v.get('status')!r}")

    return bad


def assert_ok(r, where=""):
    bad = check(r)
    assert not bad, ("불변식 위반" + (f" ({where})" if where else "") + ":\n  - "
                     + "\n  - ".join(bad))


class Monitor:
    """시간축 불변식 — 한 시점만 봐서는 못 잡는 것들.

    step() 을 단계마다 부르면 직전 상태와 비교한다.
    """

    def __init__(self, r, order_id):
        self.r, self.oid = r, order_id
        self.prev = self._now()
        self.violations = []

    def _now(self):
        o = self.r.hgetall(f"order:{self.oid}")
        return {"stage": o.get("stage"), "fault": o.get("fault")}

    def step(self, what=""):
        cur = self._now()
        # 1. stage 는 뒤로 가지 않는다 (mismatched 는 rank 가 같아서 통과한다)
        if rank(cur["stage"]) < rank(self.prev["stage"]):
            self.violations.append(
                f"{what}: stage 역행 {self.prev['stage']} → {cur['stage']}")
        # 2. fault 가 걸려 있는 동안 stage 는 움직이지 않는다.
        #    움직이면 '멈춰 세운 주문'이 보고 하나에 다음 단계로 새어 나간다
        if self.prev["fault"] and cur["fault"] and cur["stage"] != self.prev["stage"]:
            self.violations.append(
                f"{what}: fault 중 stage 이동 {self.prev['stage']} → {cur['stage']}")
        self.prev = cur
        assert_ok(self.r, what)
        return cur

    def assert_ok(self):
        assert not self.violations, "시간축 불변식 위반:\n  - " + "\n  - ".join(self.violations)
