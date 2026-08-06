"""End-to-end proof that the approval tool actually blocks the agent loop.

Spawns the real server over stdio exactly as AnythingLLM does, calls
request_user_approval(ASK_USER), and verifies that:

  1. the tool does NOT return while a human has not decided (this is the pause -
     an agent loop waiting here cannot emit another tool call);
  2. progress heartbeats are emitted when the client supplies a progressToken,
     which is what keeps the client's 60s request timer from expiring;
  3. clicking Approve on the localhost page makes the SAME call return APPROVED
     with execution unlocked;
  4. every call returns inside the client's 60s limit whether or not a progressToken
     was supplied - since 1.14.0 the wait is chunked, so the heartbeat is a bonus
     rather than the thing safety depends on.

    python tests/smoke_blocking_approval.py
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
# One port per scenario. The page refuses SO_REUSEADDR on purpose (two instances must
# never both think they own the URL), so a port released by the previous scenario sits in
# TIME_WAIT and the next bind fails.
BASE_PORT = 8770
PORT = BASE_PORT


def use_port(offset: int) -> None:
    global PORT
    PORT = BASE_PORT + offset

_failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


class Server:
    def __init__(self, state_dir: str, timeout: int = 900, budget: int = 45):
        env = dict(
            os.environ,
            PYTHONUTF8="1",
            PLANNING_MCP_BLOCKING_APPROVAL="true",
            PLANNING_MCP_APPROVAL_PORT=str(PORT),
            PLANNING_MCP_APPROVAL_OPEN_BROWSER="false",
            PLANNING_MCP_APPROVAL_TIMEOUT=str(timeout),
            PLANNING_MCP_CALL_BUDGET=str(budget),
        )
        self.p = subprocess.Popen(
            [sys.executable, "-u", str(SERVER), "--state-dir", state_dir, "--log-level", "ERROR"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
        )
        self.n = 0
        self.inbox: list[dict] = []
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
            with self.lock:
                if "id" in msg:
                    self.replies[msg["id"]] = msg
                else:
                    self.inbox.append(msg)

    def send(self, method: str, params=None, msg_id=None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if msg_id is not None:
            msg["id"] = msg_id
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method: str, params=None, wait: float = 10):
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

    def call_async(self, name: str, arguments: dict, progress_token=None) -> int:
        """Fire a tool call without waiting - so we can observe that it does not return."""
        self.n += 1
        mid = self.n
        params: dict = {"name": name, "arguments": arguments}
        if progress_token is not None:
            params["_meta"] = {"progressToken": progress_token}
        self.send("tools/call", params, mid)
        return mid

    def reply_for(self, mid: int):
        with self.lock:
            msg = self.replies.pop(mid, None)
        if msg is None:
            return None
        return json.loads(msg["result"]["content"][0]["text"])

    def progress_notes(self) -> list[dict]:
        with self.lock:
            return [m for m in self.inbox if m.get("method") == "notifications/progress"]

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
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
    s.request("tools/call", {"name": "plan_and_think", "arguments": {
        "goal": "블로킹 승인 종단 검증", "thought": "분해 완료",
        "step_number": 1, "total_steps": 1, "need_more_thinking": False,
        "task_list": ["작업 하나", "작업 둘"]}})


def main() -> int:
    use_port(0)
    print("\n== 1) 사람이 결정할 때까지 도구가 반환하지 않는가 (= 루프 정지) ==")
    with tempfile.TemporaryDirectory() as tmp:
        s = Server(tmp)
        try:
            draft(s)
            mid = s.call_async(
                "request_user_approval",
                {"decision": "ASK_USER", "plan_summary": "요약입니다"},
                progress_token="tok-1",
            )
            time.sleep(3)
            check("3초 후에도 응답 없음 (에이전트 루프는 여기서 멈춘다)",
                  s.reply_for(mid) is None)

            queue = http_json("/api/pending")["requests"]
            pending = queue[0] if queue else {}
            check("승인 페이지가 계획을 노출", pending.get("plan_id", "").startswith("plan_"),
                  json.dumps(queue, ensure_ascii=False)[:200])
            check("계획 본문이 페이지에 포함", "작업 하나" in pending.get("display", ""))

            print("\n== 2) progressToken 이 있으면 하트비트로 60초 타임아웃을 리셋하는가 ==")
            time.sleep(19)
            notes = s.progress_notes()
            check("progress 알림 수신", len(notes) >= 1, f"count={len(notes)}")
            if notes:
                check("토큰이 되돌아옴", notes[0]["params"].get("progressToken") == "tok-1")

            print("\n== 3) 승인 클릭이 같은 호출을 APPROVED 로 반환시키는가 ==")
            http_json("/api/decide",
                      {"id": pending["id"], "decision": "APPROVED", "comment": "승인"})
            payload = None
            for _ in range(100):
                payload = s.reply_for(mid)
                if payload:
                    break
                time.sleep(0.05)
            check("도구가 이제 반환됨", payload is not None)
            if payload:
                check("plan_status == APPROVED", payload["plan_status"] == "APPROVED",
                      payload["plan_status"])
                check("실행 잠금 해제 + 다음 작업 지시",
                      payload["next_action"] == "CALL_UPDATE_TASK_PROGRESS"
                      and payload.get("next_task", {}).get("task_id") == 1)
        finally:
            s.close()

    print("\n== 4) progressToken 이 없어도 60초 전에 안전하게 반환되는가 ==")
    use_port(1)
    with tempfile.TemporaryDirectory() as tmp:
        # 1.14.0: 하트비트 여부와 무관하게 한 호출은 call_budget 안에 끝난다.
        s = Server(tmp, timeout=3600, budget=5)
        try:
            draft(s)
            started = time.monotonic()
            mid = s.call_async("request_user_approval",
                               {"decision": "ASK_USER", "plan_summary": "요약"})
            payload = None
            while time.monotonic() - started < 70:
                payload = s.reply_for(mid)
                if payload:
                    break
                time.sleep(0.2)
            elapsed = time.monotonic() - started
            check("60초 미만에 반환 (클라이언트 에러 방지)",
                  payload is not None and elapsed < 60, f"elapsed={elapsed:.1f}s")
            if payload:
                check("계획은 여전히 잠김", payload["plan_status"] == "AWAITING_APPROVAL",
                      payload["plan_status"])
                # 승인으로 오독될 여지를 없애기 위해 ok:false 로 내려간다.
                check("승인으로 읽히지 않음", payload["ok"] is False)
                check("APPROVAL_PENDING", payload.get("error_code") == "APPROVAL_PENDING")
                check("즉시 재호출을 지시",
                      payload["next_action"] == "CALL_REQUEST_USER_APPROVAL")
                check("남은 예산을 알려줌", payload.get("remaining_seconds", 0) > 0)
            check("하트비트를 보내지 않음", len(s.progress_notes()) == 0)
        finally:
            s.close()

    print("\n== 5) 전체 예산 소진 후에도 승인 창이 살아있고, 늦은 클릭이 반영되는가 ==")
    use_port(2)
    with tempfile.TemporaryDirectory() as tmp:
        # 전체 대기 예산을 한 조각으로 다 써버려서 "계획을 보여주고 멈춰라" 경로를 탄다.
        s = Server(tmp, timeout=5, budget=5)
        try:
            draft(s)
            started = time.monotonic()
            mid = s.call_async("request_user_approval",
                               {"decision": "ASK_USER", "plan_summary": "요약"})
            payload = None
            while time.monotonic() - started < 70:
                payload = s.reply_for(mid)
                if payload:
                    break
                time.sleep(0.2)
            check("예산 소진으로 반환",
                  payload is not None and payload["plan_status"] == "AWAITING_APPROVAL")
            check("사람에게 보여줄 계획을 함께 반환",
                  bool(payload and payload.get("display_to_user")))

            queue = http_json("/api/pending")["requests"]
            pending = queue[0] if queue else {}
            check("승인 요청이 페이지에 그대로 남아 있음", bool(pending.get("id")),
                  json.dumps(queue, ensure_ascii=False)[:160])
            check("아직 미결정 상태", pending.get("decided") in (None, ""))

            http_json("/api/decide",
                      {"id": pending["id"], "decision": "APPROVED", "comment": "뒤늦게 승인"})
            r = s.request("tools/call", {"name": "get_current_plan",
                                         "arguments": {"plan_id": "current"}})
            after = json.loads(r["result"]["content"][0]["text"])
            check("늦은 승인이 다음 호출에서 반영됨", after["plan_status"] == "APPROVED",
                  after["plan_status"])

            r = s.request("tools/call", {"name": "update_task_progress",
                                         "arguments": {"task_id": 1, "status": "IN_PROGRESS"}})
            run = json.loads(r["result"]["content"][0]["text"])
            check("실행 잠금 해제됨", run["ok"] is True, str(run.get("error_code")))
        finally:
            s.close()

    print("\n== 6) 승인 대기가 다른 세션을 막지 않는가 ==")
    use_port(3)
    with tempfile.TemporaryDirectory() as tmp:
        s = Server(tmp, timeout=900)
        try:
            draft(s)
            s.call_async("request_user_approval",
                         {"decision": "ASK_USER", "plan_summary": "요약"},
                         progress_token="tok-block")
            time.sleep(3)   # 승인 대기 진입 확인
            started = time.monotonic()
            other = s.request("tools/call",
                              {"name": "get_current_plan", "arguments": {"plan_id": "current"}},
                              wait=30)
            elapsed = time.monotonic() - started
            check("승인 대기 중에도 다른 호출이 응답됨", other is not None, "타임아웃")
            check("지연 없음 (head-of-line blocking 없음)", elapsed < 5, f"{elapsed:.1f}s")
            if other:
                payload = json.loads(other["result"]["content"][0]["text"])
                check("계획은 여전히 잠김", payload["plan_status"] == "AWAITING_APPROVAL",
                      payload["plan_status"])
        finally:
            s.close()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} - {', '.join(_failures)}")
        return 1
    print("블로킹 승인 종단 검증 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
