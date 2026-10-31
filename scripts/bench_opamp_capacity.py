"""Capacity bench for the OpAMP endpoint: how many agents can one node serve? (#109)

Measures the *whole* message path (auth lookup + protobuf + agent-row upsert +
metrics), not a microbenchmark, because the row upsert happens per message and is
what actually bites at fleet scale.

    BENCH_AGENTS=2000 BENCH_THREADS=8 BENCH_REQUESTS=150 python3 scripts/bench_opamp_capacity.py
    BENCH_WORKERS=4 python3 scripts/bench_opamp_capacity.py     # multi-worker comparison

Numbers from the machine this was written on (baseline for #109): 452 msg/s per
worker, p50 2.0 ms; 5 SQLite upserts per message; ~38 KB RAM/agent.
"""

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import httpx

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from proto import opamp_pb2  # noqa: E402
from server.agent_auth import hash_token  # noqa: E402
from server.state import SQLiteCredentialStore  # noqa: E402

PORT = 4398
DATA = pathlib.Path("/tmp/opamp-bench")
ADMIN = "bench-pw-123456789"
AGENTS = int(os.environ.get("BENCH_AGENTS", "2000"))
THREADS = int(os.environ.get("BENCH_THREADS", "8"))
PER_THREAD = int(os.environ.get("BENCH_REQUESTS", "150"))
CONFIG_BYTES = int(os.environ.get("BENCH_CONFIG_BYTES", "2048"))


def build_message(uid: bytes, seq: int, config_body: bytes) -> bytes:
    msg = opamp_pb2.AgentToServer()
    msg.instance_uid = uid
    msg.sequence_num = seq
    msg.capabilities = opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    msg.health.healthy = True
    msg.health.start_time_unix_nano = 1
    d = msg.agent_description
    for key, value in (("service.name", "otelcol"), ("service.version", "0.140.0"),
                       ("host.name", "bench-host"), ("os.type", "linux")):
        item = d.identifying_attributes.add()
        item.key = key
        item.value.string_value = value
    for key in ("environment", "region", "team"):
        item = d.non_identifying_attributes.add()
        item.key = key
        item.value.string_value = "prod"
    msg.effective_config.config_map.config_map[""].body = config_body
    msg.effective_config.config_map.config_map[""].content_type = "text/yaml"
    msg.available_components.hash = b"\x01" * 32
    return msg.SerializeToString()


def seed_credentials(tokens: list) -> None:
    store = SQLiteCredentialStore(DATA / "opamp.db")
    for uid in range(AGENTS):
        store.add(hash_token(tokens[uid]), f"{uid:032x}")


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 1024


def main() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)

    tokens = [f"opamp_agent_bench{uid:022d}" for uid in range(AGENTS)]
    seed_credentials(tokens)

    env = {
        **os.environ,
        "DATA_DIR": str(DATA),
        "ADMIN_PASSWORD": ADMIN,
        "OPAMP_AGENT_AUTH": "token",
        "OPAMP_CONFIG_PUSH_ENABLED": "false",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.main:app", "--port", str(PORT),
         "--log-level", "error", *(["--workers", os.environ["BENCH_WORKERS"]]
                                 if os.environ.get("BENCH_WORKERS") else [])],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise SystemExit("server did not start")

        config_body = b"receivers: [otlp]\nprocessors: [batch]\nexporters: [debug]\n" + b"#" * CONFIG_BYTES
        payloads = {
            uid: build_message(bytes.fromhex(f"{uid:032x}"), uid, config_body)
            for uid in range(AGENTS)
        }
        headers = {
            uid: {"Authorization": f"Bearer {tokens[uid]}",
                  "Content-Type": "application/x-protobuf"}
            for uid in range(AGENTS)
        }

        base_rss = rss_mb(server.pid)
        latencies: list = []
        errors: list = []
        lock = threading.Lock()
        url = f"http://127.0.0.1:{PORT}/v1/opamp"

        def worker(thread_id: int) -> None:
            client = httpx.Client(limits=httpx.Limits(max_keepalive_connections=4), timeout=30)
            local = []
            try:
                for index in range(PER_THREAD):
                    uid = (thread_id * PER_THREAD + index) % AGENTS
                    start = time.perf_counter()
                    try:
                        resp = client.post(url, content=payloads[uid], headers=headers[uid])
                        if resp.status_code != 200:
                            with lock:
                                errors.append(resp.status_code)
                    except Exception as exc:  # noqa: BLE001
                        with lock:
                            errors.append(repr(exc)[:80])
                    local.append((time.perf_counter() - start) * 1000)
            finally:
                client.close()
            with lock:
                latencies.extend(local)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        start = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - start

        total = THREADS * PER_THREAD
        rps = total / elapsed
        latencies.sort()
        peak_rss = rss_mb(server.pid)

        # Writes actually reached the DB?
        with sqlite3.connect(DATA / "opamp.db") as conn:
            rows = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            creds = conn.execute("SELECT COUNT(*) FROM agent_credentials").fetchone()[0]

        print(f"agents seeded        : {AGENTS} (credentials) / {rows} registered by traffic")
        print(f"config body          : {CONFIG_BYTES} bytes per message")
        print(f"requests             : {total} over {elapsed:.2f}s across {THREADS} keep-alive clients")
        print(f"throughput           : {rps:,.0f} OpAMP msg/s")
        print(f"latency p50/p95/p99  : {latencies[len(latencies)//2]:.1f} / "
              f"{latencies[int(len(latencies)*0.95)]:.1f} / "
              f"{latencies[int(len(latencies)*0.99)]:.1f} ms")
        print(f"errors               : {len(errors)} {errors[:3]}")
        print(f"server RSS           : {base_rss:.0f} MB idle -> {peak_rss:.0f} MB "
              f"({(peak_rss - base_rss) * 1024 / max(rows, 1):.0f} KB/agent incl. cache)")
        print(f"db size              : {(DATA / 'opamp.db').stat().st_size / 1e6:.1f} MB for {creds} creds")
        for interval in (30, 10, 5):
            print(f"agents sustainable @ {interval:>2}s poll (50% headroom): "
                  f"{rps * interval * 0.5:,.0f}")
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
