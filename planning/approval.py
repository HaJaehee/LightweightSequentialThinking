"""Human approval: shared file-backed state plus a single localhost page over it.

Why the state is in a file
--------------------------
Approval used to live in process memory. Restarts leave old MCP server processes alive
on the same state directory, so each one bound its own port and served its own page. The
human had one tab open - usually on the first port - and any approval request raised by
another process appeared on a port nobody was looking at, then timed out. The gate
silently degraded to "the model asked and nothing stopped it", which is the exact failure
this whole subsystem exists to prevent.

So the request and the decision live in `state/approval.json`, and the page is just a
view over that file. Any process can publish a request; any process can read the
decision. Reads take no lock (writes are atomic rename, so a reader never sees a torn
file); only writers serialize.

Why exactly one page
--------------------
The URL has to be stable, because a human keeps that tab open. Only the base port is
ever bound. If it is already taken, we check whether the occupant is another
planning-mcp on the *same* state directory - if so it is already serving our requests
and we need no page of our own. A background thread keeps retrying the bind, so if the
owner exits another process takes the same port over and the open tab keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from .filelock import exclusive

log = logging.getLogger("planning-mcp.approval")

DECISIONS = ("APPROVED", "REJECTED", "REVISE")
APPROVAL_FILENAME = "approval.json"
APPROVAL_LOCK_FILENAME = ".approvallock"
SERVER_SIGNATURE = "planning-mcp-approval"


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------


class ApprovalStore:
    """The approval request and its decision, shared by every server process."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def path(self) -> Path:
        return self.state_dir / APPROVAL_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.state_dir / APPROVAL_LOCK_FILENAME

    # ---- io -----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        """Current record, or {}. Lock-free: writes are atomic."""
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _write(self, record: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("Could not persist approval state: %s", exc)

    # ---- operations ---------------------------------------------------
    def _requests(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return list(record.get("requests") or [])

    def publish(
        self,
        plan_id: str,
        goal: str,
        display: str,
        tasks: list[dict[str, Any]],
        fingerprint: str,
    ) -> str:
        """Queue a request for the human. Returns its id.

        Concurrent sessions each get their own entry: one queue slot per plan would make
        two sessions asking at once hide each other. A new request for the SAME plan
        replaces that plan's old entry (the plan was revised), never another plan's.
        """
        request_id = uuid.uuid4().hex
        entry = {
            "id": request_id,
            "plan_id": plan_id,
            "goal": goal,
            "display": display,
            "tasks": tasks,
            "fingerprint": fingerprint,
            "created_at": time.time(),
            "created_by_pid": os.getpid(),
            "decision": None,
            "comment": "",
            "decided_at": None,
        }
        with exclusive(self.lock_path) as got:
            record = self.read()
            queue = [r for r in self._requests(record) if r.get("plan_id") != plan_id]
            queue.append(entry)
            self._write({"requests": queue})
        if not got:
            log.warning("Published an approval request without the write lock")
        return request_id

    def record_decision(self, request_id: str, decision: str, comment: str) -> bool:
        """Called by the page, for one specific queued request."""
        if decision not in DECISIONS:
            return False
        with exclusive(self.lock_path):
            record = self.read()
            queue = self._requests(record)
            for entry in queue:
                if entry.get("id") == request_id and entry.get("decision") is None:
                    entry["decision"] = decision
                    entry["comment"] = comment or ""
                    entry["decided_at"] = time.time()
                    self._write({"requests": queue})
                    return True
            return False

    def peek(self) -> list[dict[str, Any]]:
        """Everything currently queued, oldest first."""
        return sorted(self._requests(self.read()), key=lambda r: r.get("created_at", 0))

    def _take(self, match) -> tuple[str, str] | None:
        with exclusive(self.lock_path):
            queue = self._requests(self.read())
            for entry in queue:
                verdict = match(entry)
                if verdict is None:
                    continue
                queue = [r for r in queue if r.get("id") != entry.get("id")]
                self._write({"requests": queue})
                return verdict if verdict != "drop" else None
            return None

    def claim(self, request_id: str) -> tuple[str, str] | None:
        """Consume the decision for a specific request. Used by the blocking waiter."""

        def match(entry):
            if entry.get("id") != request_id or not entry.get("decision"):
                return None
            return entry["decision"], entry.get("comment", "")

        return self._take(match)

    def claim_for_plan(self, plan_id: str, fingerprint: str) -> tuple[str, str] | None:
        """Consume a decision made after the tool call already returned.

        Only honoured for the exact plan version that was on screen - the human agreed
        to what they saw, not to whatever the plan became afterwards.
        """

        def match(entry):
            if entry.get("plan_id") != plan_id or not entry.get("decision"):
                return None
            if entry.get("fingerprint") != fingerprint:
                log.warning("Discarding a decision for a plan that has since changed (%s)", plan_id)
                return "drop"
            return entry["decision"], entry.get("comment", "")

        return self._take(match)

    def clear(self) -> None:
        with exclusive(self.lock_path):
            self._write({"requests": []})


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

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
let seen='',busy=false,flash=null,pendingCount=0;
const IDLE_TITLE='planning-mcp 승인';
// A popup can be blocked, land on another monitor, or open behind other windows.
// So the page makes itself noticeable instead: the tab title flashes and a short tone
// plays. Leaving this tab open is the reliable way to catch approval requests.
function alertOn(){
  if(flash)return;
  let on=false;
  flash=setInterval(()=>{on=!on;document.title=on?
    '\\u26A0 승인 대기 '+pendingCount+'건':IDLE_TITLE;},700);
  try{
    const C=window.AudioContext||window.webkitAudioContext;if(!C)return;
    const ctx=new C();const o=ctx.createOscillator();const g=ctx.createGain();
    o.connect(g);g.connect(ctx.destination);o.frequency.value=880;g.gain.value=0.08;
    o.start();o.stop(ctx.currentTime+0.18);
    setTimeout(()=>{try{ctx.close();}catch(e){}},400);
  }catch(e){}
}
function alertOff(){
  if(flash){clearInterval(flash);flash=null;}
  document.title=IDLE_TITLE;
}
async function poll(){
  if(busy)return;
  try{
    const r=await fetch('/api/pending');const d=await r.json();
    const list=d.requests||[];
    const sig=list.map(x=>x.id+':'+(x.decided||'')).join('|');
    if(sig===seen)return;
    seen=sig;
    const undecided=list.filter(x=>!x.decided);
    pendingCount=undecided.length;
    render(list);
    if(undecided.length)alertOn();else alertOff();
  }catch(e){}
}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function render(list){
  const root=document.getElementById('root');
  if(!list.length){root.innerHTML='<div class="idle">대기 중인 승인 요청이 없습니다.<br>'+
    '<span style="font-size:.85rem">에이전트가 계획을 제출하면 여기에 표시됩니다.</span></div>';return;}
  // 여러 세션이 동시에 승인을 기다릴 수 있으므로 큐 전체를 보여준다.
  root.innerHTML=list.map(d=>{
    if(d.decided){
      const label={APPROVED:'승인함',REJECTED:'거절함',REVISE:'수정 요청함'}[d.decided]||d.decided;
      return '<div class="done">'+esc(d.plan_id)+' — '+label+
        '<br><span style="font-weight:400;opacity:.6;font-size:.9rem">'+
        '에이전트가 이 결정을 반영합니다.</span></div>';
    }
    return '<h1>승인 요청 · '+esc(d.plan_id)+'</h1>'+
      '<p class="goal">'+esc(d.goal)+'</p>'+
      '<pre>'+esc(d.display)+'</pre>'+
      '<textarea id="c-'+d.id+'" placeholder="수정 요청 시 내용을 적어주세요 (거절 사유도 여기에)"></textarea>'+
      '<div class="row">'+
      '<button class="ok" onclick="decide(\\''+d.id+'\\',\\'APPROVED\\')">승인</button>'+
      '<button class="rev" onclick="decide(\\''+d.id+'\\',\\'REVISE\\')">수정 요청</button>'+
      '<button class="no" onclick="decide(\\''+d.id+'\\',\\'REJECTED\\')">거절</button></div>';
  }).join('<hr style="border:0;border-top:1px solid #ccd0d5;margin:1.75rem 0">')+
    '<p class="hint">결정하기 전까지 해당 에이전트는 아무것도 실행하지 못합니다. '+
    '요청은 응답하실 때까지 사라지지 않으니 천천히 검토하셔도 됩니다.</p>';
}
async function decide(id,dec){
  if(busy)return;busy=true;alertOff();
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  const box=document.getElementById('c-'+id);
  const c=box?box.value:'';
  try{
    await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id,decision:dec,comment:c})});
  }catch(e){}
  busy=false;seen='';poll();
}
poll();setInterval(poll,1500);
</script></body></html>"""


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Refuses to share its port.

    http.server sets allow_reuse_address, and on Windows SO_REUSEADDR lets a second
    process bind a port another process is already listening on. That would break the
    single-page guarantee: two instances would both think they own the URL.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        exclusive_opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_opt is not None:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive_opt, 1)
            except OSError:
                pass
        super().server_bind()


class ApprovalServer:
    """Serves the shared approval state on one stable localhost URL."""

    def __init__(
        self,
        store: ApprovalStore,
        port: int = 8765,
        open_browser: bool = True,
        takeover_interval: float = 5.0,
    ):
        self.store = store
        self.base_port = port
        self.port = port
        self.open_browser = open_browser
        self.takeover_interval = takeover_interval
        self._httpd: _ExclusiveHTTPServer | None = None
        self._lock = threading.Lock()
        self._opened_once = False
        self._stop = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def owns_page(self) -> bool:
        return self._httpd is not None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> bool:
        """Take the page if it is free, otherwise confirm a peer already serves it."""
        if self._try_bind():
            if self.open_browser and not self._opened_once:
                self._open_browser_once()
            self._start_takeover_watch()
            return True

        peer = self._probe_peer(self.base_port)
        if peer == "ours":
            log.info(
                "Approval page already served by another planning-mcp instance on %s; "
                "publishing to the shared state it reads",
                self.url,
            )
            self._start_takeover_watch()
            return True

        log.error(
            "Port %s is held by something that is not a planning-mcp approval page for "
            "this state directory. The approval page is unavailable.",
            self.base_port,
        )
        return False

    def _try_bind(self) -> bool:
        with self._lock:
            if self._httpd is not None:
                return True
            try:
                httpd = _ExclusiveHTTPServer(("127.0.0.1", self.base_port), self._make_handler())
            except OSError:
                return False
            self._httpd = httpd
            self.port = self.base_port
            threading.Thread(target=httpd.serve_forever, name="approval-ui", daemon=True).start()
            log.info("Approval UI listening on %s", self.url)
            return True

    def _probe_peer(self, port: int) -> str:
        """Is the occupant of `port` a planning-mcp page for our state directory?"""
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                info = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - any failure means "not ours"
            return "unknown"
        if info.get("server") != SERVER_SIGNATURE:
            return "foreign"
        try:
            same = Path(info.get("state_dir", "")).resolve() == self.store.state_dir.resolve()
        except OSError:
            same = False
        return "ours" if same else "other-state-dir"

    def _start_takeover_watch(self) -> None:
        """Keep trying to own the page so a dead owner is replaced automatically."""
        if self._httpd is not None:
            return  # we already own it
        def watch() -> None:
            while not self._stop.wait(self.takeover_interval):
                if self._try_bind():
                    log.warning("Took over the approval page on %s", self.url)
                    return
        threading.Thread(target=watch, name="approval-takeover", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    # ---- request flow --------------------------------------------------
    def open_request(
        self, plan_id: str, goal: str, display: str, tasks: list[dict[str, Any]],
        fingerprint: str = "",
    ) -> str | None:
        request_id = self.store.publish(plan_id, goal, display, tasks, fingerprint)
        self._surface()
        return request_id

    def claim(self, request_id: str) -> tuple[str, str] | None:
        return self.store.claim(request_id)

    def take_decision(self, plan_id: str, fingerprint: str) -> tuple[str, str] | None:
        return self.store.claim_for_plan(plan_id, fingerprint)

    def _surface(self) -> None:
        log.warning("HUMAN APPROVAL NEEDED -> %s", self.url)
        if self.open_browser:
            self._open_browser_once()

    def _open_browser_once(self) -> None:
        """Best-effort only. Corporate policy, a missing default browser, or a second
        monitor can all defeat this, which is why the page also polls."""
        try:
            if webbrowser.open(self.url):
                self._opened_once = True
                return
        except Exception as exc:  # noqa: BLE001
            log.debug("webbrowser.open failed: %s", exc)
        try:
            os.startfile(self.url)  # type: ignore[attr-defined]
            self._opened_once = True
            return
        except Exception:  # noqa: BLE001
            pass
        log.warning("Could not open a browser automatically. Open %s manually.", self.url)

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

            def _json(self, payload: Any, code: int = 200) -> None:
                self._send(
                    code,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/":
                    self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/health":
                    self._json(
                        {"server": SERVER_SIGNATURE, "state_dir": str(server.store.state_dir)}
                    )
                elif path == "/api/pending":
                    self._json({"requests": [
                        {
                            "id": e["id"],
                            "plan_id": e.get("plan_id"),
                            "goal": e.get("goal"),
                            "display": e.get("display"),
                            "decided": e.get("decision"),
                        }
                        for e in server.store.peek()
                    ]})
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
                ok = server.store.record_decision(
                    str(body.get("id", "")),
                    str(body.get("decision", "")).upper(),
                    body.get("comment", ""),
                )
                self._json({"ok": ok})

        return Handler
