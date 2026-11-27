# StepCut

로컬 프로젝트를 정해진 시나리오대로 직접 조작하면서 녹화하고, 조작 로그를 기준으로 30~60초 데모 영상을 자동으로 편집하는 도구입니다.
녹화·편집은 MCP 도구로만 이루어지고, 에이전트(Cursor agent CLI 또는 Claude Code)가 그 도구를 불러 영상 한 편을 끝까지 만듭니다. 사람은 시나리오를 승인하고 완성 영상을 확인합니다.

## 왜 만들었나

포트폴리오에 넣을 프로젝트 데모 영상을 OBS로 찍고 DaVinci Resolve로 자르는 데 한 편에 2시간 넘게 걸렸습니다.
같은 화면을 다시 찍을 때마다 동작 순서와 타이밍이 달라져서 자막과 자르는 지점도 매번 다시 잡아야 했습니다.
그래서 조작을 시나리오로 고정하고, 조작마다 녹화 시각을 기록해 그 기록으로 편집하게 했습니다.

## 흐름

```
demos/<프로젝트>/brief.yaml      영상에 꼭 보여 줄 기능 (사람이 적음)
        │  에이전트가 README와 brief를 읽고 초안 작성
demos/<프로젝트>/scenario.yaml   클릭·입력·확인 단계와 자막
        │  dry_run으로 시험 → 사람이 approve
run_scenario  ── 화면 녹화(앱을 키오스크 전체 화면으로) + log.json(단계) + events.jsonl(모든 동작)
        │  싱크 표시로 녹화 시각 보정
edit.yaml     ── 에이전트가 get_frames로 보고 자막·자르기·확대 조정
        │
render        ── out/final.mp4 (자막 입힘, 인트로/아웃트로, 클릭 확대, 대기 빨리감기)
check         ── 길이, 자막 속도, 싱크, 실패 단계, 꼭 보여 줄 기능 포함 여부
```

- **시나리오**: `goto`, `click`, `type`, `upload`, `scroll`, `wait` 같은 단계마다 성공 확인(`expect`)과 자막이 붙습니다. 요소는 역할·라벨·텍스트로 찾고 화면 좌표는 쓰지 않습니다. 확인이 실패하면 그 촬영은 버리고 다시 찍습니다.
- **녹화**: 따로 설치할 녹화 프로그램 없이 StepCut이 직접 찍습니다. 실제 마우스는 영상에 들어가지 않고, 화면에 그린 커서가 클릭 위치를 보여 줍니다. 소리는 녹음하지 않습니다.
  - `screen` (기본): ffmpeg가 모니터를 초당 30장(`--fps 60`이면 60장)으로 찍습니다. 앱은 키오스크 전체 화면으로 띄워 알림·작업 표시줄·전체 화면 안내가 보이지 않게 하고, 클릭으로 열리는 OS 파일 창은 막습니다(업로드는 파일을 직접 넣습니다). 녹화하는 동안 화면을 차지합니다.
  - `screencast`: 브라우저가 화면을 그릴 때마다 보내는 프레임을 받아 영상으로 만듭니다. 싱크 오차가 3ms 안팎이고 `--hidden`으로 창을 숨긴 채 찍을 수 있지만, 1080p에서 움직이는 장면은 초당 10~15장이라 커서 이동이 덜 부드럽습니다.
  - 웹 UI에서는 녹화 버튼 옆에서 고릅니다.
- **시각 기록**: 녹화 파일 첫 프레임을 0초로 두고 드라이버 동작(커서 이동, 클릭, 입력), 페이지가 실제로 받은 이벤트, API 요청, 녹화 상태(프레임 수, 빠진 프레임)를 `events.jsonl`에 남깁니다. 촬영 시작과 끝에 화면 구석에 자홍색 칸을 몇 프레임 띄우고 영상에서 찾아 지연을 보정합니다.
- **편집**: 원본 로그는 바꾸지 않고 편집 결정만 `edit.yaml`에 쌓습니다. 렌더는 매번 처음부터 다시 만들어 결과가 같고, `undo`로 되돌릴 수 있습니다.

## 실행

준비물: Python 3.10, Google Chrome 또는 Playwright Chromium, Node.js(대상 웹 프로젝트용), Cursor agent CLI.

```powershell
cd D:\Claude\StepCut
py -3.10 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy .env.example .env   # CURSOR_API_KEY 채우기 (git에 들어가지 않음)
```

```powershell
.venv\Scripts\python -m stepcut approve demos\spritegen   # 시나리오 승인 (사람)
.venv\Scripts\python -m stepcut agent spritegen           # 에이전트가 영상 한 편 완성
```

에이전트 없이 직접 돌릴 수도 있습니다.

```powershell
.venv\Scripts\python -m stepcut run demos\spritegen --final            # 화면 녹화, 30fps
.venv\Scripts\python -m stepcut run demos\spritegen --fps 60
.venv\Scripts\python -m stepcut run demos\spritegen --recorder screencast --hidden   # 창 숨기고
.venv\Scripts\python -m stepcut check runs\SpriteGen\<시각>
```

결과는 `runs/<프로젝트>/<시각>/out/final.mp4`와 `captions.srt`입니다. Resolve에서 더 손볼 때는 `export-resolve`가 FCPXML과 SRT를 만듭니다 (확대와 카드는 MP4에만 들어갑니다).

### 웹 UI

```powershell
.venv\Scripts\python -m stepcut ui      # http://127.0.0.1:8790
```

- **채팅**: 프로젝트를 고르고 영상을 어떻게 만들지 적으면 Cursor agent가 README·brief·참고 자료를 보고 계획(brief, 시나리오)을 고칩니다. 대화는 프로젝트마다 이어집니다 (`demos/<프로젝트>/chat/`).
- **계획 패널**: 단계와 자막, 꼭 보여 줄 기능, `brief.yaml` / `scenario.yaml` 직접 편집, 참고 자료 끌어 놓기.
- **버튼**: 시험 실행(보이지 않는 브라우저), 승인, 녹화. 승인과 녹화는 사람만 누를 수 있고, UI가 켜져 있는 동안 에이전트의 `run_scenario`는 거절됩니다.
- **영상**: 플레이어, 단계별 타임라인, `check` 결과. 재생 위치에서 코멘트를 보내면 에이전트가 그 장면을 보고 고친 뒤 다시 렌더합니다.
- 127.0.0.1에서만 열리고, 사람 전용 동작은 서버를 켤 때마다 새로 만드는 토큰이 있어야 합니다.

### 참고 자료

`demos/<프로젝트>/refs/`에 파일을 두고 `brief.yaml`의 `references:`에 용도를 적습니다 (UI에 끌어 놓으면 자동). 용도는 `pacing`(흐름·속도), `must_look_like`(이런 화면이 나와야 함), `captions`(자막 말투), `style`, `info`입니다. 영상은 에이전트가 `view_reference`로 일정 간격 프레임과 장면 전환 시각을 봅니다. 모든 프로젝트에 공통인 규칙은 `demos/_style/`에 둡니다. `refs/`는 git에 넣지 않습니다.

### MCP 도구

`.cursor/mcp.json`(Cursor)과 `.mcp.json`(Claude Code)에 `stepcut` 서버가 등록되어 있습니다. 에이전트 규칙은 [AGENTS.md](AGENTS.md)에 있습니다.

| 분류 | 도구 |
| --- | --- |
| 프로젝트 | `list_projects`, `get_project`, `set_feature`, `write_scenario`, `get_references`, `view_reference`, `add_reference` |
| 녹화 | `dry_run`, `run_scenario`, `get_job` |
| 보기 | `list_runs`, `get_run`, `get_events`, `get_frames` |
| 편집 | `set_caption`, `edit_step`, `set_global`, `history`, `undo` |
| 출력 | `render`, `check`, `export_resolve` |

사람이 정하는 것은 세 가지이고 도구로는 넘을 수 없습니다: 시나리오 승인(`approve`, 시나리오 내용이 바뀌면 승인이 풀립니다), 돈이 나가는 단계(`approve --allow-paid`), 완성 영상 확인.

### 보여 줄 기능 제안

`demos/<프로젝트>/brief.yaml`의 `must_show`(꼭), `nice_to_show`(있으면 좋음), `avoid`(보이면 안 됨)에 적거나, 에이전트에게 "이 기능도 보여 줘"라고 말하면 `set_feature`로 적습니다. 시나리오 단계의 `feature:`와 연결되어 `check`가 완성 영상에 나오는지, 몇 초에 나오는지 알려 줍니다.

## 실측 (SpriteGen, 2026-11-26)

`python -m stepcut agent spritegen` 한 번으로 Cursor agent가 dry_run → 녹화 → 미리보기·검사 → 최종 렌더까지 했습니다. 이때는 OBS로 녹화했습니다(지금은 뺐습니다).

| 항목 | StepCut | 직접 할 때 (OBS + Resolve) |
| --- | --- | --- |
| 영상 1편에 걸린 시간 | 4분 44초 (에이전트 시작부터 final.mp4까지) | 2시간 넘게 |
| 사람이 쓴 시간 | 승인 1번, 완성 영상 확인 | 녹화·자르기·자막 전부 |
| 촬영 / 재촬영 | 1번 / 0번 | — |
| 녹화 | 41.8초 (앱 창만, 1920×1080 60fps) | |
| 렌더 | 미리보기 약 6초, 최종 20초 | |
| 완성 영상 | 42.1초, 자막 9개, 클릭 확대 7곳, 인트로/아웃트로 | |
| 기록된 동작 | 138개 (`events.jsonl`) | |
| 싱크 | 화면 변화가 녹화에 담기기까지 224ms(13프레임) 지연을 영상의 표시로 찾아 보정, 시작·끝 차이 51ms는 선형 보정 | |
| 꼭 보여 줄 기능 | 4/4, 있으면 좋은 기능 2/2 | |

- 첫 에이전트 실행은 Cursor가 MCP 도구 호출을 막아 17초 만에 멈췄고, `.cursor/cli.json`에 `Mcp(stepcut:*)`만 허용한 뒤 통과했습니다. 위 시간은 두 번째 실행입니다.
- 에이전트가 찾은 StepCut 버그(녹화 작업 끝에 요약 저장 실패)는 고쳤습니다. 녹화와 렌더 결과에는 영향이 없었습니다.
- 유료 단계(실제 생성)는 승인하지 않아 건너뛰었고, 지난 생성 결과를 열어 보여 줍니다.

OBS를 뺀 뒤 자체 화면 녹화로 같은 시나리오를 찍은 결과(2026-11-27): 15단계 모두 통과, 30fps는 1,657프레임 / 60fps는 3,317프레임에 빠진 프레임 0, 녹화 시작 지연 약 0.45~0.77초는 싱크 표시로 보정, 시작·끝 차이 67~83ms는 선형 보정.

## 한계

- 지금은 웹 프로젝트(Playwright)만 됩니다. Godot, Unreal 프로젝트는 드라이버를 따로 만들어야 합니다.
- `screen` 녹화 중에는 앱이 전체 화면을 덮어 그 사이에는 PC를 쓰기 어렵습니다. 그때 PC를 써야 하면 `screencast --hidden`을 씁니다(덜 부드러움).
- 무료 DaVinci Resolve는 외부 스크립트 연결이 막혀 있어 Resolve 자동 조작은 하지 않습니다. 최종 MP4는 ffmpeg로 만듭니다.
- 시나리오가 화면 문구(버튼 이름, "#23" 같은 결과 번호)에 기대므로 대상 앱 화면이 바뀌면 시나리오도 고쳐야 합니다.
- `check`는 길이·자막 속도·싱크·기능 포함·검은/멈춘 화면만 봅니다. 화면에 개인 정보가 보이는지는 사람이 확인해야 합니다.

## 모델

- 에이전트: Cursor agent CLI + Cursor API. 기본 모델은 **`composer-2.5`**이고, StepCut이 매번 `--model`로 넘기므로 PC의 Cursor CLI 설정(`~/.cursor/cli-config.json`)과 상관없습니다.
  - 기본값 바꾸기: `.env`에 `STEPCUT_AGENT_MODEL=<모델 id>` (`cursor-agent --list-models`로 목록 확인)
  - 웹 UI: 채팅 입력창 옆 메뉴에서 대화마다 고를 수 있고, 고른 값은 브라우저에 기억됩니다. 모델을 바꿔도 대화는 이어집니다.
- Claude Code에서도 같은 MCP 도구로 쓸 수 있습니다.
- 이 도구는 Claude Code(`claude-opus-5-5`)로 만들었습니다.
