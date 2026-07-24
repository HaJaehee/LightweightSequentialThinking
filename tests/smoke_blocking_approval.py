"""End-to-end proof that the approval tool actually blocks the agent loop.

Spawns the real server over stdio exactly as AnythingLLM does, calls
request_user_approval(ASK_USER), and verifies that:

  1. the tool does NOT return while a human has not decided (this is the pause -
     an agent loop waiting here cannot emit another tool call);
  2. progress heartbeats are emitted when the client supplies a progressToken,
     which is what keeps the client's 60s request timer from expiring;
  3. clicking Approve on the localhost page makes the SAME call return APPROVED
     with execution unlocked;
  4. without a progressToken the wait is capped below the SDK timeout instead of
     hanging until the client errors.

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
PORT = 8793

_failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


class Server:
    def __init__(self, state_dir: str, timeout: int = 900):
        env = dict(
            os.environ,
            PYTHONUTF8="1",
            PLANNING_MCP_BLOCKING_APPROVAL="true",
            PLANNING_MCP_APPROVAL_PORT=str(PORT),
            PLANNING_MCP_APPROVAL_OPEN_BROWSER="false",
            PLANNING_MCP_APPROVAL_TIMEOUT=str(timeout),
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

            pending = http_json("/api/pending")
            check("승인 페이지가 계획을 노출", pending.get("plan_id", "").startswith("plan_"),
                  json.dumps(pending, ensure_ascii=False)[:200])
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

    print("\n== 4) progressToken 이 없으면 60초 전에 안전하게 만료되는가 ==")
    with tempfile.TemporaryDirectory() as tmp:
        s = Server(tmp, timeout=3600)
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
                check("정지 지시 유지", payload["next_action"] == "STOP_AND_WAIT_FOR_USER")
            check("하트비트를 보내지 않음", len(s.progress_notes()) == 0)
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
