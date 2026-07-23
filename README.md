# planning-mcp (LightweightSequentialThinking)

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
- **어긋난 입력은 거부하지 않고 수리합니다.** `"done"`, `"3"`, `"true"`, 줄바꿈으로 이어붙인
  작업 문자열 — 전부 검증 전에 정규화됩니다.

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

## 도구 4개

| 도구 | 역할 |
|---|---|
| `plan_and_think` | 필수 진입점. 호출당 사고 1스텝, 마지막 호출에 `task_list` 제출 |
| `request_user_approval` | HITL 게이트. `ASK_USER` → 정지 → `APPROVED` / `REJECTED` / `REVISE` |
| `update_task_progress` | 작업마다 `IN_PROGRESS` 선행, 종료 시 `DONE`/`FAILED`. 미승인 시 거부 |
| `get_current_plan` | 컨텍스트 절단 후 복구. 언제 호출해도 안전 |

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
