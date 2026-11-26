# StepCut 계획

**상태 (2026-11-26)**: 1~5단계 완료. 웹 UI(채팅으로 계획 → 승인 → 녹화 → 코멘트로 수정)와 참고 자료 첨부 추가. SpriteGen 영상 1편을 Cursor agent가 4분 44초에 완성 (재촬영 0회). 실측은 [README.md](README.md)에 있습니다.

이름: **StepCut** (단계별로 기록하고, 단계별로 자릅니다). 코드 패키지는 `stepcut/`, OBS 장면은 `StepCut`입니다. 폴더 `D:\Claude\StepCut` 이름은 아직 그대로입니다.

영상에 꼭 보여 줄 프로젝트 기능은 `demos/<프로젝트>/brief.yaml`에 적습니다 (아래 "보여 줄 기능").

## 목표

포트폴리오용 30~60초 데모 영상을 사람 손을 거의 쓰지 않고 만듭니다.
Claude는 화면을 클릭하지 않고 **MCP 도구만 불러서** 녹화하고 편집합니다. 사람은 완성된 MP4를 보고 확인만 합니다.

```
brief.yaml ─(에이전트가 초안)─▶ scenario.yaml ─(사람 승인)─run_scenario──▶ OBS 녹화 + 단계 로그
                                          │
                     에이전트가 MCP 도구로 확인·수정 (get_frames, set_caption, trim_step …)
                                          │
                                   edit.yaml (편집 결정)
                                          │
                              render ──▶ 완성 MP4 (ffmpeg)
                                          └─▶ (선택) FCPXML + SRT → Resolve
```

## 정해진 것

| 항목 | 결정 |
| --- | --- |
| 완성 위치 | MCP가 ffmpeg로 MP4까지 완성합니다. Resolve는 필요할 때만 FCPXML + SRT로 가져옵니다 |
| 다루는 영상 | 단계 로그가 있는 StepCut 녹화본만 다룹니다 |
| 에이전트 | Cursor agent CLI가 StepCut MCP 도구로 일합니다. Claude Code도 같은 도구를 씁니다 |
| 녹화 | 녹화도 MCP 도구(`run_scenario`)로 합니다 |
| 기본 요소 | 자막 입히기, 인트로/아웃트로 카드, 클릭 강조/확대 |
| 첫 대상 | SpriteGen (돈이 나가는 생성은 `allow_paid`일 때만) |

확인된 사실: 무료 Resolve 21.1은 외부 스크립트 연결이 안 되고(Studio 전용), 내부 콘솔은 Lua만 되며 파일 입출력도 막혀 있습니다. 그래서 Resolve 자동 조작은 하지 않습니다.

## 이미 있는 것 (재사용)

- `stepcut/web.py`: Playwright 드라이버. 역할·라벨·텍스트로 요소를 찾고(화면 좌표 클릭 없음), 화면에 가짜 커서를 그립니다
- `stepcut/obsctl.py`: obs-websocket으로 StepCut 장면(앱 창만 캡처) 구성, 녹화 시작/중지, 녹화 기준 시각. 실측 오차 8ms
- `stepcut/runner.py`: 시나리오 실행, 단계마다 성공 확인, 실패하면 그 촬영을 버리고 재촬영
- `stepcut/edit.py`: 로그 → 구간(대기 구간 컷/빨리감기), SRT, FCPXML, ffmpeg 미리보기
- 실측: SpriteGen OBS 1회 촬영 성공, 재촬영 0회, 녹화 41초 + 편집 파일 생성 8초

## 보여 줄 기능 (brief.yaml)

프로젝트마다 `demos/<프로젝트>/brief.yaml`에 영상에 꼭 나와야 할 기능(`must_show`), 있으면 좋은 기능(`nice_to_show`), 보이면 안 되는 것(`avoid`)을 적습니다. 채팅으로 "이 기능도 보여 줘"라고 해도 에이전트가 이 파일에 적습니다.

- 시나리오의 각 단계에 `feature: <id>`를 달아 어느 기능을 보여 주는지 표시합니다
- `check`가 완성 영상에 `must_show`가 모두 남았는지, 몇 초에 나오는지 알려 줍니다. 잘렸거나 실패한 단계에 있던 기능은 "빠짐"으로 나옵니다
- 대상 프로젝트 저장소는 건드리지 않습니다

## 모든 동작을 녹화 시각에 맞춰 기록

녹화 시작(파일 첫 프레임)을 0초로 두고, 프로젝트를 만지는 모든 동작을 같은 시계로 `events.jsonl`에 한 줄씩 남깁니다.

| 종류 | 남기는 것 |
| --- | --- |
| 드라이버 동작 | 커서 이동 시작/끝, 클릭, 타이핑 글자, 스크롤, 파일 올리기 |
| 페이지 안 이벤트 | 앱이 실제로 받은 클릭·입력, 화면 변화(결과 그림 등장 등). 페이지 시각을 녹화 시각으로 바꿔 기록 |
| 앱 서버 | API 요청과 응답, 처리 시간 |
| 녹화 상태 | OBS 시작/중지, 장면 전환, 떨어진 프레임 |

```json
{"t": 12.418, "src": "driver", "type": "click", "step": "open-text-method", "target": "button 문장참고", "x": 1510, "y": 22}
```

자막, 클릭 확대, 대기 건너뛰기는 모두 이 이벤트 시각에 붙입니다.

**싱크 검증**: 촬영 시작과 끝에 페이지 구석에 한 프레임짜리 표시를 띄우고, 영상에서 그 프레임을 찾아 실제 지연(화면 변화 → OBS 캡처)을 자동 보정합니다. 지금 시계 오차는 8ms(1프레임 미만)이고, 이 검사로 영상 자체를 기준으로 한 번 더 확인합니다. 차이가 2프레임을 넘으면 `check`가 경고합니다.

## 편집 데이터

녹화 로그(`log.json`)는 바꾸지 않고, 편집 결정만 `edit.yaml`에 쌓습니다. 렌더는 항상 `log.json + edit.yaml`로 처음부터 다시 만들기 때문에 결과가 매번 같고, 이력으로 되돌릴 수 있습니다.

```yaml
global:
  wait: speed        # speed | cut
  speed: 8
  intro: {title: SpriteGen, subtitle: 그림 한 장으로 같은 캐릭터의 스프라이트 8장, seconds: 2.5}
  outro: {title: SpriteGen, lines: [github.com/…], seconds: 2.5}
  subtitles: {burn: true, size: 40, position: bottom}
steps:
  upload-character: {caption: "…", trim_head: 0.3}
  type-name: {drop: true}
  open-punch-result: {zoom: 1.6, hold_extra: 0.5}
history: [...]       # 도구 호출마다 한 줄
```

## MCP 도구 (로컬 Python, stdio)

| 분류 | 도구 | 하는 일 |
| --- | --- | --- |
| 녹화 | `list_scenarios` | `demos/*.yaml` 목록 |
| | `run_scenario(scenario, allow_paid=false)` | OBS로 녹화, 실패 시 재촬영, run id 반환 |
| 보기 | `list_runs` / `get_run(run)` | 단계·시간·자막·현재 편집·완성 길이 |
| | `get_frames(run, step 또는 시각, timeline=source/output)` | 프레임 이미지를 돌려줘서 Claude가 직접 보고 판단 |
| 편집 | `set_caption(run, step, text)` | 자막 수정 |
| | `edit_step(run, step, trim_head, trim_tail, speed, drop, zoom)` | 단계 단위 편집 |
| | `set_global(run, …)` | 대기 처리, 인트로/아웃트로, 자막 스타일 |
| | `undo(run)` / `history(run)` | 되돌리기 |
| 출력 | `render(run, quality=preview/final)` | 완성 MP4 경로와 길이 |
| | `check(run)` | 자동 품질 검사 (아래) |
| | `export_resolve(run)` | FCPXML + SRT (선택) |

`.cursor/mcp.json`(Cursor agent)과 `.mcp.json`(Claude Code)에 같은 서버를 등록합니다.

## 에이전트 (Cursor agent CLI)

도구는 일만 하고, 무엇을 할지는 에이전트가 정합니다. 규칙은 `AGENTS.md`에 둡니다.

1. brief와 README를 읽고 시나리오 초안 작성
2. 녹화 없이 시험 실행, 못 찾은 요소 고치기
3. **사람 승인 대기** (시나리오)
4. `run_scenario`로 녹화, 실패하면 재촬영
5. `get_frames`로 화면을 보고 자막·자르기·확대 조정
6. `render` → `check` (길이, 싱크, 기능 포함)
7. 완성 MP4와 수치 보고

- 즉흥은 초안 작성과 편집 검토에서만 합니다. 녹화는 승인된 시나리오를 그대로 반복합니다
- 사람이 멈춰 확인하는 곳: 시나리오 승인, 돈이 나가는 단계, 완성 영상 확인
- 화면 클릭이나 Resolve 조작은 하지 않고 MCP 도구만 씁니다
- 실행: `cursor-agent -p --approve-mcps --workspace D:\Claude\StepCut "SpriteGen 영상 만들어줘"` 또는 Cursor/Claude Code 대화창

## 렌더 방식 (ffmpeg)

- 구간: 보여 줄 구간은 1배, 대기 구간은 빨리감기 또는 컷
- 자막: SRT를 ASS로 바꿔 맑은 고딕으로 입힙니다 (미리보기에서 한글 표시 확인됨). SRT 파일도 따로 남깁니다
- 인트로/아웃트로 카드: HTML 카드를 Playwright로 1920×1080 PNG로 찍어 정지 화면으로 붙입니다 (한글 글꼴이 깔끔하게 나옵니다)
- 클릭 강조/확대: 드라이버가 클릭할 때 요소 위치를 로그에 남기고, 렌더에서 그 지점으로 천천히 확대(예: 1.4배)했다가 돌아옵니다. 커서 클릭 표시는 이미 화면에 그려집니다

## 추가로 넣으면 좋을 것 (제안)

1. **단계 표시**: 화면 위쪽에 `2/5 동작 적기` 같은 작은 칩을 넣어 지금 무엇을 하는지 보여 줍니다
2. **빨리감기 표시**: 빨리 넘긴 구간에 `×8` 배지를 붙여 영상을 속이지 않는다는 걸 보여 줍니다 (실제 생성 대기 같은 구간)
3. **자동 품질 검사** (`check`): 전체 길이 30~60초, 자막 읽기 속도(초당 글자 수), 자막 겹침, 검은 화면·멈춘 화면 구간, 실패 단계 여부
4. **썸네일**: 가장 보여 줄 만한 장면 한 장에 제목을 얹은 PNG
5. **짧은 루프판**: 포트폴리오 카드용 5~8초 무음 WebM/GIF
6. **결과 정지 화면**: 마지막에 결과물(예: 8칸 시트)을 1~2초 멈춰 보여 주기

배경음악은 고르지 않으셔서 넣지 않습니다.

## 진행 순서

| 단계 | 내용 | 끝난 기준 |
| --- | --- | --- |
| 1 | `edit.yaml` 편집 모델 + 렌더 (구간, 자막 입히기, 인트로/아웃트로) | 이미 찍은 SpriteGen 녹화본으로 완성 MP4가 나옴 |
| 2 | 클릭 위치 기록 + 확대 효과 | 클릭 장면이 확대되어 보임 |
| 3 | MCP 서버 + 도구, `.mcp.json` 등록 | Claude가 도구만으로 자막 하나 고치고 다시 렌더 |
| 4 | `run_scenario` 도구 + `check` (기능 포함 검사) | 녹화 → 편집 → 렌더를 MCP 호출로 한 번에 |
| 4b | Cursor agent 설정 (`.cursor/mcp.json`, `AGENTS.md`) | Cursor agent가 도구만으로 영상 한 편 완성 |
| 5 | 실측과 한국어 README | 영상 1편당 시간(도구 vs 수동 2시간 이상), 재촬영 횟수 기록 |

제안 항목 1~6은 고르신 것만 2~4단계 사이에 넣습니다.

## 다른 프로젝트 영상 만들기 (다음 단계)

### 공통으로 바꿀 것

프로젝트 종류마다 **드라이버**만 바꾸고, 녹화·로그·편집·렌더·검사·MCP 도구·에이전트는 그대로 씁니다. 시나리오의 `driver:` 값으로 고릅니다.

모든 드라이버가 지켜야 할 약속:

| 약속 | 내용 |
| --- | --- |
| `launch` / `close` | 앱을 켜고 끄기. 녹화 전 준비(`setup:`: 상태 초기화, 서버 깨우기, 에디터 로딩)는 녹화 밖에서 |
| `window` | OBS가 잡을 창 (제목·클래스·exe) |
| `do(step)` | 화면 좌표가 아니라 앱이 아는 이름으로 조작 (웹: 역할·라벨, Godot: 노드·칸 이름, Unreal: MCP 도구) |
| `check(expect)` | 단계 성공 확인 (화면 글자, 앱 상태, 로그 한 줄) |
| `events` | 모든 동작을 녹화 시각으로 `events.jsonl`에 기록 |
| `sync_marker` | 앱 창 구석에 자홍색 칸 표시. 앱이 못 그리면 OBS 장면의 색 소스를 켰다 끄는 방식으로 대신 |

새로 넣을 단계 종류: `press` / `hold` / `keys`(키보드, 게임용), `drag`, `http`(앱 API 호출), `wait_log`(앱 로그에서 한 줄 기다리기). 창 두 개를 나란히 녹화하는 OBS 배치도 추가합니다 (온라인 대전용).

### 프로젝트별

| 프로젝트 | 종류 | 드라이버 | 조작과 확인 | 주의할 점 |
| --- | --- | --- | --- | --- |
| Low Poly Cultivar | 로컬 웹 도구 | `web` (지금 것 그대로) | GLB 올리기 → Generate All → 비교 화면. 화면 글자로 확인 | 처리 약 48초는 대기 구간으로 빨리감기. **새 코드 없이 시나리오만 쓰면 됨** |
| PromptAssetForge | 로컬 웹 도구 (Flask) | `web` | 테마 입력 → 프롬프트 목록 → PNG → GLB 목록 | 생성에 Cursor API와 Trellis(GPU, 수 분)가 들어감. `paid: true`로 막고 기본은 지난 결과(`output/`)를 열어 보여 줌 |
| GrowRoll | 웹 게임 (3D) | `web` + 키보드 단계 | `hold W 3초`로 굴리며 소품 줍기. HUD 글자(등급, 개수)로 확인 | 게임 안 상태를 읽을 길이 HUD뿐이면 확인이 약함 |
| GGPO Web Fighter | 웹 게임 (Canvas) | `web` + 키보드 단계 + 창 두 개 | 로컬 2인: 이동·펀치 콤보 입력. 온라인: 탭 두 개로 방 만들기/들어가기 | 온라인 서버(Render 무료)가 깨는 데 약 11초 → `setup:`에서 미리 깨움. 같은 PC 두 창을 나란히 녹화 |
| Godot NNUE Chess | Godot 게임 | `godot` (새로) | 게임 안에 작은 StepCut 연결 스크립트(로컬 소켓)를 두고 "e2→e4 두기", "채팅 패널 글자 읽기"를 명령. `AI_SIDECAR_INJECT_UCI`로 AI 답을 고정해 매번 같은 판 | 연결 스크립트를 chess 저장소에 추가해야 함 (승인 필요). 이미 있는 `capture_gate_demo.gd`가 출발점 |
| Unreal Prompt Editor | Unreal 에디터 플러그인 | `unreal` (새로) | 사이드카 HTTP `/prompt`로 문장 보내기, 플러그인 MCP 도구(180개)로 결과 확인(액터가 생겼는지 등), 승인 버튼은 사이드카 API로 | 에디터 로딩(1~2분)은 녹화 밖에서. 매 문장이 Cursor API 호출이라 `paid`이고, AI 답이 매번 달라 확인 조건을 느슨하게 둠. 실패 재촬영 횟수 제한 |
| (그 밖의 Windows 앱) | 데스크톱 앱 | `uia` (나중) | Windows UI Automation으로 컨트롤 이름을 찾아 조작 | 앱이 마커를 못 그리므로 OBS 쪽 마커만 |

### 순서 제안

1. **Low Poly Cultivar**: 새 코드 없이 다른 프로젝트에서도 되는지, 긴 대기 빨리감기가 되는지 확인
2. **GrowRoll, GGPO**: 키보드 단계와 창 두 개 녹화
3. **Godot Chess**: 첫 비웹 드라이버
4. **PromptAssetForge**: 느리고 돈 드는 생성 다루기
5. **Unreal Prompt Editor**: 가장 복잡 (에디터 로딩, AI 답이 매번 다름)

대상 저장소는 건드리지 않고 brief와 시나리오는 StepCut의 `demos/<프로젝트>/`에 둡니다. Godot 연결 스크립트처럼 대상 저장소에 파일을 넣어야 할 때만 따로 승인을 받습니다.

## 한계 (미리 알려 드릴 것)

- 웹 프로젝트만 먼저 됩니다. Godot Chess, Unreal Prompt Editor는 드라이버를 따로 만들어야 합니다
- 녹화 중에는 Chromium이 전체 화면으로 떠서 그동안 PC를 쓰기 어렵습니다 (녹화에는 앱 창만 담깁니다)
- 시나리오 초안은 Claude가 README를 보고 쓰지만, 실행은 정해진 시나리오만 그대로 반복합니다

## 정리할 것

- OBS 설정에서 웹소켓 서버를 켰습니다 (원래 설정은 `config.json.autodemo-backup`에 있습니다). OBS에 `AutoDemo` 장면이 남아 있으면 지워도 됩니다
- Resolve 확인 중에 빈 프로젝트 `AutoDemo scratch`를 만들었고, Scripts 폴더의 `AutoDemo_Import.lua`는 지웠습니다. 프로젝트는 지워도 됩니다
