"""Synthetic child protocol fixture; never makes a network request."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

request = json.loads(sys.stdin.buffer.readline())
url = request["url"]
mode = url.split("?", 1)[0].rsplit("/", 1)[-1]
if mode == "block":
    while True:
        time.sleep(1)
if mode == "cpu":
    while True:
        pass
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
if mode in {"spawn", "spawn-then-result"}:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    marker = url.split("?marker=", 1)[1]
    with open(marker, "w") as output:
        output.write(str(child.pid))
    if mode == "spawn":
        while True:
            time.sleep(1)
if mode == "crash":
    raise RuntimeError("synthetic crash with sensitive text")
if mode == "late":
    time.sleep(0.4)
if mode == "malformed":
    sys.stdout.write("not json\n")
    raise SystemExit(0)
if mode == "partial":
    sys.stdout.write('{"version":1')
    raise SystemExit(0)
if mode == "oversized":
    sys.stdout.write("x" * 9000)
    raise SystemExit(0)
if mode == "duplicate":
    sys.stdout.write('{"version":1,"version":1}\n')
    raise SystemExit(0)
if mode == "inspect-env":
    leaks = [name for name in os.environ if "DATABASE" in name or "SECRET" in name]
    sockets = []
    marker = url.split("?marker=", 1)[1]
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
            if target.startswith("socket:") or target == marker:
                sockets.append(fd)
        except FileNotFoundError:
            pass
    status_code = 599 if leaks or sockets else 200
else:
    status_code = 200
if mode == "wrong-attempt":
    request["attempt_id"] = str(uuid4())
response = {
    "version": True if mode == "bad-version" else 1,
    "attempt_id": request["attempt_id"],
    "kind": "result",
    "result": {
        "status": "invalid" if mode == "bad-schema" else "verified",
        "status_code": status_code,
        "checked_at": datetime.now(UTC).isoformat(),
    },
}
if mode == "max-result":
    response["result"]["emails"] = [f"user{i}@example.test" for i in range(10)]
    response["result"]["phones"] = [f"+1555000{i:04d}" for i in range(10)]
if mode == "extra":
    response["lease_token"] = str(uuid4())
if mode == "unknown-code":
    response = {
        "version": 1, "attempt_id": request["attempt_id"],
        "kind": "error", "code": "arbitrary_child_code",
    }
if mode == "nan":
    data = json.dumps(response).replace('"status_code": 200', '"status_code": NaN')
    sys.stdout.write(data + "\n")
    raise SystemExit(0)
sys.stdout.write(json.dumps(response) + "\n")
sys.stdout.flush()
if mode == "nonzero-success":
    raise SystemExit(2)
