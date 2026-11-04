import os
import json
import time
import requests
from datetime import datetime
from infra.logger import logger
from infra import latency

from infra.extensions import r

from config import (STATION_ORDER_PREFIX,
                    KST,
                    MP_URL, 
                    CB,
                    AE_RN,
                    ORIGIN,
                    API_KEY,
                    LECTURE_ID,
                    CREATOR_ID,
                    STAGE_ORDER
                   )

def _headers(ty=None):
    h = {
        "X-API-KEY": API_KEY,
        "X-AUTH-CUSTOM-LECTURE": LECTURE_ID,
        "X-AUTH-CUSTOM-CREATOR": CREATOR_ID,
        "Accept": "application/json",
        "X-M2M-RI": os.urandom(4).hex(),
        "X-M2M-Origin": ORIGIN,
    }
    if ty:
        h["Content-Type"] = f"application/json;ty={ty}"
    return h

def _timed_request(part, cnt, method, url, **kwargs):
    """Mobius 요청 1건을 측정한다. 실패도 남긴다 — 타임아웃이 빠지면 가장 느린 값이 사라진다."""
    started = time.perf_counter()
    try:
        res = requests.request(method, url, **kwargs)
    except requests.RequestException as e:
        latency.record(part, (time.perf_counter() - started) * 1000, ok=False,
                       cnt=cnt, error=type(e).__name__)
        raise
    latency.record(part, (time.perf_counter() - started) * 1000, ok=res.status_code in (200, 201),
                   cnt=cnt, status=res.status_code, sent_bytes=len(res.request.body or b""))
    return res

def create_cin(cnt_rn, con_dict):
    url = f"{MP_URL}/{CB}/{AE_RN}/{cnt_rn}"
    body = {"m2m:cin": {"con": json.dumps(con_dict, ensure_ascii=False)}}
    try:
        res = _timed_request("mobius_write", cnt_rn, "POST", url, headers=_headers(ty=4), json=body, timeout=(3, 5))
    except requests.RequestException as e:
        logger.error(f"[create_cin] {cnt_rn} 요청 실패: {e}")
        return False        # ← 예외를 여기서 흡수해야 _dispatch_order 롤백이 돌아감

    ok = res.status_code in (200, 201)
    if not ok:
        logger.error(f"[create_cin] {cnt_rn} {res.status_code} {res.text[:200]}")
    return ok

def get_latest_con(cnt_rn):
    """cnt의 최신 CIN con을 dict로 반환. 실패하면 None."""
    url = f"{MP_URL}/{CB}/{AE_RN}/{cnt_rn}/la"

    try:
        res = _timed_request("mobius_read", cnt_rn, "GET", url, headers=_headers(), timeout=(3, 5))
    except requests.RequestException as e:
        logger.error(f"[get_latest_con] {cnt_rn} 요청 실패: {e}")
        return None

    if res.status_code != 200:
        logger.error(f"[get_latest_con] {cnt_rn} {res.status_code} {res.text[:200]}")
        return None

    try:
        con = res.json()["m2m:cin"]["con"]
    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"[get_latest_con] {cnt_rn} 응답 구조 이상: {e} body={res.text[:200]}")
        return None

    if isinstance(con, str):
        try:
            return json.loads(con)
        except json.JSONDecodeError:
            logger.error(f"[get_latest_con] {cnt_rn} con 파싱 실패: {con[:200]}")
            return None
    return con

def get_recent_cons(cnt_rn, limit=8):
    """컨테이너의 최근 CIN limit 건을 con(dict) 목록으로. 최신이 앞이다.

    부팅 복구가 '이 주문의 키캡이 몇 개나 배출됐나'를 세는 데 쓴다.
    /la 는 1건뿐이라 4건 중 몇 건이 나왔는지 알 수 없다.

    [ 실측으로 정한 경로 — debug/get_cin_test.py ]
    · rcn=4 / rcn=6 / rcn=8 은 이 플랫폼에서 CIN 을 실어 주지 않는다 (200 이지만 0건)
    · discovery(fu=1&ty=4)만 동작하고, URI 목록을 준다
    · 반환 순서는 **최신 → 과거 (내림차순)** — rn 이 `4-YYYYMMDDHHMMSSmmm` 이라 확정
    · **lim 을 쓰면 안 된다.** 이 플랫폼의 lim 은 '최신 N건'이 아니라
      '가장 오래된 N건'을 준다 (lim=16 과 lim 없음의 꼬리 3건이 동일했다).
      그래서 전량을 받고 앞에서 자른다. mni 가 50이라 목록 자체는 작다.
    · 개별 GET 은 건당 0.11초. 8건에 0.9초 — 복구는 재기동 때 한 번이라 감당된다.
    """
    dis = _request("GET", cnt_rn, params={"fu": 1, "ty": 4}, what="discovery")
    if dis is None:
        return []
    uris = dis.get("m2m:uril") or dis.get("uril") or (dis if isinstance(dis, list) else [])
    if not isinstance(uris, list):
        return []

    out = []
    for uri in uris[:limit]:                     # 내림차순이라 앞이 최신
        body = _request("GET", None, url=f"{MP_URL}/{str(uri).lstrip('/')}", what="cin")
        if not body:
            continue
        try:
            con = body["m2m:cin"]["con"]
        except (KeyError, TypeError):
            continue
        if isinstance(con, str):
            try:
                con = json.loads(con)
            except json.JSONDecodeError:
                continue
        if isinstance(con, dict):
            out.append(con)
    return out


def _request(method, cnt_rn, url=None, params=None, what=""):
    """GET 한 번. 실패는 None 으로 접는다 — 복구가 조회 실패로 죽으면 안 된다."""
    target = url or f"{MP_URL}/{CB}/{AE_RN}/{cnt_rn}"
    try:
        res = _timed_request("mobius_read", cnt_rn or what, method, target, headers=_headers(),
                             params=params, timeout=(3, 8))
    except requests.RequestException as e:
        logger.error(f"[mobius] {what} {cnt_rn or target} 요청 실패: {e}")
        return None
    if res.status_code != 200:
        logger.error(f"[mobius] {what} {cnt_rn or target} {res.status_code} {res.text[:160]}")
        return None
    try:
        return res.json()
    except ValueError:
        logger.error(f"[mobius] {what} {cnt_rn or target} JSON 아님: {res.text[:160]}")
        return None


# order
def send_order_cin(order_id, board, keycap, colors, station_id, order_seq):
    con = {
        "order_id": order_id,
        "station_id": station_id,
        "board": board,
        "colors": colors,
        "keycap": keycap,
        "order_seq": order_seq,
    }
    return create_cin("cnt_order", con)

def _dispatch_order(order_id):
    """배정된 주문을 창고에 불출 지시(cnt_order append). 성공 여부만 반환."""
    od = r.hgetall(f"order:{order_id}")
    required = ("board", "keycap", "colors", "station_id", "order_seq")
    if not od or any(k not in od for k in required):
        logger.error(f"[dispatch] order 데이터 불완전 (order_id={order_id}) od={od}")
        return False

    return send_order_cin(
        order_id,
        od["board"],
        od["keycap"],
        od["colors"].split(","),
        str(od["station_id"]),
        int(od["order_seq"]),
    )

# stock 캐시는 services/stock.py 로 이관 (warehouse:stock 은 hash)

# cnt_station — 기록용. 구독자 0. 확정본 형태는 래퍼 없이 조립대 번호가 최상위다.
#   {"1": {"order_id": "ord_xxxx", "order_seq": "109"}, "2": {...}, "3": {...}}
def send_station_cin(stations):
    return create_cin("cnt_station", stations)

def _build_station_snapshot():
    stations = {}
    for sid in ("1", "2", "3"):
        order_id = r.get(f"{STATION_ORDER_PREFIX}{sid}")
        od = r.hgetall(f"order:{order_id}") if order_id else None
        if not od:
            stations[sid] = {"order_id": None, "order_seq": None}
            continue
        stations[sid] = {"order_id": order_id, "order_seq": od.get("order_seq")}
    return stations

def _push_station_snapshot():
    """redis 기준 스냅샷을 cnt_station에 기록. 실패해도 로직은 계속 (명령이 아니다)."""
    ok = send_station_cin(_build_station_snapshot())
    if not ok:
        logger.warning("[station] cnt_station 기록 실패")
    return ok

# 명령은 cnt_process 하나로만 나간다 (services/process.push_process)

# supscription
def _extract_notification_con(data):
    """Mobius 알림 봉투에서 con을 dict로 꺼낸다. 못 꺼내면 None."""
    if not isinstance(data, dict):
        return None

    sgn = data.get("m2m:sgn")
    if sgn is None:
        return data or None

    con = (((sgn.get("nev") or {}).get("rep") or {})
           .get("m2m:cin") or {}).get("con")

    # dict가 될 때까지 푼다 (이중 인코딩 대비 최대 2회)
    for _ in range(2):
        if not isinstance(con, str):
            break
        try:
            con = json.loads(con)
        except json.JSONDecodeError:
            logger.warning(f"[callback] con 파싱 실패: {con[:200]}")
            return None

    if not isinstance(con, dict):
        logger.warning(f"[callback] con이 dict가 아님: {type(con).__name__} {str(con)[:200]}")
        return None

    # 있을 때만 정규화
    if "count" in con:
        try:
            con["count"] = int(con["count"])
        except (TypeError, ValueError):
            logger.warning(f"[callback] count 이상: {con['count']!r}")
            con["count"] = 0

    return con
