"""네트워크 구간 측정 기록. LATENCY_LOG=1 일 때만 동작한다.

logs/latency/YYYY-MM-DD.jsonl 에 한 줄에 한 건씩 쌓는다.
  {"created_at": ..., "part": "mobius_write", "ms": 152.4, "ok": true,
   "trace_id": ..., "pid": 1234, "thread": "MainThread", "note": {"cnt": "cnt_process", "status": 201}}

part: redis_write · redis_read · mobius_write · mobius_read · mobius_subscribe
      callback_handle · http_request · watchdog_expire   요청 1건. 같은 trace_id 의 Redis·Mobius 시간을 포함하므로 합산하지 않는다.
      order_stage · process_push_skipped · system        시각과 횟수만 보는 사건. ms 는 항상 0 이다.

요청 1건 기록의 metrics: redis_ms · mobius_ms · code_ms · cpu_ms · rss_before_mb · rss_after_mb
  cpu_ms 는 Redis·Mobius 호출 중 쓴 CPU 를 포함해 code_ms 와 더하면 안 된다.
  RSS 는 프로세스 전체 값이다. 요청별 사용량이 아니라 증가 추세를 보는 용도다.
system 의 metrics: PC CPU·메모리·스왑, 워커 RSS·사용률, 연결 대기열. LATENCY_SAMPLE_SEC 마다 워커별 1건.

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

# CIN rn 앞 17자리(4-YYYYMMDDHHMMSSmmm)의 시간대. 실제 Mobius 는 UTC 로 찍는다 (2026-11-04 서버 확인).
# 잘못되면 mobius_subscribe 가 약 ±32,400,000ms 로 나온다.
RN_TZ = ZoneInfo(os.getenv("LATENCY_RN_TZ", "UTC"))

SAMPLE_SEC = float(os.getenv("LATENCY_SAMPLE_SEC", "5"))
# 연결 대기열을 볼 gunicorn 포트 (-b 의 포트와 맞춘다)
PORT = int(os.getenv("LATENCY_PORT", "8080"))

# 실제 rn 은 4-20261104101509559008000 처럼 17자리 뒤에 일련번호 6자리가 더 붙는다. 뒤는 버린다.
_RN = re.compile(r"4-(\d{17})\d*")

_trace = contextvars.ContextVar("latency_trace", default=None)
_queue = queue.Queue(maxsize=50000)
_lock = threading.Lock()
_worker = None
_worker_pid = None
_dropped = 0
_busy = {"ms": 0.0, "requests": 0}
_WORKER_PARTS = frozenset({"callback_handle", "http_request"})  # 워커 사용률에 넣는 요청
_sampler_pid = None
_process = None
_process_pid = None
_psutil = None
_psutil_checked = False
_MB = 1024 * 1024

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
def record(part, ms, ok=True, metrics=None, **note):
    """측정 1건을 큐에 넣는다. 어떤 경우에도 서비스 흐름으로 예외를 올리지 않는다."""
    global _dropped
    if not ENABLED:
        return
    try:
        trace = _trace.get()
        entry = {
            "created_at": datetime.now(KST).isoformat(timespec="milliseconds"),
            "part": part,
            "ms": round(ms, 3),
            "ok": ok,
            "trace_id": trace["id"] if trace else None,
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "note": note,
        }
        if metrics:
            entry["metrics"] = metrics
        if RUN_ID:
            entry["run_id"] = RUN_ID
        if trace:
            if part.startswith("redis_"):
                trace["redis_ms"] += ms
            elif part in ("mobius_read", "mobius_write"):
                trace["mobius_ms"] += ms
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
    if not ENABLED:
        return _trace.set(None)
    return _trace.set({
        "id": uuid.uuid4().hex[:8],
        "cpu": time.thread_time(),
        "rss": _rss_mb(),
        "redis_ms": 0.0,
        "mobius_ms": 0.0,
    })


def end_trace(token):
    _trace.reset(token)


def record_request(part, ms, ok=True, **note):
    """요청 1건 기록. 같은 trace 의 호출 시간을 빼서 코드 시간을 남긴다."""
    if not ENABLED:
        return
    metrics = None
    trace = _trace.get()
    if trace:
        metrics = {
            "redis_ms": round(trace["redis_ms"], 3),
            "mobius_ms": round(trace["mobius_ms"], 3),
            "code_ms": round(max(0.0, ms - trace["redis_ms"] - trace["mobius_ms"]), 3),
            "cpu_ms": round((time.thread_time() - trace["cpu"]) * 1000, 3),
            "rss_before_mb": trace["rss"],
            "rss_after_mb": _rss_mb(),
        }
    record(part, ms, ok, metrics=metrics, **note)
    if part in _WORKER_PARTS:
        with _lock:
            _busy["ms"] += ms
            _busy["requests"] += 1


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
            record_request("http_request", (time.perf_counter() - started) * 1000, ok=response.status_code < 400,
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


# ── PC 상태 ─────────────────────────────────────────────────────────
def _load_psutil():
    """psutil 이 없어도 서비스와 시간 측정은 계속 돌아야 한다."""
    global _psutil, _psutil_checked
    if not _psutil_checked:
        _psutil_checked = True
        try:
            import psutil
            _psutil = psutil
        except ImportError:
            logger.warning("[latency] psutil 없음 — CPU·메모리 상태를 기록하지 않는다")
    return _psutil


def _current_process():
    global _process, _process_pid
    psutil = _load_psutil()
    if psutil is None:
        return None
    if _process_pid != os.getpid():
        _process, _process_pid = psutil.Process(), os.getpid()
    return _process


def _rss_mb():
    try:
        process = _current_process()
        return round(process.memory_info().rss / _MB, 2) if process else None
    except Exception:
        return None


def accept_queue(port, paths=("/proc/net/tcp", "/proc/net/tcp6")):
    """리눅스에서 port 의 연결 대기 수. 워커가 받아 가기를 기다리는 요청이다.

    /proc/net/tcp 의 LISTEN(0A) 행은 rx_queue 가 대기 연결 수다. 읽을 수 없으면 None.
    """
    found = None
    for path in paths:
        try:
            with open(path, encoding="ascii") as fp:
                next(fp, None)
                for line in fp:
                    cols = line.split()
                    if len(cols) > 4 and cols[3] == "0A" and int(cols[1].rsplit(":", 1)[1], 16) == port:
                        found = (found or 0) + int(cols[4].split(":")[1], 16)
        except (OSError, ValueError, IndexError):
            continue
    return found


def sample_system(previous=None, now=None):
    """PC 와 이 워커의 현재 상태. (metrics, 다음 호출에 넘길 값)"""
    now = time.perf_counter() if now is None else now
    with _lock:
        busy_ms, requests = _busy["ms"], _busy["requests"]
        _busy["ms"], _busy["requests"] = 0.0, 0

    previous = previous or {}
    elapsed = now - previous["at"] if "at" in previous else None
    metrics = {
        "worker_busy_pct": round(busy_ms / (elapsed * 1000) * 100, 1) if elapsed else None,
        "requests": requests,
        "accept_queue": accept_queue(PORT),
        "load_1m": round(os.getloadavg()[0], 2),
    }

    current = {"at": now}
    psutil = _load_psutil()
    if psutil is not None:
        cpu = psutil.cpu_times_percent(interval=None)
        iowait = getattr(cpu, "iowait", None)           # 리눅스에만 있다
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        process = _current_process()
        current.update(sin=swap.sin, sout=swap.sout)
        metrics.update({
            "cpu_count": psutil.cpu_count(),
            "cpu_pct": round(100 - cpu.idle - (iowait or 0), 1),
            "iowait_pct": iowait,
            "mem_available_mb": round(memory.available / _MB, 1),
            "swap_used_mb": round(swap.used / _MB, 1),
            "swap_in_mb": round((swap.sin - previous["sin"]) / _MB, 2) if "sin" in previous else None,
            "swap_out_mb": round((swap.sout - previous["sout"]) / _MB, 2) if "sout" in previous else None,
            "worker_rss_mb": round(process.memory_info().rss / _MB, 2),
            "worker_cpu_pct": process.cpu_percent(interval=None),
        })
    return metrics, current


def start_sampler():
    """워커마다 1개. create_app 이 부른다 (--preload 면 스레드가 워커로 넘어가지 않는다)."""
    global _sampler_pid
    if not ENABLED or SAMPLE_SEC <= 0 or _sampler_pid == os.getpid():
        return
    _sampler_pid = os.getpid()

    def loop():
        _, previous = sample_system()               # CPU·스왑 기준값. 첫 값은 기록하지 않는다
        while True:
            time.sleep(SAMPLE_SEC)
            try:
                metrics, previous = sample_system(previous)
                record("system", 0, metrics=metrics)
            except Exception:
                logger.exception("[latency] PC 상태 수집 실패")

    threading.Thread(target=loop, name="latency-sampler", daemon=True).start()
