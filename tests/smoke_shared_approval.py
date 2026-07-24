"""근본 해결 검증: 프로세스가 여러 개여도 승인 페이지는 하나인가?

시나리오 - 사람은 8765 탭 하나만 열어둔 상태:
  1) 서버 A, B 를 같은 state_dir 로 띄운다 (A가 포트를 잡는다)
  2) B(포트를 못 잡은 쪽)가 승인을 요청한다
  3) 8765 페이지에 B의 요청이 뜨는가?
  4) 그 페이지에서 승인하면 B의 블로킹 호출이 풀리는가?
  5) A를 죽이면 B가 같은 포트를 인계받는가?
"""
import json, os, subprocess, sys, tempfile, threading, time, urllib.request
from pathlib import Path

SERVER = r"D:\LightweightSequentialThinking\server.py"
PORT = 8765
fails = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


class S:
    def __init__(self, state_dir, tag):
        self.tag = tag
        env = dict(os.environ, PYTHONUTF8="1", PLANNING_MCP_BLOCKING_APPROVAL="true",
                   PLANNING_MCP_APPROVAL_PORT=str(PORT),
                   PLANNING_MCP_APPROVAL_OPEN_BROWSER="false",
                   PLANNING_MCP_APPROVAL_TIMEOUT="900")
        self.p = subprocess.Popen([sys.executable, "-u", SERVER, "--state-dir", str(state_dir),
                                   "--log-level", "ERROR"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
        self.n = 0; self.lock = threading.Lock(); self.replies = {}
        threading.Thread(target=self._read, daemon=True).start()
        self.req("initialize", {"protocolVersion": "2024-11-05"})

    def _read(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in m:
                with self.lock:
                    self.replies[m["id"]] = m

    def _send(self, method, params, mid):
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n"); self.p.stdin.flush()

    def req(self, method, params=None, wait=60):
        self.n += 1; mid = self.n; self._send(method, params, mid)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            with self.lock:
                if mid in self.replies:
                    return self.replies.pop(mid)
            time.sleep(0.02)
        return None

    def call(self, name, args, wait=60):
        r = self.req("tools/call", {"name": name, "arguments": args}, wait)
        return json.loads(r["result"]["content"][0]["text"]) if r else None

    def call_async(self, name, args):
        self.n += 1; mid = self.n
        self._send("tools/call", {"name": name, "arguments": args}, mid)
        return mid

    def reply_for(self, mid):
        with self.lock:
            m = self.replies.pop(mid, None)
        return json.loads(m["result"]["content"][0]["text"]) if m else None

    def kill(self):
        self.p.kill(); self.p.wait(timeout=5)

    def close(self):
        try:
            self.p.stdin.close(); self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def page(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


shared = tempfile.mkdtemp()
print("== 1) 같은 state_dir 로 서버 2개 기동 ==")
A = S(shared, "A"); time.sleep(1.5)
B = S(shared, "B"); time.sleep(1.5)
health = page("/api/health")
check("8765 를 planning-mcp 가 서비스 중", health.get("server") == "planning-mcp-approval")
check("state_dir 일치", Path(health["state_dir"]).resolve() == Path(shared).resolve())

print("\n== 2) 포트를 못 잡은 B 가 승인 요청 ==")
B.call("plan_and_think", {"goal": "B의 목표", "thought": "t", "step_number": 1,
                          "total_steps": 1, "need_more_thinking": False,
                          "task_list": ["B작업1", "B작업2"]})
mid = B.call_async("request_user_approval", {"decision": "ASK_USER", "plan_summary": "B 요약"})
time.sleep(2)
check("B의 호출이 아직 블로킹 중", B.reply_for(mid) is None)

print("\n== 3) 사람이 보고 있는 그 페이지에 B의 요청이 뜨는가 ==")
queue = page("/api/pending")["requests"]
pending = queue[0] if queue else {}
check("요청이 페이지에 노출", bool(pending.get("id")), json.dumps(queue, ensure_ascii=False)[:150])
check("B의 계획 내용이 맞음", "B작업1" in (pending.get("display") or ""),
      (pending.get("display") or "")[:80])

print("\n== 4) 그 페이지에서 승인하면 B의 블로킹이 풀리는가 ==")
post("/api/decide", {"id": pending["id"], "decision": "APPROVED", "comment": "승인"})
payload = None
for _ in range(60):
    payload = B.reply_for(mid)
    if payload:
        break
    time.sleep(0.2)
check("B의 호출이 반환됨", payload is not None)
if payload:
    check("APPROVED 로 반환", payload["plan_status"] == "APPROVED", payload["plan_status"])

print("\n== 5) 페이지 주인(A)이 죽으면 B가 인계받는가 ==")
A.kill()
took_over = False
for _ in range(30):
    time.sleep(1)
    try:
        h = page("/api/health")
        took_over = h.get("server") == "planning-mcp-approval"
        if took_over:
            break
    except Exception:
        continue
check("같은 URL 이 계속 살아 있음 (자동 인계)", took_over)

B.close()
print()
print("FAILED: " + ", ".join(fails) if fails else "근본 해결 검증 통과 — 프로세스가 여러 개여도 승인 페이지는 하나.")
sys.exit(1 if fails else 0)
