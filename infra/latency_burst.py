"""워커 수별 동시 콜백 처리 비교. 실제 Mobius·장비에는 닿지 않는다.

가짜 Mobius 와 전용 Redis DB 를 강제로 지정해 gunicorn 을 직접 띄운다.

  python -m infra.latency_burst --workers 1 --concurrency 20 --requests 100 --cin-ms 150
  python -m infra.latency_burst --workers 2 --concurrency 20 --requests 100 --cin-ms 150

--cin-ms 에는 실제로 잰 mobius_write p50 을 넣는다.
대기 추정 = 클라이언트 응답 시간 - callback_handle. 워커가 모자라면 이 값이 커진다.

기록은 logs/latency/burst/<시각>-w<워커>/ 에 남는다 (--out 으로 변경).
  YYYY-MM-DD.jsonl  요청별 기록과 system 샘플 (run_id = burst-w<워커>)
  summary.json      화면에 찍는 요약과 실행 조건
"""
import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOTS = [f"{letter}_{color}" for letter in "EISNTFJP" for color in "rygb"]
AE = "ae_burst"


def stub_mobius(cin_ms):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(cin_ms / 1000)
            self._reply(201)

        def do_GET(self):
            self._reply(200)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def pct(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def line(name, values):
    print(f"{name:<22} n={len(values):<5} p50={statistics.median(values):8.1f}  "
          f"p95={pct(values, 0.95):8.1f}  max={max(values):8.1f} ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--requests", type=int, default=100)
    ap.add_argument("--cin-ms", type=float, default=150)
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--redis", default="redis://127.0.0.1:6379/15")
    ap.add_argument("--threads", type=int, default=0, help="gunicorn --threads. 0 이면 넘기지 않는다")
    ap.add_argument("--out", help="기록 폴더. 기본 logs/latency/burst/<시각>-w<워커>")
    a = ap.parse_args()

    if a.redis.rstrip("/").rsplit("/", 1)[-1] in ("0", a.redis):
        sys.exit("--redis 에 전용 DB 번호를 지정한다 (0 번 불가)")
    rds = redis.from_url(a.redis, decode_responses=True)
    if rds.dbsize():
        sys.exit(f"{a.redis} 가 비어 있지 않다. 이 DB 는 끝나면 비워지므로 빈 DB 만 받는다")

    stub = stub_mobius(a.cin_ms)
    run_id = f"burst-w{a.workers}" + (f"-t{a.threads}" if a.threads else "")
    log_dir = a.out or os.path.join(
        ROOT, "logs", "latency", "burst",
        f"{datetime.datetime.now():%Y%m%d-%H%M%S}-w{a.workers}" + (f"-t{a.threads}" if a.threads else ""))
    os.makedirs(log_dir, exist_ok=True)
    if any(n.endswith(".jsonl") for n in os.listdir(log_dir)):
        sys.exit(f"{log_dir} 에 기록이 이미 있다. 빈 폴더를 지정한다")
    env = {**os.environ,
           "MP_URL": f"http://127.0.0.1:{stub.server_port}", "AE_RN": AE, "REDIS_URL": a.redis,
           "LATENCY_LOG": "1", "LATENCY_DIR": log_dir, "LATENCY_RUN_ID": run_id,
           "LATENCY_SAMPLE_SEC": "1", "LATENCY_PORT": str(a.port)}
    cmd = [sys.executable, "-m", "gunicorn", "-w", str(a.workers)]
    if a.threads:
        cmd += ["--threads", str(a.threads)]
    cmd += ["-b", f"127.0.0.1:{a.port}", "app:create_app()"]
    server = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    base = f"http://127.0.0.1:{a.port}"
    try:
        for _ in range(100):
            try:
                requests.get(f"{base}/mobius/callback/_probe", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)
        else:
            sys.exit("gunicorn 이 뜨지 않았다")
        time.sleep(1)                       # 부팅 재고 시드 대기

        # restock 목록은 관리자 채움(warehouse:restock)으로만 바뀌고, 창고의 count>0 보고가 지운다.
        # 모든 칸을 '채운 상태'로 두고 count 40(지움)·0(되돌림) 을 번갈아 보내
        # 요청마다 목록이 바뀌게 한다 → 요청마다 cnt_process push
        rds.sadd("warehouse:restock", *SLOTS)
        started_at = time.time()

        def send(i):
            slot = SLOTS[i % len(SLOTS)]
            count = 40 if (i // len(SLOTS)) % 2 == 0 else 0
            if count == 0:
                rds.sadd("warehouse:restock", slot)
            path = f"/mobius/callback/cnt_warehouse/cnt_keycap_status/cnt_{slot}"
            t0 = time.perf_counter()
            res = requests.post(base + path, timeout=300, json={"m2m:sgn": {"nev": {"rep": {"m2m:cin": {
                "con": {"count": count, "status": "idle"}}}}}})
            return (time.perf_counter() - t0) * 1000, res.status_code

        with ThreadPoolExecutor(a.concurrency) as pool:
            results = list(pool.map(send, range(a.requests)))
        elapsed = time.time() - started_at
        time.sleep(1)                       # 기록 스레드가 파일에 쓸 시간
    finally:
        server.terminate()
        server.wait(10)
        rds.flushdb()

    entries = []
    for name in os.listdir(log_dir):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(log_dir, name), encoding="utf-8") as fp:
            entries += [json.loads(l) for l in fp]
    handled = [e for e in entries if e["part"] == "callback_handle"]
    writes = [e for e in entries if e["part"] == "mobius_write" and e["trace_id"]]
    client = [ms for ms, _ in results]
    busy = [e["ms"] for e in handled]

    print(f"워커 {a.workers}  동시 {a.concurrency}  요청 {a.requests}  가짜 CIN {a.cin_ms:g}ms  "
          f"소요 {elapsed:.1f}s  실패 {sum(1 for _, s in results if s != 200)}")
    line("클라이언트 응답", client)
    line("callback_handle", busy)
    print(f"대기 추정 p50={statistics.median(client) - statistics.median(busy):.1f} ms  "
          f"p95={pct(client, 0.95) - pct(busy, 0.95):.1f} ms")
    print(f"CIN 발생 {len(writes)}건 / 요청 {len(handled)}건")
    for pid in sorted({e["pid"] for e in handled}):
        mine = [e["ms"] for e in handled if e["pid"] == pid]
        print(f"  pid={pid}  처리 {len(mine)}건  처리 시간 합 {sum(mine) / 1000:.1f}s")
    samples = [e["metrics"] for e in entries if e["part"] == "system"]
    system = {}
    for name in ("worker_busy_pct", "accept_queue", "cpu_pct", "mem_available_mb", "worker_rss_mb"):
        values = [m[name] for m in samples if m.get(name) is not None]
        system[name] = {"min": min(values), "max": max(values)} if values else None
        print(f"  {name:<18} " + (f"최소 {min(values)}  최대 {max(values)}" if values else "기록 없음"))

    def stats(values):
        return {"n": len(values), "p50": round(statistics.median(values), 1),
                "p95": round(pct(values, 0.95), 1), "max": round(max(values), 1)} if values else None

    summary = {
        "run_id": run_id,
        "workers": a.workers, "threads": a.threads or None,
        "concurrency": a.concurrency, "requests": a.requests, "cin_ms": a.cin_ms,
        "elapsed_sec": round(elapsed, 1),
        "failed": sum(1 for _, s in results if s != 200),
        "client_ms": stats(client), "callback_handle_ms": stats(busy),
        "wait_ms": {"p50": round(statistics.median(client) - statistics.median(busy), 1),
                    "p95": round(pct(client, 0.95) - pct(busy, 0.95), 1)},
        "cin_writes": len(writes), "handled": len(handled),
        "per_pid": {str(pid): len([e for e in handled if e["pid"] == pid]) for pid in sorted({e["pid"] for e in handled})},
        "system": system,
    }
    with open(os.path.join(log_dir, "summary.json"), "w", encoding="utf-8") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)
    print(f"기록: {log_dir}")


if __name__ == "__main__":
    main()
