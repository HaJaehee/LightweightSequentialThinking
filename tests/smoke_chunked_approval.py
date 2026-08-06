"""End-to-end proof that the approval wait never outruns the client's request timeout.

Spawns the real server over stdio exactly as AnythingLLM does. The thing being proved is
not "the pause works" (smoke_blocking_approval covers that) but that the pause is taken in
slices small enough that no client ever kills the call - because a killed call does not
merely fail, it makes the client discard the result and breaks the conversation in the
middle of an approval.

Verified here:

  1. a slice returns well inside the client's 60s limit, as ok:false + APPROVAL_PENDING;
  2. the response carries nothing a weak model could read as a verdict;
  3. re-asking reuses the SAME request - the page never redraws, so a half-written
     comment is never wiped, and created_at keeps counting toward the total budget;
  4. a click during any later slice unlocks execution in that slice;
  5. while the request is open, a decision sent by the MODEL is refused;
  6. notifications/cancelled unblocks the call and is recorded as the client's real cap.

    python tests/smoke_chunked_approval.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"

# One port per scenario. The approval page refuses SO_REUSEADDR on purpose (two
# instances must never both think they own the URL), so a port just released by the
# previous scenario sits in TIME_WAIT and the next bind fails.
BASE_PORT = 8799
PORT = BASE_PORT

# Small enough to keep the smoke run quick, large enough that a slice is observably a
# wait rather than an immediate return.
BUDGET = 3

_failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


class Server:
    def __init__(self, state_dir: str, timeout: int = 900, budget: int = BUDGET,
                 mode: str = "chunked"):
        env = dict(
            os.environ,
            PYTHONUTF8="1",
            PLANNING_MCP_BLOCKING_APPROVAL="true",
            PLANNING_MCP_APPROVAL_PORT=str(PORT),
            PLANNING_MCP_APPROVAL_OPEN_BROWSER="false",
            PLANNING_MCP_APPROVAL_TIMEOUT=str(timeout),
            PLANNING_MCP_CALL_BUDGET=str(budget),
            PLANNING_MCP_APPROVAL_MODE=mode,
        )
        self.state_dir = Path(state_dir)
        self.p = subprocess.Popen(
            [sys.executable, "-u", str(SERVER), "--state-dir", state_dir, "--log-level", "ERROR"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env,
        )
        self.n = 0
        self.replies: dict[int, dict] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2024-11-05"})

    def _reader(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                with self.lock:
                    self.replies[msg["id"]] = msg

    def send(self, method: str, params=None, msg_id=None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if msg_id is not None:
            msg["id"] = msg_id
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method: str, params=None, wait: float = 20):
        self.n += 1
        mid = self.n
        self.send(method, params, mid)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            with self.lock:
                if mid in self.replies:
                    return self.replies.pop(mid)
            time.sleep(0.02)
        return None

    def call(self, name: str, arguments: dict, wait: float = 20):
        reply = self.request("tools/call", {"name": name, "arguments": arguments}, wait=wait)
        if reply is None:
            return None
        return json.loads(reply["result"]["content"][0]["text"])

    def call_async(self, name: str, arguments: dict) -> int:
        self.n += 1
        mid = self.n
        self.send("tools/call", {"name": name, "arguments": arguments}, mid)
        return mid

    def reply_for(self, mid: int):
        with self.lock:
            msg = self.replies.pop(mid, None)
        return None if msg is None else json.loads(msg["result"]["content"][0]["text"])

    def audit(self) -> list[dict]:
        path = self.state_dir / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def page_is_up(self, deadline: float = 8.0) -> bool:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                if http_json("/api/health").get("server") == "planning-mcp-approval":
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.1)
        return False

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.p.kill()


def http_json(path: str, body=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    if body is None:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def draft(s: Server) -> None:
    s.call("plan_and_think", {
        "goal": "분할 승인 종단 검증", "thought": "분해 완료",
        "step_number": 1, "total_steps": 1, "need_more_thinking": False,
        "task_list": ["작업 하나", "작업 둘"]})


def ask(s: Server, wait: float = 20):
    return s.call("request_user_approval",
                  {"decision": "ASK_USER", "plan_summary": "요약"}, wait=wait)


def wait_for_page(deadline: float = 10.0) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            if http_json("/api/pending")["requests"]:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
    return False


def scenario_chunking(state_dir: str) -> None:
    print("\n1) a slice ends inside the client's limit and says PENDING")
    s = Server(state_dir)
    try:
        check("the approval page is listening", s.page_is_up())
        draft(s)
        started = time.monotonic()
        res = ask(s)
        elapsed = time.monotonic() - started
        check("the call returned at all", res is not None)
        check("it returned inside the SDK's 60s limit", elapsed < 55, f"took {elapsed:.1f}s")
        check("it waited rather than returning instantly", elapsed >= BUDGET - 0.5,
              f"took {elapsed:.1f}s")
        check("it does not read as success", res.get("ok") is False)
        check("error_code is APPROVAL_PENDING", res.get("error_code") == "APPROVAL_PENDING")
        check("the plan is still locked", res.get("plan_status") == "AWAITING_APPROVAL")
        check("it names the call to repeat",
              res.get("next_action") == "CALL_REQUEST_USER_APPROVAL")
        check("the hint denies approval outright", "NOT decided" in res.get("next_action_hint", ""))

        print("\n2) nothing in the payload can be mistaken for a verdict")
        for field in ("tasks", "display_to_user", "next_task"):
            check(f"no {field}", field not in res)
        check("it reports the remaining budget", res.get("remaining_seconds", 0) > 0)

        print("\n3) re-asking reuses the request, so the page never redraws")
        check("the request is on the page", wait_for_page())
        first = http_json("/api/pending")["requests"][0]
        ask(s)
        ask(s)
        after = http_json("/api/pending")["requests"]
        check("still exactly one request", len(after) == 1, f"{len(after)} queued")
        check("same request id", after[0]["id"] == first["id"])
        check("the agent is shown as waiting", after[0]["agent_waiting"] is True)

        print("\n4) a click during a later slice unlocks execution")
        mid = s.call_async("request_user_approval",
                           {"decision": "ASK_USER", "plan_summary": "요약"})
        time.sleep(0.5)
        http_json("/api/decide", {"id": first["id"], "decision": "APPROVED",
                                  "comment": "좋습니다", "task_comments": {}, "scope": "PLAN"})
        deadline = time.monotonic() + 20
        res = None
        while time.monotonic() < deadline:
            res = s.reply_for(mid)
            if res is not None:
                break
            time.sleep(0.05)
        check("the slice in flight returned the decision", res is not None)
        check("execution is unlocked", res and res.get("plan_status") == "APPROVED")
        check("the next task is handed over", bool(res and res.get("next_task")))
    finally:
        s.close()


def scenario_self_approval(state_dir: str) -> None:
    print("\n5) the model cannot answer its own question")
    s = Server(state_dir)
    try:
        check("the approval page is listening", s.page_is_up())
        draft(s)
        ask(s)
        check("the request is on the page", wait_for_page())
        for decision in ("APPROVED", "REJECTED", "REVISE"):
            res = s.call("request_user_approval",
                         {"decision": decision, "user_comment": "내 마음대로"})
            check(f"{decision} from the model is refused",
                  res.get("error_code") == "APPROVAL_PENDING")
            check(f"the plan stays locked after {decision}",
                  res.get("plan_status") == "AWAITING_APPROVAL")
        check("the refusal is audited",
              any(e["event"] == "self_approval_refused" for e in s.audit()))
        check("the request is still answerable by a human",
              len(http_json("/api/pending")["requests"]) == 1)
    finally:
        s.close()


def scenario_cancellation(state_dir: str) -> None:
    print("\n6) a client giving up unblocks the call and teaches the server its limit")
    s = Server(state_dir, budget=30)
    try:
        check("the approval page is listening", s.page_is_up())
        draft(s)
        mid = s.call_async("request_user_approval",
                           {"decision": "ASK_USER", "plan_summary": "요약"})
        check("the request reached the page", wait_for_page())
        time.sleep(0.5)
        started = time.monotonic()
        s.send("notifications/cancelled", {"requestId": mid, "reason": "timeout"})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if s.reply_for(mid) is not None:
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        check("the wait ended promptly", elapsed < 10, f"took {elapsed:.1f}s")
        events = s.audit()
        check("the cancellation is audited",
              any(e["event"] == "client_cancelled_call" for e in events))
        caps = s.state_dir / "client_caps.json"
        check("the observed limit is remembered", caps.exists())
        if caps.exists():
            check("it records how long the client allowed",
                  json.loads(caps.read_text(encoding="utf-8")).get("cancelled_after_sec", 0) > 0)
        check("the human can still decide",
              len(http_json("/api/pending")["requests"]) == 1)
    finally:
        s.close()


def main() -> int:
    print("Chunked approval smoke test")
    print(f"  server : {SERVER}")
    print(f"  ports  : {BASE_PORT}-{BASE_PORT + 2} (one per scenario)")
    print(f"  budget : {BUDGET}s per call")
    global PORT
    for offset, scenario in enumerate(
        (scenario_chunking, scenario_self_approval, scenario_cancellation)
    ):
        PORT = BASE_PORT + offset
        with tempfile.TemporaryDirectory() as state_dir:
            scenario(state_dir)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
