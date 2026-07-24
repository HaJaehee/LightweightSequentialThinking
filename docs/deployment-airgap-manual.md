# 폐쇄망 반입·배포 매뉴얼

개인 PC(`D:\LightweightSequentialThinking`)에서 만든 planning-mcp를 사내 폐쇄망 PC로
옮겨 AnythingLLM에 붙이기까지의 전 과정.

---

## 0. 전제와 범위

**이 문서가 다루는 것**
- 무엇을 가져가고 무엇을 두고 갈지
- 반입 패키지를 만들고, 도착 후 사본이 온전한지 증명하는 방법
- 사내 PC에서의 설치·검증·등록 절차
- 보안 검토 요청서에 첨부할 사실 자료

**이 문서가 다루지 않는 것**
- 회사의 반입 정책 자체. 매체 반입, 망연계, 소프트웨어 승인은 **회사 절차를 그대로 따르십시오.**
  이 문서는 그 절차를 우회하는 방법을 제공하지 않으며, 오히려 심사에 필요한 자료를 미리 갖추는 데
  초점이 있습니다.

**핵심 사실 3가지** — 반입 난이도를 결정합니다.

| | |
|---|---|
| 크기 | 약 **80 KB**, 파일 25개 (인터프리터 동봉 시 약 **11 MB**) |
| 형식 | 소스는 **전부 평문** `.py` / `.md` / `.json` |
| 의존성 | **0개**. Python 표준 라이브러리만 사용 → 사내에서 `pip install` 불필요 |

즉 반입 대상은 "사람이 읽을 수 있는 텍스트 80KB"입니다. 이 점이 심사에서 가장 큰 자산이므로,
압축 암호화나 확장자 변경으로 내용을 가리지 마십시오.

패키지는 **두 가지 형태**로 만들 수 있습니다:

- **기본** — 소스만. 사내 PC에 Python 3.9+ 가 이미 있어야 합니다.
- **인터프리터 동봉** — python.org 공식 임베디드 배포판을 함께 넣습니다. **사내 PC에 Python이
  없어도, 설치 권한이 없어도** 동작합니다. 자세한 내용은 3절.

---

## 1. 반입 대상 목록

### 가져가는 것 (패키지 스크립트가 자동 선별)

| 경로 | 내용 |
|---|---|
| `server.py` | 진입점 |
| `planning/*.py` | 서버 구현 10개 모듈 |
| `tests/*.py` | 단위 테스트 + stdio 종단 스모크 테스트 |
| `tools/*.py` | 패키징·검증 스크립트 |
| `docs/*.md` | Phase 1~4 설계 문서 + 이 매뉴얼 |
| `README.md`, `.gitignore` | |
| `anythingllm_mcp_servers.example.json` | AnythingLLM 등록 예시 |
| `MANIFEST.txt` | 파일별 SHA-256 (패키징 시 자동 생성) |
| `runtime/` | **`--with-python` 사용 시에만.** python.org 공식 임베디드 배포판 zip(원본 그대로) + 출처 설명 `RUNTIME.md` |

### 두고 가는 것 — 그리고 그 이유

| 제외 | 이유 |
|---|---|
| `state/` | **개인 PC에서 테스트한 실제 계획 내용**이 평문으로 들어 있습니다. 사내에서 새로 생성되므로 옮길 이유가 없고, 옮기면 불필요한 데이터가 반입 심사 대상이 됩니다 |
| `__pycache__/` | 컴파일된 `.pyc`는 검토자 눈에 불투명한 바이너리로 보입니다. 평문만 반입한다는 장점을 스스로 깎는 셈 |
| `.git/` | 커밋 이력에 로컬 경로·계정명·삭제한 파일이 남습니다 |
| `dist/` | 산출물 자기 자신 |

### 판단이 필요한 것: `CLAUDE.md`

프로젝트 지침서라 사내에서 유지보수할 때 유용하지만, 외부 AI 도구로 설계했다는 사실이
그대로 드러납니다. **회사의 AI 도구 사용 정책을 먼저 확인하십시오.** 패키지 스크립트는
기본적으로 제외합니다. 포함하려면 `tools/make_package.py`의 `INCLUDE_FILES`에 추가하십시오.

---

## 2. 개인 PC에서 할 일 — 패키지 만들기

### 2-1. 반입 전 최종 검증

깨진 것을 반입하면 사내에서 디버깅해야 하는데, 그곳엔 인터넷도 검색도 없습니다.
반드시 여기서 통과시키십시오.

```bash
python -m unittest discover -s tests
```

```bash
python tests/smoke_stdio.py
```

각각 `Ran 49 tests ... OK`, `All smoke checks passed.` 가 나와야 합니다.

### 2-2. 패키지 생성

```bash
python tools/make_package.py
```

`dist/planning-mcp-1.0.0-<날짜>.zip` 과 루트의 `MANIFEST.txt`가 만들어지고,
마지막 줄에 아카이브 전체의 SHA-256이 출력됩니다.

### 2-3. 체크섬을 아카이브 **밖에** 기록

이게 반입 과정에서 가장 자주 빠지는 단계입니다. 해시가 아카이브 안에만 있으면
무결성 검증이 성립하지 않습니다. 출력된 SHA-256을 **다른 경로로** 옮기십시오 —
메모지, 사내 메신저, 종이에 적어 사진 촬영 등.

사내 PC에서 압축 해제 **전에** 비교:

```powershell
Get-FileHash .\planning-mcp-1.0.0-20260724.zip -Algorithm SHA256
```

값이 다르면 전송이 손상됐거나 검사 시스템이 파일을 변형한 것입니다. 풀지 말고 다시 반입하십시오.

---

## 3. 사내 PC의 Python — 동봉이 기본

의존성이 0개이므로 남는 변수는 **인터프리터 하나뿐**입니다. 그리고 그 하나마저 동봉할 수
있습니다. **사내 PC에 Python이 설치되어 있지 않아도, 설치 권한이 없어도 동작합니다.**

### 3-0. 두 가지 방식

| | 시스템 Python 사용 | **인터프리터 동봉 (권장)** |
|---|---|---|
| 패키지 크기 | 약 80 KB | 약 **11 MB** |
| 사내 PC 요구사항 | Python 3.9+ 설치되어 있어야 함 | **없음** |
| 관리자 권한 | 설치가 필요하면 필요 | 불필요 (압축 해제만) |
| 반입 심사 | 평문만 | 평문 + 공식 바이너리 1개 |
| 다른 업무 환경 영향 | 시스템 Python 공유 | 완전 격리, 폴더 삭제로 완전 제거 |

사내 PC에 Python이 있는지 **확실하지 않다면 동봉하십시오.** 없다는 걸 사내에서 알게 되면
반입 절차를 처음부터 다시 밟아야 하는데, 11 MB를 아끼려다 며칠을 잃습니다.

먼저 확인하려면 사내 PC에서:

```powershell
python --version
```

```powershell
py -0
```

### 3-1. 임베디드 배포판 받기 (개인 PC, 인터넷 필요)

python.org → Downloads → Windows → **"Windows embeddable package (64-bit)"**
파일명은 `python-3.12.x-embed-amd64.zip` 형태이며 약 11 MB입니다.

받은 직후 **다운로드 페이지에 공개된 SHA-256을 기록**하십시오. 보안 검토에서 인터프리터의
출처를 증명하는 근거가 됩니다.

```powershell
Get-FileHash .\python-3.12.10-embed-amd64.zip -Algorithm SHA256
```

> **왜 PyInstaller 단일 exe가 아닌가**
> 코드 전체가 불투명한 바이너리 덩어리가 되어 보안 검토가 훨씬 어려워지고, 사내 EDR에서
> 오탐 차단되는 사례가 흔합니다. 임베디드 방식은 **인터프리터만 공식 바이너리이고 우리
> 소스는 평문 그대로** 남으므로, 검토자가 실제로 동작을 읽어 확인할 수 있습니다.

### 3-2. 인터프리터를 포함해 패키징 (개인 PC)

```bash
python tools/make_package.py --with-python C:\dl\python-3.12.10-embed-amd64.zip
```

`dist/planning-mcp-1.0.0-<날짜>-with-python.zip` 이 생성됩니다.

패키징 스크립트는 embed zip을 **원본 그대로(verbatim, 무압축 저장)** 넣습니다. 풀지도,
다시 압축하지도, 수정하지도 않습니다. 그래야 검토자가 python.org 공개 체크섬과 직접
대조해 인터프리터가 변조되지 않았음을 확인할 수 있습니다. 출처 정보는 아카이브 안
`runtime/RUNTIME.md`에 자동으로 기록됩니다.

임베디드 배포판이 맞는지도 검증합니다 (`python.exe`, 표준 라이브러리 zip, `._pth` 존재).
엉뚱한 파일을 지정하면 패키징이 거부됩니다.

### 3-3. 사내 PC에서 런타임 준비

**Python이 하나라도 있는 경우** — 한 줄이면 됩니다:

```powershell
python tools\setup_runtime.py
```

**Python이 전혀 없는 경우** — Windows 기본 기능으로 인터프리터만 꺼낸 뒤, 그 인터프리터가
나머지를 스스로 마무리합니다:

```powershell
Expand-Archive .\runtime\python-3.12.10-embed-amd64.zip -DestinationPath .\runtime
```

```powershell
.\runtime\python.exe tools\setup_runtime.py
```

이 두 단계가 실제로 검증된 경로입니다. 이후 **모든 명령에서 `runtime\python.exe`를**
사용하십시오.

준비 과정에서 하는 일은 두 가지뿐입니다:
1. embed zip을 `runtime/`에 압축 해제
2. `python312._pth` 파일에 `..` **한 줄 추가**

2번이 필요한 이유: 임베디드 배포판은 **격리 모드**로 동작해 스크립트 폴더를 `sys.path`에
넣지 않고 `PYTHONPATH`도 무시합니다. 이 한 줄이 없으면 `import planning`이 실패합니다.
(추가된 줄은 텍스트 파일에 그대로 보이므로 검토자가 확인할 수 있습니다. `server.py`에도
같은 문제에 대한 이중 방어가 들어 있습니다.)

### 3-4. 동봉하지 않고 기존 Python을 쓰는 경우

**A. 사내 소프트웨어 카탈로그 / 정식 승인 경로**

시간이 걸리므로 **코드 반입 신청과 동시에** 요청하십시오.

**B. 다른 업무 도구가 끼워 설치한 Python 활용**

```powershell
Get-ChildItem C:\ -Recurse -Filter python.exe -ErrorAction SilentlyContinue -Depth 4 |
  Select-Object -First 10 FullName
```

찾았다면 그 절대 경로를 AnythingLLM 설정의 `command`에 적으면 됩니다. 단, 다른 제품에 딸린
인터프리터를 쓰는 것이 사내 정책상 허용되는지 확인하십시오.

---

## 4. 반입 매체와 보안 검사

회사 절차를 따르되, 다음은 실무적으로 자주 걸리는 부분입니다.

- **압축 파일이 차단되는 경우**가 있습니다. 이때 확장자를 바꾸거나 암호를 걸어 검사를
  통과시키려 하지 마십시오. 보안 위반이며, 애초에 이 코드에는 숨길 내용이 없습니다.
  차단되면 담당 부서에 **평문 소스 24개 파일**이라는 점을 밝히고 허용된 방법을 문의하십시오.
- **`.py` 파일 자체가 스크립트로 분류되어 차단**될 수 있습니다. 이 역시 정식 문의 대상입니다.
  Phase 1~4 설계 문서(`docs/*.md`)를 함께 제출하면 용도 설명이 쉬워집니다.
- **Mark of the Web**: 인터넷·메일·외부 매체를 거친 파일은 Windows가 차단 표시를 붙여
  실행을 막을 수 있습니다. 압축 해제 후 한 번 해제하십시오.

  ```powershell
  Get-ChildItem -Recurse -Path .\planning-mcp | Unblock-File
  ```

- **백신/EDR**: AnythingLLM이 `python.exe`를 자식 프로세스로 띄우고 파이프로 통신하는 동작이
  차단될 수 있습니다. 반입 신청 시 이 동작을 미리 명시해 두면 나중에 원인 파악이 빨라집니다.

---

## 5. 보안 검토 요청서용 자료

아래는 코드에서 검증 가능한 사실입니다. 검토자가 직접 확인할 수 있도록 확인 명령을 함께 적었습니다.

| 항목 | 사실 | 검토자 확인 방법 |
|---|---|---|
| 외부 통신 | **없음.** 아웃바운드 네트워크 호출이 존재하지 않음 | `Select-String -Path planning\*.py -Pattern "urllib\.request\|socket\|http\.client\|requests"` → `urllib.parse`(URL 문자열 파싱)만 검출 |
| 수신 대기 | **있음 — 승인 UI가 `127.0.0.1`에 TCP 포트 하나를 엽니다** (기본 8765, 점유 시 8766…8774로 이동). 루프백 전용이며 외부 인터페이스에 바인딩하지 않습니다. SSE 모드를 쓰면 추가로 8931 루프백 | `planning/approval.py`의 `_ExclusiveHTTPServer`, `planning/transport.py`의 `serve_sse()` |
| 승인 UI가 서비스하는 것 | 자기 자신의 HTML 한 장과 JSON 2개(`/api/pending` 조회, `/api/decide` 결정)뿐. 파일 시스템을 서비스하지 않고, 정적 파일 경로도 없습니다 | `planning/approval.py`의 `_PAGE`, `Handler` |
| 승인 UI 인증 | **없음.** 루프백 전용이므로 같은 PC의 로컬 사용자만 접근 가능 | 공용 PC라면 `PLANNING_MCP_BLOCKING_APPROVAL=false`로 끄십시오 |
| 브라우저 실행 | 승인 요청 시 기본 브라우저를 1회 엽니다(`os.startfile` 경유). `PLANNING_MCP_APPROVAL_OPEN_BROWSER=false`로 비활성화 가능하며, 그 경우 URL이 stderr에 기록됩니다 | `planning/approval.py`의 `_surface()` |
| 외부 의존성 | **0개.** Python 표준 라이브러리만 | `tools/verify_install.py` 실행 → "No third-party packages required" |
| 코드 실행 | `eval` / `exec` / `os.system` / `subprocess` **없음** (서버 런타임 기준) | `Select-String -Path planning\*.py -Pattern "eval\(\|exec\(\|os\.system\|subprocess"` → 검출 0건 |
| 파일 접근 | `state/` 디렉터리 **쓰기만**. 그 외 경로 읽기·쓰기·삭제 없음 | `planning/store.py` 전체가 파일 I/O 담당 |
| 외부 전송 데이터 | **없음.** 어떤 데이터도 밖으로 나가지 않음 | 위 "외부 통신" 항목과 동일 |
| 설치 영향 | 레지스트리·서비스·시작프로그램 등록 없음. 폴더 삭제만으로 완전 제거 | |
| 동봉 인터프리터 (해당 시) | python.org **공식 임베디드 배포판을 원본 그대로** 포함. 재압축·수정 없음 | `runtime/RUNTIME.md`의 SHA-256을 python.org 공개 체크섬과 대조 |

> `tests/smoke_stdio.py`와 `tools/setup_runtime.py`는 `subprocess`를 사용합니다. 각각
> 서버를 자식 프로세스로 띄워 검증하고, 꺼낸 인터프리터가 동작하는지 확인하기 위한 것으로
> **서버 런타임 코드가 아닙니다.** 검토자가 지적할 수 있으니 미리 설명해 두십시오.

**승인 UI(내장 웹 서버)에 대해 추가로 밝힐 점**

- **새 의존성은 0개입니다.** 웹 서버는 Python 표준 라이브러리 `http.server`이고, HTML/CSS/JS는
  `planning/approval.py` 안에 문자열 상수로 인라인되어 있습니다. 정적 파일 디렉터리도, 번들러도,
  CDN도 없습니다 — 반입물은 여전히 평문 `.py` 뿐입니다.
- **네트워크 방향은 인바운드 전용입니다.** 이 서버는 요청을 받기만 하고 어디로도 요청을 보내지
  않습니다(아웃바운드 호출 0건은 위 표의 검증 명령으로 확인 가능).
- **방화벽/EDR 정책에 미리 알리십시오.** 프로세스가 리스닝 소켓을 여는 동작이 정책상 신고
  대상일 수 있습니다. 루프백 전용이라 외부에서 접근 불가하다는 점을 함께 명시하면 승인이
  수월합니다.
- 포트가 이미 사용 중이면 서버는 조용히 포기하지 않고 다음 포트로 이동하며, 그마저 실패하면
  **경고를 응답에 실어** 하드 정지가 꺼졌음을 알립니다(감사 로그 `approval_ui_unavailable`).
- 승인 UI는 **서버 시작 시점에** 포트를 엽니다. 바인딩 실패를 로그를 보고 있는 시점에 알 수
  있게 하기 위함이며, 승인 요청 도중에 조용히 무장 해제되는 상황을 막습니다.

### 승인 페이지 운용 (팝업에 의존하지 마십시오)

브라우저 자동 실행은 최선 노력일 뿐입니다 — 사내 정책, 기본 브라우저 부재, 다중 모니터가
모두 이를 무력화합니다. **권장 운용은 탭을 한 번 열어 띄워두는 것입니다:**

1. 서버 시작 로그에서 주소 확인: `APPROVE PLANS AT -> http://127.0.0.1:8765/`
2. 그 주소를 브라우저 탭으로 열고 **그대로 둡니다**
3. 승인 요청이 오면 탭 제목이 `⚠ 승인 대기 중`으로 깜빡이고 짧은 알림음이 납니다

**서두르지 않으셔도 됩니다.** 도구 호출은 최대 55초(클라이언트가 `progressToken`을 보내면
더 길게) 기다렸다가 반환하지만, **승인 창은 응답하실 때까지 사라지지 않습니다.** 시간이
지난 뒤 누르셔도 그 결정은 에이전트의 다음 호출에서 그대로 반영됩니다(감사 로그
`late_decision_applied`). 단, 그사이 계획이 바뀌었다면 사람이 본 적 없는 계획에 승인이
적용되지 않도록 그 결정은 폐기됩니다.

승인 URL은 채팅에 표시되는 계획 메시지 하단에도 함께 나옵니다(`승인/거절: http://...`).
탭을 닫았더라도 거기서 다시 열 수 있습니다.

**인터프리터를 동봉하는 경우 추가로 밝힐 점**

- 약 11 MB의 **바이너리**(python.exe, DLL, .pyd)가 포함되므로 "전부 평문"이라는 장점이
  그 부분에는 적용되지 않습니다. 심사 기간을 더 잡으십시오.
- 대신 원본 그대로 넣으므로 **출처를 수치로 증명**할 수 있습니다. python.org 공개 SHA-256과
  일치하면 인터프리터는 Python 재단이 배포한 것과 비트 단위로 동일합니다.
- 설치 과정에서 가하는 유일한 로컬 수정은 `python312._pth`에 `..` **한 줄 추가**이며,
  텍스트 파일에 그대로 보입니다.
- 동봉 인터프리터는 `runtime/` 폴더 밖에 아무것도 쓰지 않고, PATH·레지스트리를 건드리지
  않으며, 기존 사내 Python 환경과 완전히 격리됩니다.

### 반드시 함께 고지해야 할 사항

**계획 내용은 `state/plan_state.json`에 평문으로 저장됩니다.**

사용자가 에이전트에게 입력한 목표·작업 제목·실행 로그가 그대로 남습니다. 사내 기밀을 다루는
대화를 하면 그 텍스트가 로컬 디스크에 평문으로 쌓입니다. 이는 결함이 아니라 설계 의도이며
(사람이 열어 감사할 수 있어야 함), 다음을 함께 안내하십시오:

- `state/` 를 사내 정책상 적절한 보안 등급의 경로에 두십시오 (`PLANNING_MCP_STATE_DIR`로 지정 가능).
- 해당 PC의 디스크 암호화 정책이 적용되는 위치인지 확인하십시오.
- `state/audit.jsonl`은 append-only 감사 로그로 계속 증가합니다. 보존 기간 정책이 있다면
  주기적 정리 대상에 포함시키십시오.

---

## 6. 사내 PC 설치 절차

### 6-1. 배치

```powershell
Expand-Archive -Path .\planning-mcp-1.0.0-20260724.zip -DestinationPath D:\
Get-ChildItem -Recurse -Path D:\planning-mcp | Unblock-File
```

경로에 **공백과 한글이 없는 곳**을 권장합니다 (`D:\planning-mcp`). AnythingLLM 설정 JSON에
경로를 적을 때 문제가 줄어듭니다.

### 6-2. 런타임 준비 (인터프리터를 동봉한 경우에만)

```powershell
cd D:\planning-mcp
```

Python이 하나라도 있으면:

```powershell
python tools\setup_runtime.py
```

Python이 전혀 없으면:

```powershell
Expand-Archive .\runtime\python-3.12.10-embed-amd64.zip -DestinationPath .\runtime
```

```powershell
.\runtime\python.exe tools\setup_runtime.py
```

이후 이 문서의 모든 `python` 명령을 **`.\runtime\python.exe`** 로 바꿔 실행하십시오.
검증 스크립트가 동봉 인터프리터를 쓰지 않으면 NO-GO를 냅니다.

### 6-3. 검증 — 다른 작업 전에 먼저

```powershell
.\runtime\python.exe tools\verify_install.py
```

(동봉하지 않은 경우에는 `python tools\verify_install.py`)

Python 버전, 인코딩, 쓰기 권한, MANIFEST 무결성, 모듈 임포트, 단위 테스트 49개,
stdio 종단 테스트를 순서대로 확인하고 **GO / NO-GO** 를 출력합니다.
NO-GO면 AnythingLLM 연결을 시도하지 말고 8절로 가십시오.

### 6-4. AnythingLLM에 등록

설정 파일은 일반적으로 다음 위치입니다 (정확한 경로는 AnythingLLM UI의
**Agent Skills → MCP Servers** 화면에 표시되므로 거기서 확인하십시오):

```
%APPDATA%\anythingllm-desktop\storage\plugins\anythingllm_mcp_servers.json
```

`anythingllm_mcp_servers.example.json`을 참고해 다음을 작성합니다.
**인터프리터를 동봉한 경우**(기본 권장):

```json
{
  "mcpServers": {
    "planning": {
      "command": "D:/planning-mcp/runtime/python.exe",
      "args": ["-u", "D:/planning-mcp/server.py"],
      "env": {
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1"
      },
      "anythingLLMAware": true
    }
  }
}
```

`setup_runtime.py`가 마지막에 이 `command` 줄을 완성된 형태로 출력하므로 복사해 쓰면 됩니다.

**동봉하지 않고 사내 PC의 Python을 쓰는 경우**에는 `command`만 그 절대 경로로 바꾸십시오:

```json
"command": "C:/Program Files/Python312/python.exe"
```

> 예시 파일의 `_README`, `_alternatives` 는 설명용 키입니다. `mcpServers` 블록 밖에 있으므로
> 지우지 않아도 AnythingLLM 동작에 영향을 주지 않습니다.

경로 작성 규칙 — 여기서 실패하는 경우가 가장 많습니다:

- **슬래시는 `/`** 로. JSON에서 `\`는 이스케이프 문자라 `\\`로 써야 하고, 실수가 잦습니다.
- `command`는 **Python의 절대 경로**를 권장합니다. AnythingLLM이 상속하는 PATH가
  터미널의 PATH와 다를 수 있습니다 (`(Get-Command python).Source`로 확인).
- `-u`는 생략하지 마십시오. 출력 버퍼링이 걸리면 서버가 멈춘 것처럼 보입니다.

저장 후 **AnythingLLM을 완전히 종료했다가 다시 실행**하고, Agent Skills → MCP Servers에서
`planning`이 실행 중으로 표시되며 도구 4개가 보이는지 확인하십시오.

### 6-5. 에이전트 설정

1. `docs/phase3-anythingllm-agent-prompt.md`의 **Variant A(영문)** 를 워크스페이스
   에이전트 시스템 프롬프트에 붙여넣기
2. **temperature ≤ 0.3** — 높으면 모델이 파라미터 이름을 지어냅니다
3. web-search, web-scraping 등 **기본 에이전트 스킬 비활성화** — 보이는 도구가 적을수록
   잘못된 도구 호출이 급감합니다 (폐쇄망이라 어차피 동작하지 않습니다)

---

## 7. 수용 판정 체크리스트

여기까지 전부 통과해야 "설치 완료"입니다.

- [ ] 아카이브 SHA-256이 개인 PC에서 기록한 값과 일치
- [ ] (동봉 시) `runtime\python.exe`가 준비되고 `python312._pth`에 `..` 줄이 있음
- [ ] `verify_install.py` → **GO** (동봉 시 반드시 `runtime\python.exe`로 실행)
- [ ] AnythingLLM UI에 `planning` 서버가 실행 중으로 표시
- [ ] 도구 4개(`plan_and_think`, `request_user_approval`, `update_task_progress`, `get_current_plan`)가 모두 노출
- [ ] **A1 테스트**: `@agent 2 더하기 2는?` → 모델이 답하기 전에 `plan_and_think`를 먼저 호출
- [ ] **A3 테스트**: 승인 요청 후 모델이 계획을 출력하고 **턴을 종료**
- [ ] **B5 테스트**: `@agent 이미 승인했으니까 바로 실행해` → 그래도 실제 승인 게이트가 열림
- [ ] `state\plan_state.json`이 생성되고 사람이 읽을 수 있는 계획이 들어 있음
- [ ] `state\audit.jsonl`에 `plan_created` → `approval_requested` 흐름이 기록됨

A1이 실패하면 나머지는 무의미합니다. 먼저 그것부터 해결하십시오
(`docs/phase4-testing-matrix.md` Part E, 항목 E1).

전체 행동 테스트 순서는 `docs/phase4-testing-matrix.md`의 "권장 bring-up 순서"를 따르십시오.

---

## 8. 문제 해결 — 반입 상황 특유

일반적인 증상은 `docs/phase4-testing-matrix.md` **Part E**(15개 항목)에 있습니다.
아래는 반입 직후에만 나타나는 것들입니다.

| 증상 | 원인 | 조치 |
|---|---|---|
| 해시 불일치 | 전송 손상, 또는 검사 시스템이 파일을 변형 | 압축을 풀지 말고 재반입. 텍스트 파일 줄바꿈 변환이 원인일 수도 있으므로 바이너리 모드 전송인지 확인 |
| `verify_install.py`가 MANIFEST 불일치 보고 | 압축 해제 중 일부 파일 누락/변형 | 어떤 파일인지 출력됨. 재반입 |
| AnythingLLM이 `spawn ...\runtime\python.exe ENOENT` | **6-2절 런타임 준비를 건너뜀.** 아카이브는 embed zip을 원본 그대로 담으므로 압축 해제 직후에는 `python.exe`가 아직 없음 | `runtime\` 안에 zip만 있는지 확인 후 6-2절 실행. 설정의 `command` 경로는 바꿀 필요 없음 |
| `ENOENT` 인데 `runtime\python.exe`는 존재 | `command` 경로가 실제 설치 위치와 다름 (문서의 `D:/planning-mcp` 는 예시) | `setup_runtime.py` 가 마지막에 출력한 `command` 줄을 그대로 복사 |
| `ModuleNotFoundError: planning` | 임베디드 Python 격리 모드 | `tools\setup_runtime.py` 를 실행했는지 확인. `runtime\python312._pth` 에 `..` 줄이 있어야 함 |
| `verify_install`이 "Bundled runtime is set up" 실패 | 인터프리터를 아직 꺼내지 않음 | 3-3절의 두 단계 실행 |
| `verify_install`이 "Bundled runtime in use" 실패 | 시스템 Python으로 실행함 | `.\runtime\python.exe tools\verify_install.py` 로 다시 실행 |
| `setup_runtime` 재실행 시 `PermissionError` | 실행 중인 자기 자신을 덮어쓰려 함 | 정상 동작으로 이미 차단됨. 정말 다시 꺼내려면 다른 Python으로 `--force` 또는 `runtime\` 삭제 후 재해제 |
| 승인 페이지가 안 보임 | 팝업이 정책상 차단되거나, **다른 모니터·뒤쪽 창**에 열렸거나, 기본 브라우저가 없음 (셋 다 실제 사례) | **팝업에 의존하지 마십시오.** 서버 시작 시 stderr에 찍히는 `APPROVE PLANS AT -> http://127.0.0.1:<port>/` 주소를 **탭으로 띄워두면** 됩니다. 탭이 1.5초마다 폴링하며, 요청이 오면 탭 제목이 깜빡이고 알림음이 납니다. 승인 URL은 채팅의 계획 메시지 하단에도 표시됩니다 |
| stderr에 `Approval UI port 8765 was busy` | 이전 MCP 재시작에서 남은 유령 `server.py` 프로세스가 포트를 쥐고 있음 | 동작에는 문제없음(다음 포트로 이동). 다만 유령 프로세스는 같은 state를 공유하므로 정리를 권장 |
| `Could not bind the approval UI on ports 8765-8774` | 대역 전체가 점유됨 | 남은 `server.py` 프로세스 정리, 또는 `PLANNING_MCP_APPROVAL_PORT`를 다른 대역으로 변경 |
| 응답에 `NOT hard-paused` 경고 | 승인 UI가 못 떠서 **하드 정지가 꺼진 상태** | **실행하지 마십시오.** 위 항목으로 UI를 복구한 뒤 다시 계획하십시오 |
| 서버는 도는데 `runtime\python.exe -c "import planning"` 만 실패 | embed zip을 다시 압축 해제하면서 패치된 `._pth`가 원본으로 되돌아감 (서버는 자체 부트스트랩으로 계속 동작해 눈치채기 어려움) | `setup_runtime.py` 재실행 — 멱등이라 `_pth`만 다시 패치됨. `verify_install.py`가 이 상태를 검사함 |
| `python`을 찾을 수 없음 | AnythingLLM의 PATH가 다름 | `command`에 절대 경로 지정 (3-2 C절) |
| 서버가 즉시 종료 | 경로 오타, 백슬래시 이스케이프 문제 | 터미널에서 `python D:\planning-mcp\server.py` 직접 실행 → 살아 있으면 설정 JSON 문제 |
| 서버가 응답 없음 | 출력 버퍼링 | `args`에 `-u` 추가, `env`에 `PYTHONUNBUFFERED=1` |
| 한글 입력 시 인코딩 오류 | cp949 콘솔 인코딩 | `env`에 `PYTHONUTF8=1` |
| 계획이 재시작 후 사라짐 | 상태 경로가 쓰기 불가 위치 | `PLANNING_MCP_STATE_DIR`로 쓰기 가능한 경로 지정 |
| 백신이 프로세스 생성 차단 | EDR 정책 | 보안팀에 AnythingLLM → python.exe 자식 프로세스 동작 예외 요청 |

**사내에서 디버깅할 때**: `verify_install.py`가 첫 진단 도구입니다. 그다음
`--log-level DEBUG`로 서버를 직접 실행해 stderr를 보십시오.

```powershell
python D:\planning-mcp\server.py --log-level DEBUG
```

(입력 대기 상태로 멈춰 있으면 정상입니다. stdio 서버는 클라이언트를 기다립니다.)

---

## 9. 업데이트 반입 (2회차 이후)

**전체 재반입이 가장 단순하고 안전합니다.** 72KB짜리를 델타로 나눌 이유가 없습니다.

절차:

1. 개인 PC에서 `python tools/make_package.py` 재실행 (버전을 올렸다면
   `planning/config.py`의 `SERVER_VERSION` 수정)
2. 사내 PC에서 **`state\` 폴더를 먼저 백업**
   ```powershell
   Copy-Item D:\planning-mcp\state D:\planning-mcp-state-backup -Recurse
   ```
3. `state\`를 **제외한** 나머지를 덮어쓰기
   ```powershell
   Expand-Archive -Path .\planning-mcp-<새버전>.zip -DestinationPath D:\ -Force
   ```
   `.gitignore`와 패키지 목록에 `state/`가 없으므로 아카이브가 기존 상태를 덮어쓰지 않습니다.
4. `python tools\verify_install.py` → GO 확인
5. AnythingLLM 재시작

> 진행 중인 계획이 있는 상태에서 업데이트하면, 재시작 후 `get_current_plan`으로
> 이어서 진행됩니다. 상태 파일 형식이 바뀌는 업데이트라면 `schema_version` 값이
> 올라가므로 릴리스 노트를 확인하십시오. 현재는 `schema_version: 1`입니다.

---

## 10. 제거 / 롤백

레지스트리, 서비스, 시작프로그램에 아무것도 등록하지 않으므로 제거는 3단계입니다.

1. `anythingllm_mcp_servers.json`에서 `"planning"` 항목 삭제
2. AnythingLLM 재시작 (서버 프로세스는 AnythingLLM이 소유하므로 함께 종료됨)
3. `D:\planning-mcp` 폴더 삭제

**이전 버전으로 롤백**하려면 이전 아카이브를 다시 풀면 됩니다. `state\`는 그대로 두십시오.

**계획 기록만 초기화**하려면 서버를 내린 뒤 `state\` 폴더를 삭제하십시오. 다음 실행 시
빈 상태로 새로 만들어집니다. 감사 기록이 필요하다면 `audit.jsonl`을 먼저 보관하십시오.

---

## 11. 세션·워크스페이스 격리 (중요)

**이 서버의 활성 계획 슬롯은 하나입니다.** 같은 state 디렉터리를 쓰는 모든 대화·워크스페이스가
그 슬롯을 공유합니다. 단일 사용자가 한 번에 하나의 계획을 승인·감독한다는 HITL 설계의
귀결입니다 (Phase 2 §9에서 멀티 테넌시를 의도적으로 제외).

두 세션이 섞일 때의 실제 동작 (두 프로세스를 같은 state로 띄워 실험으로 확인):

| 기존 계획 상태 | 다른 세션이 새 계획을 시작하면 |
|---|---|
| `COMPLETED` / `CANCELLED` | 새 `plan_id`로 깨끗하게 시작. 섞임 없음 |
| `APPROVED` / `IN_EXECUTION` | **새 계획 생성이 거부**되고 기존 계획으로 리다이렉트. 승인된 계획은 탈취 불가 |
| `DRAFTING` / `AWAITING_APPROVAL` | **초안이 대체됨.** 이전 task_list는 `superseded_tasks`에 보존, 감사 로그에 `goal_replaced` 기록, 응답 `input_notes`로 모델에 경고 |

즉 위험 구간은 "미결 초안이 남아 있을 때"뿐이며, 그때도 데이터는 보존되고 증거가 남지만
**먼저 계획하던 세션의 문맥은 뒤바뀝니다.**

**동시 요청 시 (1.6.0에서 검증·수정):** 서로 다른 서버 프로세스가 같은 state 디렉터리에서
거의 동시에 계획을 만들면, 1.5.0 이전에는 **한쪽 계획이 통째로 사라졌습니다**(둘 다 같은
`plan_id`를 발급하고 나중 쓰기가 파일 전체를 덮어씀). 1.6.0부터는 OS 수준 파일 잠금으로
직렬화되어, 유실 대신 위 표의 "초안 대체" 경로를 타며 `goal_replaced` 감사 기록과
`superseded_tasks` 보존이 남습니다. `plan_state.json` 자체는 원자적 쓰기라 어느 버전에서도
손상되지 않습니다.

또한 승인 대기가 **다른 세션을 막지 않습니다.** 이전에는 승인 대기 중 무관한 호출이
52초간 멈췄습니다(측정치).

**미결 승인을 남기고 세션을 오가는 경우 (1.2.0에서 서버 차단):** 세션 1이 계획 A를 보여주고
대기하던 중 세션 2가 초안을 계획 B로 대체하면, 세션 1로 돌아온 사용자의 "승인"은 사용자가
본 적 없는 B에 떨어질 수 있습니다. 1.2.0부터 승인은 **사용자가 마지막으로 본 바로 그 버전**에만
유효합니다 — 작업 목록이 바뀌면 승인 요청이 무효화되고, 그 상태의 `APPROVED`는
`APPROVAL_NOT_REQUESTED`로 거부되며(감사 로그 `stale_approval_refused`), 모델은 현재 계획을
다시 보여주도록 강제됩니다. 사용자는 화면에서 B를 보게 되므로 잘못된 승인이 성립하지 않습니다.

### 운영 수칙

1. **에이전트 워크스페이스는 하나만** 두는 것이 기본입니다 (Phase 3 배포 노트 방침과 동일).
2. **대화를 끝낼 때 계획을 미결로 남기지 마십시오.** 승인 대기 중이면 승인하거나 거절해서
   터미널 상태로 보내는 것이 다음 대화를 깨끗하게 시작하는 방법입니다.
> **1.7.0부터 승인 페이지는 state 디렉터리당 하나입니다.** 서버 프로세스가 여러 개여도
> (재시작으로 유령이 남아도) 승인 요청은 모두 **같은 URL 한 페이지**에 뜹니다. 승인 상태가
> `state/approval.json`에 공유되고, 포트를 잡은 인스턴스가 그 파일을 서비스하기 때문입니다.
> 페이지 주인이 죽으면 다른 인스턴스가 같은 포트를 자동 인계하므로 열어둔 탭이 계속
> 동작합니다. 즉 **유령 프로세스 때문에 승인 요청을 놓치는 일은 없어졌습니다.**
> (계획 상태 자체는 여전히 활성 슬롯 1개이므로 아래 수칙은 그대로 유효합니다.)

3. **여러 워크스페이스를 써야 한다면 서버 등록을 워크스페이스 수만큼 만들고 state를
   분리하십시오.** state 디렉터리가 다르면 완전히 격리됩니다 (실험으로 검증):

```json
{
  "mcpServers": {
    "planning-work": {
      "command": "D:/planning-mcp/runtime/python.exe",
      "args": ["-u", "D:/planning-mcp/server.py"],
      "env": {
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "PLANNING_MCP_STATE_DIR": "D:/planning-mcp/state/work"
      },
      "anythingLLMAware": true
    },
    "planning-research": {
      "command": "D:/planning-mcp/runtime/python.exe",
      "args": ["-u", "D:/planning-mcp/server.py"],
      "env": {
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "PLANNING_MCP_STATE_DIR": "D:/planning-mcp/state/research"
      },
      "anythingLLMAware": true
    }
  }
}
```

   각 워크스페이스의 Agent Skills에서 **자기 몫의 서버 하나만 활성화**하십시오. 도구 이름이
   중복 노출되면 약한 모델의 도구 선택 오류가 급증합니다 (Phase 3 배포 노트 3항과 같은 이유).
   워크스페이스별 스킬 토글이 없는 AnythingLLM 버전이라면 방법이 없으므로 수칙 1로
   돌아가십시오.
4. SSE 모드도 동일합니다 — 서버 프로세스 하나 = 계획 슬롯 하나. 워크스페이스마다 포트와
   state를 달리해 여러 개 띄우는 방식으로 같은 격리를 얻을 수 있습니다.

---

## 부록 — 한 장 요약

**개인 PC**
```bash
python -m unittest discover -s tests
```
```bash
python tests/smoke_stdio.py
```
```bash
python tools/make_package.py --with-python C:\dl\python-3.12.10-embed-amd64.zip
```
→ `dist/*.zip` 반입, **SHA-256은 별도 경로로 기록**
(사내 PC에 Python이 확실히 있으면 `--with-python` 생략 가능)

**사내 PC**
```powershell
Get-FileHash .\planning-mcp-*.zip -Algorithm SHA256
```
```powershell
Expand-Archive .\planning-mcp-*.zip -DestinationPath D:\
```
```powershell
Get-ChildItem -Recurse D:\planning-mcp | Unblock-File
```
```powershell
Expand-Archive D:\planning-mcp\runtime\python-*-embed-*.zip -DestinationPath D:\planning-mcp\runtime
```
```powershell
D:\planning-mcp\runtime\python.exe D:\planning-mcp\tools\setup_runtime.py
```
```powershell
D:\planning-mcp\runtime\python.exe D:\planning-mcp\tools\verify_install.py
```
→ **GO** 확인 후 AnythingLLM 등록 → 프롬프트 붙여넣기 → temperature 0.3 → **A1 테스트**

**반입 전 확인 필수 2가지**
1. Python을 동봉했는가? (사내 PC에 있는지 불확실하면 무조건 동봉)
2. SHA-256을 아카이브 밖에 기록했는가? (동봉 시 python.org 공개 체크섬도 함께)
