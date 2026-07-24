"""Out-of-band human approval: a localhost page with real Approve/Reject buttons.

Why this exists
---------------
AnythingLLM's agent loop calls a tool and waits *synchronously* for its result before
the model can generate anything else. Returning "STOP_AND_WAIT_FOR_USER" as text does
not stop a weak model - it reads the instruction as one more observation and keeps
calling tools. But if the tool simply does not return yet, the loop physically cannot
advance: control has not gone back to the LLM.

So the pause is achieved by making `request_user_approval(ASK_USER)` block until a human
decides here. This needs nothing from AnythingLLM - no loop patch, no directOutput, no
per-tool config (none of which exist as of 1.15.0).

The cost is that approval happens on this page rather than in the chat bubble, and that
is unavoidable: an in-chat reply is by definition a *new turn*, which can only happen
after the tool has already returned - the opposite of blocking.

Standard library only, so it runs under the bundled embeddable interpreter (which has
no tkinter, ruling out native dialogs).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("planning-mcp.approval")

DECISIONS = ("APPROVED", "REJECTED", "REVISE")


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """HTTP server that refuses to share a port.

    http.server sets allow_reuse_address, and on Windows SO_REUSEADDR lets a second
    process bind a port another process is already listening on. That would make the
    port-fallback below silently useless: a duplicate planning-mcp instance would
    "succeed" on the same port and its approval page would never be the one the browser
    reaches. Turning reuse off makes a real conflict raise, so we walk to the next port.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:  # Windows: hard exclusivity
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            except OSError:
                pass
        super().server_bind()


class PendingApproval:
    """One awaited human decision."""

    def __init__(self, plan_id: str, goal: str, display: str, tasks: list[dict[str, Any]]):
        self.id = uuid.uuid4().hex
        self.plan_id = plan_id
        self.goal = goal
        self.display = display
        self.tasks = tasks
        self.decision: str | None = None
        self.comment: str = ""
        self._event = threading.Event()

    def resolve(self, decision: str, comment: str = "") -> bool:
        if self._event.is_set():
            return False
        self.decision = decision
        self.comment = comment or ""
        self._event.set()
        return True

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

    def cancel(self) -> None:
        self._event.set()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "goal": self.goal,
            "display": self.display,
            "tasks": self.tasks,
            "decided": self.decision,
        }


_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>planning-mcp 승인</title>
<style>
:root{color-scheme:light dark}
body{font-family:system-ui,"Segoe UI","Malgun Gothic",sans-serif;margin:0;padding:2rem 1rem;
     background:#f6f7f9;color:#16181d}
@media(prefers-color-scheme:dark){body{background:#15171c;color:#e8eaed}}
.card{max-width:720px;margin:0 auto;background:#fff;border-radius:12px;padding:1.75rem;
      box-shadow:0 1px 3px rgba(0,0,0,.12)}
@media(prefers-color-scheme:dark){.card{background:#1e2126;box-shadow:none;border:1px solid #2c3038}}
h1{font-size:1.05rem;margin:0 0 .35rem;letter-spacing:.02em;text-transform:uppercase;opacity:.6}
.goal{font-size:1.15rem;font-weight:600;margin:0 0 1.25rem;line-height:1.4}
pre{white-space:pre-wrap;word-break:break-word;background:#f2f3f5;border-radius:8px;
    padding:1rem;font-size:.92rem;line-height:1.6;margin:0 0 1.25rem;
    font-family:ui-monospace,Consolas,monospace}
@media(prefers-color-scheme:dark){pre{background:#15171c}}
textarea{width:100%;box-sizing:border-box;min-height:64px;border-radius:8px;padding:.6rem;
         border:1px solid #ccd0d5;font:inherit;font-size:.92rem;margin-bottom:1rem;
         background:transparent;color:inherit}
@media(prefers-color-scheme:dark){textarea{border-color:#3a3f47}}
.row{display:flex;gap:.6rem;flex-wrap:wrap}
button{flex:1 1 auto;min-width:140px;padding:.85rem 1rem;border:0;border-radius:8px;
       font:inherit;font-weight:600;cursor:pointer;font-size:.95rem}
.ok{background:#1a7f37;color:#fff}.no{background:#b42318;color:#fff}.rev{background:#8a5a00;color:#fff}
button:disabled{opacity:.5;cursor:default}
.idle{text-align:center;opacity:.6;padding:2.5rem 0;font-size:.95rem}
.done{text-align:center;padding:2rem 0;font-size:1.05rem;font-weight:600}
.hint{margin-top:1rem;font-size:.82rem;opacity:.55;line-height:1.5}
</style></head><body><div class="card" id="root">
<div class="idle">연결 중…</div></div>
<script>
let currentId=null,busy=false;
async function poll(){
  if(busy)return;
  try{
    const r=await fetch('/api/pending');const d=await r.json();
    if(!d.id){currentId=null;render(null);return;}
    if(d.id!==currentId){currentId=d.id;render(d);}
  }catch(e){}
}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function render(d){
  const root=document.getElementById('root');
  if(!d){root.innerHTML='<div class="idle">대기 중인 승인 요청이 없습니다.<br>'+
    '<span style="font-size:.85rem">에이전트가 계획을 제출하면 여기에 표시됩니다.</span></div>';return;}
  root.innerHTML='<h1>승인 요청 · '+esc(d.plan_id)+'</h1>'+
    '<p class="goal">'+esc(d.goal)+'</p>'+
    '<pre>'+esc(d.display)+'</pre>'+
    '<textarea id="c" placeholder="수정 요청 시 내용을 적어주세요 (거절 사유도 여기에)"></textarea>'+
    '<div class="row">'+
    '<button class="ok" onclick="decide(\\'APPROVED\\')">승인</button>'+
    '<button class="rev" onclick="decide(\\'REVISE\\')">수정 요청</button>'+
    '<button class="no" onclick="decide(\\'REJECTED\\')">거절</button></div>'+
    '<p class="hint">이 결정이 에이전트에게 즉시 전달됩니다. 결정하기 전까지 에이전트는 '+
    '아무것도 실행하지 못하고 멈춰 있습니다.</p>';
}
async function decide(dec){
  if(busy||!currentId)return;busy=true;
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  const c=(document.getElementById('c')||{}).value||'';
  try{
    await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:currentId,decision:dec,comment:c})});
    const label={APPROVED:'승인했습니다',REJECTED:'거절했습니다',REVISE:'수정을 요청했습니다'}[dec];
    document.getElementById('root').innerHTML='<div class="done">'+label+
      '<br><span style="font-weight:400;opacity:.6;font-size:.9rem">AnythingLLM 대화로 돌아가세요.</span></div>';
  }catch(e){}
  currentId=null;busy=false;
  setTimeout(()=>{poll();},2500);
}
poll();setInterval(poll,1500);
</script></body></html>"""


class ApprovalServer:
    """Lazily-started localhost page that resolves pending approvals."""

    def __init__(self, port: int = 8765, open_browser: bool = True, port_attempts: int = 10):
        self.base_port = port
        self.port = port
        self.open_browser = open_browser
        self.port_attempts = max(1, port_attempts)
        self._httpd: _ExclusiveHTTPServer | None = None
        self._lock = threading.Lock()
        self._pending: PendingApproval | None = None
        self._opened_once = False

    # ---- lifecycle -----------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def _ensure_started(self) -> bool:
        """Bind the approval UI, walking forward if the port is taken.

        A stale server process from a previous restart keeps holding the base port. If a
        second instance simply gave up, blocking approval would silently degrade back to
        advisory-only - the exact safety loss this feature exists to prevent - so we take
        the next free port instead.
        """
        with self._lock:
            if self._httpd is not None:
                return True
            handler = self._make_handler()
            last: OSError | None = None
            for offset in range(self.port_attempts):
                port = self.base_port + offset
                try:
                    self._httpd = _ExclusiveHTTPServer(("127.0.0.1", port), handler)
                except OSError as exc:
                    last = exc
                    continue
                self.port = port
                threading.Thread(
                    target=self._httpd.serve_forever, name="approval-ui", daemon=True
                ).start()
                if offset:
                    log.warning(
                        "Approval UI port %s was busy (another planning-mcp instance?); "
                        "using %s instead",
                        self.base_port,
                        port,
                    )
                log.info("Approval UI listening on %s", self.url)
                return True
            log.error(
                "Could not bind the approval UI on ports %s-%s: %s",
                self.base_port,
                self.base_port + self.port_attempts - 1,
                last,
            )
            self._httpd = None
            return False

    def shutdown(self) -> None:
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()  # release the listening socket, not just the loop

    # ---- approval flow -------------------------------------------------
    def open_request(
        self, plan_id: str, goal: str, display: str, tasks: list[dict[str, Any]]
    ) -> PendingApproval | None:
        """Publish an approval request. Returns None if the UI could not start."""
        if not self._ensure_started():
            return None
        pending = PendingApproval(plan_id, goal, display, tasks)
        with self._lock:
            if self._pending is not None:
                self._pending.cancel()  # a superseded request must not block a thread
            self._pending = pending
        self._surface()
        return pending

    def close_request(self, pending: PendingApproval) -> None:
        with self._lock:
            if self._pending is pending:
                self._pending = None

    def _surface(self) -> None:
        """Bring the approval page in front of the human."""
        log.warning("HUMAN APPROVAL NEEDED -> %s", self.url)
        if not self.open_browser:
            return
        try:
            webbrowser.open(self.url)
            self._opened_once = True
        except Exception as exc:  # noqa: BLE001 - a headless box must not break approval
            log.warning("Could not open a browser (%s). Open %s manually.", exc, self.url)

    # ---- http ----------------------------------------------------------
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                log.debug("approval-ui %s", fmt % args)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/":
                    self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/pending":
                    with server._lock:
                        p = server._pending
                    payload = p.to_json() if p else {"id": None}
                    self._send(
                        200,
                        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8",
                    )
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/api/decide":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400)
                    return

                decision = str(body.get("decision", "")).upper()
                if decision not in DECISIONS:
                    self._send(400, b'{"ok":false}', "application/json")
                    return

                with server._lock:
                    p = server._pending
                ok = bool(p and p.id == body.get("id") and p.resolve(decision, body.get("comment", "")))
                self._send(
                    200,
                    json.dumps({"ok": ok}).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

        return Handler
