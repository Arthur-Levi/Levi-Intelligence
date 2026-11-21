"""Synthetic child protocol fixture; never makes a network request."""

import json
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

request = json.loads(sys.stdin.buffer.readline())
if request["url"].endswith("/block"):
    while True:
        time.sleep(1)
if request["url"].endswith("/wrong-attempt"):
    request["attempt_id"] = str(uuid4())
response = {
    "version": 1,
    "attempt_id": request["attempt_id"],
    "kind": "result",
    "result": {
        "status": "verified", "status_code": 200,
        "checked_at": datetime.now(UTC).isoformat(),
    },
}
sys.stdout.write(json.dumps(response) + "\n")
sys.stdout.flush()
