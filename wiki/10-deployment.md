# 10 · Deployment (Air-Gapped Transfer)

Full step-by-step (Korean, with security-review material) is in
`docs/deployment-airgap-manual.md`. This page is the English summary for an AI.

## The shape of the problem

The target is an isolated corporate Windows PC. What we ship is **plain-text Python, ~110 KB,
zero dependencies** — a big asset for security review, so never obscure it (no encrypted zips,
no renamed extensions). The only real variable is the Python interpreter, which can be bundled.

## Tooling (`tools/`)

| Tool | Purpose |
|---|---|
| `make_package.py` | Builds `dist/planning-mcp-<ver>-<date>.zip` (allow-list of plain-text files) + `MANIFEST.txt` of per-file SHA-256. `--with-python <embed.zip>` bundles the official python.org embeddable distribution **verbatim** (stored, not recompressed) so its checksum can be verified against python.org. |
| `setup_runtime.py` | On the target: unpacks the bundled interpreter and patches `python3xx._pth` (adds `..`) so `import planning` resolves under isolated mode. Idempotent; self-repairs a BOM-damaged or re-extracted `._pth`. Refuses to overwrite a running interpreter. |
| `verify_install.py` | GO/NO-GO acceptance: Python version, UTF-8, writable state dir, MANIFEST integrity, stdlib-only imports, `._pth` patched & BOM-free, then the unit suite + all 5 smoke tests. |

## Build → transfer → install

**On the build machine:**
```bash
python -m unittest discover -s tests
python tests/smoke_stdio.py
python tools/make_package.py --with-python C:\dl\python-3.12.10-embed-amd64.zip
```
Record the archive SHA-256 **outside** the archive (a note/message). If bundling Python, also
record the python.org checksum for the embeddable zip.

**On the target:**
```powershell
Get-FileHash .\planning-mcp-*.zip -Algorithm SHA256      # compare before unpacking
Expand-Archive .\planning-mcp-*.zip -DestinationPath D:\
Get-ChildItem -Recurse D:\planning-mcp | Unblock-File     # clear Mark-of-the-Web
# if Python bundled and none installed: unpack the interpreter with Windows, then:
D:\planning-mcp\runtime\python.exe D:\planning-mcp\tools\setup_runtime.py
D:\planning-mcp\runtime\python.exe D:\planning-mcp\tools\verify_install.py   # expect GO
```

Then register the MCP server in AnythingLLM (**Agent Skills → MCP Servers**;
`anythingllm_mcp_servers.json`), paste the agent prompt from
`docs/phase3-anythingllm-agent-prompt.md`, set **temperature ≤ 0.3**, and disable other agent
skills during bring-up. Example config: `anythingllm_mcp_servers.example.json`.

```json
{ "mcpServers": { "planning": {
  "command": "D:/planning-mcp/runtime/python.exe",
  "args": ["-u", "D:/planning-mcp/server.py"],
  "env": { "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1" },
  "anythingLLMAware": true } } }
```
Forward slashes, absolute paths, never omit `-u`. Restart AnythingLLM fully after saving.

## Security-review facts (verifiable in the code)

- **No outbound network calls** (grep confirms; `urllib.parse` is string parsing only).
- **Inbound only:** the approval page listens on `127.0.0.1:8765` (loopback), serving one HTML
  page + 3 JSON endpoints, no filesystem serving, **no authentication** (loopback = local user
  only; disable blocking approval on a shared PC). SSE mode adds `127.0.0.1:8931`.
- **No `eval`/`exec`/`os.system`/`subprocess`** in the server runtime. (`subprocess` appears only
  in test/tooling scripts — flag this proactively to reviewers.)
- **File access:** the state directory only.
- **Plan contents are stored plaintext** in `state/plan_state.json` (by design, for auditability).
  Put the state dir on an encryption-policy path; set retention for `state/audit.jsonl`.
- Bundled interpreter (if used) is the official python.org embeddable, verbatim; the only local
  modification is one `..` line in `._pth`, visible in the file.

## Windows / AnythingLLM notes

- `python -u` (or `PYTHONUNBUFFERED=1`) — buffered stdout makes stdio look hung.
- `PYTHONUTF8=1` — Korean `user_comment`/`result_log` vs cp949.
- State dir resolves from `planning/config.py`, not CWD — AnythingLLM spawns with its own CWD.
- Multiple workspaces → one server registration each with distinct `PLANNING_MCP_STATE_DIR`
  (separate state dirs are fully isolated).
