"""네트워크 구간 측정 기록. LATENCY_LOG=1 일 때만 동작한다.

logs/latency/YYYY-MM-DD.jsonl 에 한 줄에 한 건씩 쌓는다.
  {"created_at": ..., "part": "mobius_write", "ms": 152.4, "ok": true,
   "trace_id": ..., "pid": 1234, "thread": "MainThread", "note": {"cnt": "cnt_process", "status": 201}}

part: redis_write · redis_read · mobius_write · mobius_read · mobius_subscribe
      callback_handle · http_request · watchdog_expire   요청 1건. 같은 trace_id 의 Redis·Mobius 시간을 포함하므로 합산하지 않는다.
      order_stage · process_push_skipped                 시각과 횟수만 보는 사건. ms 는 항상 0 이다.

요청 스레드는 큐에 넣기만 하고 파일은 백그라운드 스레드가 쓴다 — 측정이 측정값을 늘리면 안 된다.
조회·집계는 python -m infra.latency_report
"""
import atexit
import contextvars
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import g, request

from config import KST
from infra.logger import logger

ENABLED = os.getenv("LATENCY_LOG") == "1"
RUN_ID = os.getenv("LATENCY_RUN_ID")
LOG_DIR = os.getenv("LATENCY_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "latency")

# CIN rn(4-YYYYMMDDHHMMSSmmm)의 시간대. 잘못되면 mobius_subscribe 가 약 ±32,400,000ms 로 나온다.
RN_TZ = ZoneInfo(os.getenv("LATENCY_RN_TZ", "Asia/Seoul"))

_RN = re.compile(r"4-(\d{17})")

_trace = contextvars.ContextVar("latency_trace", default=None)
_queue = queue.Queue(maxsize=50000)
_lock = threading.Lock()
_worker = None
_worker_pid = None
_dropped = 0

READ_COMMANDS = frozenset({
    "GET", "MGET", "EXISTS", "TTL", "PTTL", "TYPE", "SCAN",
    "HGET", "HGETALL", "HMGET", "HKEYS", "HVALS", "HLEN", "HEXISTS",
    "LRANGE", "LLEN", "LINDEX",
    "ZSCORE", "ZRANGE", "ZRANGEBYSCORE", "ZCARD", "ZCOUNT", "ZRANK",
    "SMEMBERS", "SCARD", "SISMEMBER",
})
_KEYLESS = frozenset({"SCAN", "PING", "INFO", "DBSIZE", "SELECT"})
_HASH_FIELD_ARGS = frozenset({"HGET", "HMGET", "HDEL", "HINCRBY", "HEXISTS"})


# ── 기록 ────────────────────────────────────────────────────────────
def record(part, ms, ok=True, **note):
    """측정 1건을 큐에 넣는다. 어떤 경우에도 서비스 흐름으로 예외를 올리지 않는다."""
    global _dropped
    if not ENABLED:
        return
    try:
        entry = {
            "created_at": datetime.now(KST).isoformat(timespec="milliseconds"),
            "part": part,
            "ms": round(ms, 3),
            "ok": ok,
            "trace_id": _trace.get(),
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "note": note,
        }
        if RUN_ID:
            entry["run_id"] = RUN_ID
        _ensure_worker()
        _queue.put_nowait(entry)
    except queue.Full:
        _dropped += 1
    except Exception:
        logger.exception("[latency] 기록 실패")


def flush(timeout=5):
    """큐가 빌 때까지 기다린다. 프로세스 종료와 테스트에서 쓴다."""
    if _worker is None or _worker_pid != os.getpid():
        return
    done = threading.Event()
    def wait():
        _queue.join()
        done.set()
    threading.Thread(target=wait, daemon=True).start()
    done.wait(timeout)


def _ensure_worker():
    global _worker, _worker_pid, _queue
    if _worker is not None and _worker_pid == os.getpid() and _worker.is_alive():
        return
    with _lock:
        if _worker is not None and _worker_pid == os.getpid() and _worker.is_alive():
            return
        if _worker_pid != os.getpid():
            _queue = queue.Queue(maxsize=50000)     # fork 뒤에는 부모의 큐를 물려 쓰지 않는다
        _worker_pid = os.getpid()
        _worker = threading.Thread(target=_write_loop, name="latency-writer", daemon=True)
        _worker.start()


def _write_loop():
    global _dropped
    current_path, fp = None, None
    while True:
        entry = _queue.get()
        try:
            path = os.path.join(LOG_DIR, f'{entry["created_at"][:10]}.jsonl')
            if path != current_path:
                if fp:
                    fp.close()
                os.makedirs(LOG_DIR, exist_ok=True)
                # 줄 단위 버퍼 — 워커 여럿이 같은 파일에 덧붙여도 한 줄이 쪼개지지 않는다
                fp = open(path, "a", encoding="utf-8", buffering=1)
                current_path = path
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if _dropped:
                logger.warning(f"[latency] 큐 초과로 {_dropped}건 버림")
                _dropped = 0
        except Exception:
            logger.exception("[latency] 파일 쓰기 실패")
            current_path, fp = None, None
        finally:
            _queue.task_done()


atexit.register(flush)


# ── trace ───────────────────────────────────────────────────────────
def begin_trace():
    return _trace.set(uuid.uuid4().hex[:8])


def end_trace(token):
    _trace.reset(token)


def install_request_trace(app):
    """콜백 외 HTTP 요청에도 trace 를 붙인다. 콜백은 routes/callback.py 가 따로 잰다."""
    if not ENABLED:
        return

    def is_callback():
        return request.path.startswith("/mobius/callback/")

    @app.before_request
    def begin():
        if not is_callback():
            g.latency_started = time.perf_counter()
            g.latency_token = begin_trace()

    @app.after_request
    def finish(response):
        started = g.pop("latency_started", None)
        if started is not None:
            rule = request.url_rule.rule if request.url_rule else "(unmatched)"
            record("http_request", (time.perf_counter() - started) * 1000, ok=response.status_code < 400,
                   route=f"{request.method} {rule}", status=response.status_code)
        return response

    @app.teardown_request
    def end(_exc):
        token = g.pop("latency_token", None)
        if token is not None:
            end_trace(token)


# ── Redis ───────────────────────────────────────────────────────────
def normalize_key(key):
    """주문·날짜·세션마다 달라지는 부분을 * 로 묶는다. (묶인 key, order_id)

    auth:session 과 idempotency 는 키 자체가 토큰이라 원문을 파일에 남기지 않는다.
    """
    key = str(key)
    if key.startswith("auth:session:"):
        return "auth:session:*", None
    if key.startswith("idempotency:"):
        return "idempotency:*", None
    order = re.search(r"\bord_[^:]+", key)
    order_id = order.group(0) if order else None
    if order_id:
        key = key.replace(order_id, "*")
    return re.sub(r"(?<=:)\d{8}(?=$|:)", "*", key), order_id


def is_read(command):
    return str(command).upper() in READ_COMMANDS


def _command_note(args):
    command = str(args[0]).upper() if args else "?"
    note = {"command": command}
    if len(args) < 2 or command in _KEYLESS:
        return note
    note["key"], order_id = normalize_key(args[1])
    if order_id:
        note["order_id"] = order_id
    if command == "HSET":
        note["field"] = ",".join(str(f) for f in args[2::2])
    elif command in _HASH_FIELD_ARGS:
        fields = args[2:3] if command == "HINCRBY" else args[2:]
        note["field"] = ",".join(str(f) for f in fields)
    return note


def record_redis(args, ms, ok):
    if not ENABLED:
        return
    part = "redis_read" if args and is_read(args[0]) else "redis_write"
    record(part, ms, ok, **_command_note(args))


def record_redis_pipeline(commands, ms, ok):
    """파이프라인은 왕복 1회라 명령이 여러 개여도 1건으로 남긴다."""
    if not ENABLED or not commands:
        return
    notes = [_command_note(args) for args, _ in commands]
    part = "redis_read" if all(is_read(n["command"]) for n in notes) else "redis_write"
    keys = list(dict.fromkeys(n["key"] for n in notes if "key" in n))
    record(part, ms, ok, command="PIPELINE", commands=[n["command"] for n in notes], key=",".join(keys))


# ── Mobius 구독 ─────────────────────────────────────────────────────
def record_subscribe(cnt, rn, received_at):
    """CIN 생성 → 콜백 도착. received_at 은 time.time() 값.

    Mobius 와 이 PC 의 시계 차이가 그대로 들어가므로 음수도 버리지 않는다.
    나중에 다시 계산할 수 있게 rn 원문을 함께 남긴다.
    """
    if not ENABLED or not isinstance(rn, str):
        return
    matched = _RN.fullmatch(rn)
    if not matched:
        return
    created = datetime.strptime(matched.group(1), "%Y%m%d%H%M%S%f").replace(tzinfo=RN_TZ)
    record("mobius_subscribe", (received_at - created.timestamp()) * 1000, cnt=cnt, cin_rn=rn)
