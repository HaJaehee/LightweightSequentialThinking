# planning-mcp (PlanningHarnessMCP)

**버전: 1.15.1** · MCP 서버 이름 `planning-mcp` · 단일 출처: `planning/config.py`의
`SERVER_VERSION` (MCP `initialize` 응답의 `serverInfo.version` 으로 보고됨)

AnythingLLM Agent Mode용 경량 **계획·작업 관리 MCP 서버**. 폐쇄망의 성능이 약한 사내 LLM이
기억에 의존해 바로 답해버리는 대신, `계획 → 사람 승인 → 실행 → 보고` 생애주기를 따르도록
하니스를 씌웁니다.

**의존성 0개.** Python 3.9+ 표준 라이브러리만 사용 — `pip install`이 필요 없습니다.
패키지 저장소가 없는 폐쇄망에서 이것이 결정적입니다.

---

## 왜 만들었나

사내 모델은 OpenAI 호환 API로만 접근 가능하고 다단계 추론과 도구 호출에 약합니다.
그대로 두면 계획과 승인이 먼저여야 할 요청에도 즉시, 자신 있게 답해버립니다.
이 서버는 그것을 구조적으로 어렵게 만듭니다:

- **상태는 서버가 소유합니다.** 모델이 계획을 기억할 필요가 없으므로 계획을 지어낼 수도 없습니다.
- **모든 응답에 `next_action`이 실려 있습니다.** 모델이 스스로 추론해 나아가길 기대하는 대신,
  서버가 모델을 상태머신처럼 구동합니다.
- **승인 게이트는 부탁이 아니라 강제입니다.** 사용자가 실제로 승인하기 전까지
  `update_task_progress`가 진행 기록을 거부하므로, 지시를 무시하는 모델도 실행할 수 없습니다.
- **에이전트 루프를 실제로 멈춥니다.** `request_user_approval`이 사람이 결정할 때까지
  도구 호출을 반환하지 않습니다. 에이전트 루프는 도구 결과를 **동기적으로** 기다리므로,
  모델은 그동안 다른 도구를 부를 수 없습니다. "멈추라고 말하는" 것이 아니라 물리적으로
  멈춥니다.
- **어긋난 입력은 거부하지 않고 수리합니다.** `"done"`, `"3"`, `"true"`, 줄바꿈으로 이어붙인
  작업 문자열 — 전부 검증 전에 정규화됩니다.
- **작업을 건너뛸 수 없습니다.** `DONE` 은 시작 기록·순서·실질적 `result_log` 가 모두
  있어야 받아들여지고, 마지막 작업까지 끝나도 **사람이 작업별 증거를 확인해야** 계획이
  완료됩니다. 계획만 세우고 실행을 빠뜨리는 소형 모델의 전형적 오작동을 구조적으로 막습니다.

---

## 빠른 시작

```bash
python -m unittest discover -s tests
```

```bash
python tests/smoke_stdio.py
```

패키지에 인터프리터가 동봉되어 있다면 **등록보다 먼저** 압축을 푸십시오 — 아카이브는
python.org zip을 원본 그대로 담고 있어서, 이걸 실행하기 전에는 `runtime/python.exe`가
존재하지 않습니다 (이 단계를 건너뛰면 `spawn ... python.exe ENOENT`가 납니다):

```bash
python tools/setup_runtime.py
```

그다음 AnythingLLM에 서버를 등록합니다 (**Agent Skills → MCP Servers**; 이 화면에
`anythingllm_mcp_servers.json`의 실제 위치가 표시됩니다) —
[anythingllm_mcp_servers.example.json](anythingllm_mcp_servers.example.json) 참고:

```json
{
  "mcpServers": {
    "planning": {
      "command": "D:/planning-mcp/runtime/python.exe",
      "args": ["-u", "D:/planning-mcp/server.py"],
      "env": { "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1" },
      "anythingLLMAware": true
    }
  }
}
```

`command`는 `tools/setup_runtime.py`가 풀어놓은 동봉 인터프리터를 가리킵니다. 동봉하지
않았다면 설치된 Python의 절대 경로를 대신 적으십시오 (`(Get-Command python).Source`로 확인).
슬래시는 `/`, 경로는 반드시 절대 경로 — AnythingLLM이 상속하는 PATH는 터미널의 PATH와
다릅니다. 저장 후 AnythingLLM을 완전히 종료했다가 다시 실행하십시오.

MCP 도구는 Agent Mode에만 노출되므로, 워크스페이스가 기본으로 에이전트 모드가 아니라면
메시지를 `@agent`로 시작해야 합니다. 전체 절차:
[docs/deployment-airgap-manual.md](docs/deployment-airgap-manual.md) 6절.

마지막으로 [docs/phase3-anythingllm-agent-prompt.md](docs/phase3-anythingllm-agent-prompt.md)의
에이전트 시스템 프롬프트를 워크스페이스 에이전트 설정에 붙여넣고, **temperature ≤ 0.3**으로
설정하고, 초기 안정화 기간에는 다른 기본 에이전트 스킬을 비활성화하십시오 — 보이는 도구가
적을수록 잘못된 도구 호출이 급감합니다.

---

## 시스템 프롬프트 

```
<system_directive>
<role>
You are a background AI agent that autonomously utilizes tools integrated into the system to resolve user requests.
</role>
<core_workflow>
1. PLAN: You MUST call the `plan_and_think` tool before answering ANY user request or executing any task.
2. APPROVE: You MUST call `request_user_approval` (with decision="ASK_USER") after generating the complete plan.
3. WAIT: After calling `request_user_approval`, STOP GENERATING TEXT IMMEDIATELY. Output only the plan summary to the user and wait for approval. If the response is `APPROVAL_PENDING`, the user has NOT answered yet - call the tool again at once (see its Usage Rules) and output nothing.
4. EXECUTE: Read the `next_action` field in every tool response and OBEY IT LITERALLY to execute tasks only after approval.
5. VERIFY: After the LAST task is DONE, call `request_user_approval` (decision="ASK_USER") again so the user can confirm the completion report. Only the user may declare a plan finished.
6. REWORK: If the user sends specific tasks back from that completion report, the plan is NOT being replanned. Do NOT call `plan_and_think` and do NOT ask for approval again - the plan is unchanged and still approved. Redo ONLY the tasks named in `next_action_hint`, then report completion again.
</core_workflow>
<tool_protocol name="plan_and_think">
Usage Rules:
- Call once per step, starting at step_number = 1.
- Set `need_more_thinking = true` for intermediate steps.
- Set `need_more_thinking = false` AND provide `task_list` on your FINAL step.
- Set `revises_step = [step number]` to correct a previously generated step.
- Repeat the SAME `goal` text on every step. If the USER corrects the goal itself ("I meant Q4, not Q3"), keep sending the old text as `goal` and send the corrected one as `revised_goal` - do NOT start a second plan. Use the corrected text from the next call on. Never use `revised_goal` to merely reword the same goal.
- DO NOT execute tasks or answer the user directly while thinking.
- TARGETED REVISION: if the server reports that the user commented on specific tasks, send `task_updates` (NOT `task_list`) on your final step, rewriting ONLY those tasks. Every other task was already accepted by the user - do not change, renumber, reorder or resend it. `next_action_hint` contains the exact argument to send.
</tool_protocol>
<tool_protocol name="request_user_approval">
Usage Rules:
- `decision = "ASK_USER"` is the ONLY value you may send on your own initiative. It asks the user; it does not answer for them.
- NEVER send `APPROVED`, `REJECTED` or `REVISE` unless the server has told you the user chose it. You are not permitted to decide on the user's behalf, and the server refuses it with `APPROVAL_PENDING`.
- `plan_summary` is MANDATORY with `ASK_USER`: a plain-language overview of how you intend to reach the goal.
- WAITING LOOP: an `APPROVAL_PENDING` response means the user is still deciding. Immediately call `request_user_approval` again with `decision = "ASK_USER"` and the SAME `plan_summary`. Output no text between attempts, do not re-print the plan, do not ask the user anything, and do not start any work. Keep repeating until the response changes - `remaining_seconds` tells you how much time is left.
- When the user answers, the server applies it for you: `plan_status` becomes `APPROVED` (start executing), `CANCELLED` (stop), or `DRAFTING` (revise as `next_action_hint` describes).
- If the response instead hands you `display_to_user`, the wait is over and nobody answered. Show the plan to the user and STOP.
</tool_protocol>
<tool_protocol name="update_task_progress">
Usage Rules:
- You MUST complete EVERY task in the plan, one at a time, in order.
- ALWAYS take `task_id` from the `next_task` field of the server's MOST RECENT response. `next_task` is the only place the server publishes a task_id. Never reuse a task_id from an earlier response and never count tasks yourself - earlier responses named tasks that are already finished.
- The response carries `progress` ("2/5 done") but NOT the task list. If you have lost track of the plan, call `get_current_plan` - that is what it is for. Do not guess.
- Send `status = "IN_PROGRESS"` for the FIRST task only. Then do the real work, then send `status = "DONE"`.
- The server then starts the NEXT task itself and names it in `next_task`. Do that work and send `status = "DONE"` for it too. DO NOT send IN_PROGRESS again - one call per task from here on. A 5-task plan is 1 IN_PROGRESS call and 5 DONE calls.
- `result_log` is MANDATORY for DONE and must state the CONCRETE outcome: what you produced, found, or saved.
- These are REJECTED as evidence: "done", "ok", "완료", or repeating the task title. If you cannot write a real outcome, the task is NOT done.
- The server REFUSES a false DONE: TASK_NOT_STARTED (that task is not the one in progress), TASK_OUT_OF_ORDER (an earlier task is unfinished), MISSING_RESULT_LOG (no evidence), REWORK_NOT_DONE (you re-sent the very outcome the user rejected). Fix the cause and retry that task.
- NEVER mark tasks DONE in a batch. NEVER tell the user the work is finished while `next_action_hint` still reports remaining tasks.
- REWORK: a task that comes back with `revision_note` in `next_task` was rejected by the user AFTER you finished it. Its `previous_result_log` is what you produced last time - it was not good enough. Do the work AGAIN so that it answers what the user said, and write a `result_log` describing the NEW outcome. Do not resend the old one. Tasks still marked DONE were accepted: do not redo, rewrite or re-report them.
</tool_protocol>
<tool_protocol name="get_current_plan">
Usage Rules:
- Call it whenever you are unsure what the plan is or which task you were on. It changes nothing and is always safe.
- SEND YOUR OWN `plan_id`: every server response carries a `plan_id` field - send that exact value. Other conversations may be running their own plans at the same time, and your `plan_id` is the only thing that identifies yours.
- Send `"current"` ONLY if you do not know your `plan_id` yet. It is a guess: if several plans are in flight the server cannot tell which is yours and answers with an `active_plans` list instead.
- If you get that list back, do NOT start a new plan. Call again with your own `plan_id` from the list.
</tool_protocol>
<strict_constraints>
- SYNTAX: Use pure JSON format with standard double quotes (") for tool calls.
- RESPONSE: Always follow the returned `next_action` field.
- LANGUAGE: Think step-by-step strictly in English. Output the final result in Korean.
</strict_constraints>
</system_directive>

```

---

## 도구 4개

| 도구 | 역할 |
|---|---|
| `plan_and_think` | 필수 진입점. 호출당 사고 1스텝, 마지막 호출에 `task_list` 제출. 태스크별 수정 요청에는 `task_updates`, 사용자가 목표 자체를 정정하면 `revised_goal` |
| `request_user_approval` | HITL 게이트. `ASK_USER` → 정지 → `APPROVED` / `REJECTED` / `REVISE` |
| `update_task_progress` | 첫 작업만 `IN_PROGRESS`, 이후는 작업당 `DONE`/`FAILED` 1회 (다음 작업은 서버가 시작). 미승인 시 거부 |
| `get_current_plan` | 컨텍스트 절단 후 복구. 언제 호출해도 안전. 자기 `plan_id`를 보내면 항상 자기 플랜을 받음 |

전체 스키마와 응답 계약: [docs/phase1-tool-schema-blueprint.md](docs/phase1-tool-schema-blueprint.md)

모든 도구의 모든 응답에는 `ok`, `plan_status`, `next_action`, `next_action_hint`가
항상 포함됩니다.

---

## 구조

```
server.py                 진입점: 트랜스포트 연결 + 도구 등록
planning/
  schemas.py              도구 4개의 스키마 (models.py의 enum에서 생성)
  models.py               Plan / Task / ThinkingStep + 전체 enum
  store.py                원자적 JSON 영속화 + append-only 감사 로그
  leniency.py             입력 수리 (대소문자, 별칭, 타입, task_list 형태)
  state_machine.py        전이 규칙 + 유일한 next_action 결정자
  handlers.py             도구 4개의 구현
  responses.py            유일한 응답 빌더
  protocol.py             최소 MCP / JSON-RPC 2.0
  transport.py            stdio (기본), SSE (선택, 루프백 전용)
state/                    런타임: plan_state.json, audit.jsonl  (gitignore 대상)
tests/                    유닛 스위트 + stdio 종단 스모크 테스트
docs/                     Phase 1~4: 스키마, 아키텍처, 에이전트 프롬프트, 테스트 매트릭스
```

---

## 설정

전부 선택 사항이며, 기본값이 안전합니다.

| 환경 변수 | 기본값 | 용도 |
|---|---|---|
| `PLANNING_MCP_STATE_DIR` | `<프로젝트>/state` | 상태 파일 위치 변경 |
| `PLANNING_MCP_LOG_LEVEL` | `INFO` | stderr 로그 상세도 |
| `PLANNING_MCP_MAX_PLANS` | `20` | 오래된 계획 정리 전 보존 개수 |
| `PLANNING_MCP_MAX_TASKS` | `12` | 초과 작업 목록은 거부 대신 잘라냄 |
| `PLANNING_MCP_BLOCKING_APPROVAL` | `true` | 승인 도구가 사람이 결정할 때까지 반환을 보류 (에이전트 루프 정지). `false`면 "멈춰라"고 말만 함 |
| `PLANNING_MCP_APPROVAL_PORT` | `8765` | 승인 페이지 포트 (127.0.0.1 전용) |
| `PLANNING_MCP_SSE_PORT` | `8931` | SSE 트랜스포트 포트 (`--transport sse` 일 때). CLI `--port` 가 우선 |
| `PLANNING_MCP_SSE_HOST` | `127.0.0.1` | SSE 바인드 주소. 루프백 외 주소는 코드가 거부 |
| `PLANNING_MCP_APPROVAL_TIMEOUT` | `900` | 사람이 결정하기를 기다리는 **전체** 상한(초). 여러 번의 도구 호출에 걸쳐 누적되며, 승인 요청이 처음 뜬 시각부터 잰다 |
| `PLANNING_MCP_CALL_BUDGET` | `45` | **도구 호출 1회**가 붙잡고 있을 수 있는 최대 시간(초). 클라이언트 대부분이 60초에 호출을 끊고 결과를 버리므로 그 아래로 유지해야 한다. 실제로 취소당한 적이 있으면 그 값보다 더 줄어든다 |
| `PLANNING_MCP_APPROVAL_MODE` | `chunked` | `chunked`: 45초씩 끊어 대기하고 모델에게 즉시 재호출을 지시 (승인 후 사용자 입력 없이 대화가 이어짐). `return`: 요청만 띄우고 바로 반환 (승인 후 채팅에 메시지를 한 번 보내야 진행). `trust_heartbeat`: 1.13 이전 방식으로 한 번에 길게 대기 — progress 알림이 타이머를 리셋한다고 **측정된** 클라이언트에서만 |
| `PLANNING_MCP_APPROVAL_OPEN_BROWSER` | `true` | 승인 요청 시 브라우저 자동 실행 |
| `PLANNING_MCP_APPROVAL_TTL` | `1800` | 승인 유효 시간(초). 이만큼 방치된 계획은 승인이 만료되어 재승인이 필요 |
| `PLANNING_MCP_MAX_ACTIVE_PLANS` | `5` | 동시에 진행할 수 있는 계획 수 상한 |
| `PLANNING_MCP_COMPLETION_APPROVAL` | `true` | 마지막 작업이 DONE 되어도 바로 완료되지 않고, 사람이 작업별 증거를 확인해야 COMPLETED |
| `PLANNING_MCP_MIN_RESULT_LOG` | `8` | DONE 에 필요한 최소 증거 길이(공백·문장부호 제외). 상투어구("완료", "done")는 길이와 무관하게 거부 |
| `PLANNING_MCP_AUTO_ADVANCE` | `true` | 작업을 DONE 보고하면 다음 작업을 서버가 `IN_PROGRESS`로 시작. 5개 작업 기준 실행 호출 10회 → 6회. `false`면 작업마다 `IN_PROGRESS`를 직접 보내야 함 (도구 설명도 그에 맞게 바뀜) |
| `PLANNING_MCP_AUTOAPPROVE` | `false` | **테스트 전용** — HITL 게이트 우회. 호출마다 경고 로그 |

CLI: `--transport stdio|sse`, `--host`, `--port`, `--state-dir`, `--log-level`

---

## 에이전트가 무엇을 했는지 확인하기

`state/plan_state.json`은 사람이 읽을 수 있습니다 — 열어 보면 에이전트가 지금 무엇을
한다고 생각하는지 그대로 보입니다. `state/audit.jsonl`은 한 줄에 JSON 객체 하나인
append-only 증거 기록입니다:

```
plan_created → thinking_step → execution_blocked → plan_finalized →
approval_requested → approved → task_started → task_done → task_failed
```

`execution_blocked` 항목은 강제 게이트가 조기 실행 시도를 막은 기록입니다 — 모델이
승인을 건너뛰려 한 것 같다면 가장 먼저 확인할 곳입니다.

---

## Windows / AnythingLLM 주의사항

- `python -u`로 실행하십시오 (또는 `PYTHONUNBUFFERED=1`). 출력 버퍼링이 걸리면 stdio
  서버가 멈춘 것처럼 보입니다.
- `PYTHONUTF8=1`을 설정하십시오. 한글 `user_comment` / `result_log`가 cp949 인코딩
  오류를 내지 않게 합니다.
- MCP JSON 설정에서 슬래시는 `/`를 쓰십시오.
- 상태 디렉터리는 작업 디렉터리가 아니라 `planning/config.py` 기준으로 결정됩니다 —
  AnythingLLM은 자기만의 CWD로 서버를 띄웁니다.
- stdio에서는 기동 시 `sys.stdout`을 stderr로 재지정하므로, 어디선가 `print()`가
  새어나가도 JSON-RPC 스트림이 오염되지 않습니다.

증상 → 원인 → 해결 표: [docs/phase4-testing-matrix.md](docs/phase4-testing-matrix.md) Part E

---

## 문서

| Phase | 문서 |
|---|---|
| 1 | [도구 인터페이스·스키마 청사진](docs/phase1-tool-schema-blueprint.md) |
| 2 | [로컬 서버 아키텍처](docs/phase2-server-architecture.md) |
| 3 | [AnythingLLM 에이전트 시스템 프롬프트](docs/phase3-anythingllm-agent-prompt.md) |
| 4 | [테스트·트러블슈팅 매트릭스](docs/phase4-testing-matrix.md) |
| — | [폐쇄망 반입·배포 매뉴얼](docs/deployment-airgap-manual.md) |

## 반입용 패키징

```bash
python tools/make_package.py
```

`dist/planning-mcp-<버전>-<날짜>.zip` (약 90 KB, 전부 평문)과 파일별 SHA-256이 담긴
`MANIFEST.txt`를 생성합니다.

도착지 PC에 Python이 아예 없어도 되게 하려면 python.org 공식 임베디드 배포판을
동봉하십시오 — 원본 그대로 담기므로 체크섬을 python.org 공개값과 대조해 검증할 수
있습니다:

```bash
python tools/make_package.py --with-python C:\dl\python-3.12.10-embed-amd64.zip
```

도착지 PC에서는 (동봉본이 있다면 `tools/setup_runtime.py`로 인터프리터를 먼저 풀고):

```bash
python tools/verify_install.py
```

Python 버전, 파일 무결성, 표준 라이브러리 전용 여부, 유닛 스위트, stdio 스모크 테스트를
검사한 뒤 **GO / NO-GO** 판정을 출력합니다. 전체 절차:
[docs/deployment-airgap-manual.md](docs/deployment-airgap-manual.md)
