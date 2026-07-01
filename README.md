# tokendance

**상시 켜진 호스트 위에서 당신의 코딩 일감을 스스로 관리하는 자율 마스터 에이전트.**

Slack 봇에게 일감을 한 줄 던지면 — 마스터가 깨어나 격리된 워커에게 시키고, 결과물을 직접
리뷰하고, 진행 상황과 "판단이 필요한 지점"을 당신에게 보고합니다. 막히면 솔직히 말하고, 잘
풀리면 조용히 처리합니다. **사람은 봇과 대화만 하면 됩니다 — 나머지는 에이전트들이 합니다.**

---

## 핵심 아이디어

- **상시 + 자율.** 사람 입력이 없어도 멈추지 않습니다. 주기적으로 깨어나 일감 풀을 보고,
  새 메시지가 오면 즉시 반응하고, 할 일이 없으면 점점 더 드물게 깨어납니다(idle 백오프).

- **워커 = 독립 OS 프로세스.** 마스터는 일감을 "코드로 띄운 별도 `claude` 프로세스"(워커)에
  맡기고 기다리지 않고 잠듭니다. 워커는 자기 수명대로 살다 끝나면 결과를 파일에 남깁니다.
  하네스 세션에 묶이지 않아 진짜 fire-and-forget이 됩니다.

- **격리로 얻는 효율.** 일감끼리도, 마스터↔워커도 컨텍스트를 공유하지 않습니다. 각자 깨끗한
  머리로 자기 일만. 워커는 격리된 git worktree에서 작업하므로 동시에 여러 개가 돌아도 안 부딪힙니다.

- **파일이 단일 진실원.** 모든 상태와 소통은 레포 안 파일에 있습니다. 연속성은 "살아있는
  프로세스"가 아니라 "상태 파일"에 사니, 마스터가 죽었다 깨어나도 파일을 보고 이어갑니다.

- **마스터가 직접 리뷰.** 워커 결과물을 마스터가 검수해 합격/반려를 판단합니다(자동 테스트도 곁들여).
  진행 중인 워커에게 중간 의견(steer)도 주입할 수 있습니다.

- **사람 인터페이스 = Slack 봇.** 폰에서 봇에게 던지고 받습니다. 받는 즉시 "받았어요 + 현재
  상태" 가 오고, 처리되면 결과가 옵니다. 당신 메시지만 반응합니다.

- **스스로 개선한다(dogfooding).** tokendance의 개선 과제도 일감으로 넣으면, tokendance가
  자기 코드를 워커로 고쳐 PR로 올립니다. 실제로 워커 격리·자기 회복·멀티레포 지원 등을 스스로 구현했습니다.

- **배우고 정리한다.** 작업에서 얻은 노하우/레포 지식을 라이브러리로 축적하고, 주기적으로
  스스로 다듬습니다(중복 병합·재분류·구조화). 불확실한 지식은 1급으로 올리기 전에 당신 검토를 받습니다.

## 사람이 하는 일

**Slack에서 tokendance 봇에게 DM 한 줄.** 일감이든 질문이든 피드백이든.
나머지(분류·실행·리뷰·보고)는 마스터와 워커가 합니다.

진행이 궁금하면 봇에게 물어보면 됩니다("지금 상태 알려줘"). 가동/설정은 보통 에이전트에게
시키지만, 직접 하려면 아래 레퍼런스를 보세요.

## 더 읽을거리

- 설계: [docs/superpowers/specs/2026-06-24-tokendance-master-agent-design.md](docs/superpowers/specs/2026-06-24-tokendance-master-agent-design.md)
- 구현 플랜: [docs/superpowers/plans/2026-06-24-tokendance-master-agent-mvp.md](docs/superpowers/plans/2026-06-24-tokendance-master-agent-mvp.md)
- 기반 검증(왜 이렇게 만들었나): [docs/superpowers/spikes/2026-06-24-foundation-findings.md](docs/superpowers/spikes/2026-06-24-foundation-findings.md)

---

# 레퍼런스 (운영·내부 — 대개 에이전트가 다룸)

### 한 단락 동작 요약
`scripts/supervisor.py`(상주 루프)가 60초마다 워커 헬스/Slack을 살피고, 주기적으로 headless
`claude` 마스터를 1회 기동한다. supervisor 자신은 `supervise.sh` keepalive 래퍼가 감시해
죽으면 재기동한다. 마스터는 inbox(=Slack 폴링 + 파일 입력)를 읽어 질문엔 직접 답하고, 사소한
일은 직접 하고, 본격 코딩 일감은 `launch-worker.sh`로 격리 워커를 띄운다. 워커는 progress/
heartbeat/steer 파일로만 소통하고, 끝나면 마스터가 다음 깨어남에 리뷰한다.

### 설정 & 가동
```bash
cp config.example.md config.local.md      # 인스턴스 설정(git 미추적). 채울 값:
#   SLACK_BOT_TOKEN : Slack 앱 Bot User OAuth Token (xoxb-…)   ┐ 둘 다 있어야
#   SLACK_CHANNEL   : 봇과 DM 할 사용자 Slack user id (U0…)     ┘ Slack 연동 켜짐
#   MAX_WORKERS(기본1) · POLL_INTERVAL(기본1800s) · MASTER_SESSION_MAX_CYCLES(기본20)

export TOKENDANCE_CLAUDE="$(ls -dt /root/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude | head -1)"
scripts/start.sh        # 데몬 기동(keepalive 래퍼가 감시)
scripts/stop.sh         # 정지
python3 scripts/supervisor.py --once      # 수동 1사이클
python3 scripts/supervisor.py metrics     # 마지막 tick 메트릭
```
Slack 봇 사용 전 한 번: 앱 설정 → App Home → **Messages Tab** 켜기(봇에게 DM 가능해짐).

### 일감 주는 경로 (전부 inbox 로 수렴)
- **Slack 봇 DM**(권장): supervisor가 60초마다 폴링 → inbox. 너의 메시지만 처리(봇/타인 무시).
- **파일/터미널**: `python3 scripts/inbox.py add "할 일" --slug x` 또는 `state/inbox/pending/`에 `.md` 투입.

### 상태 보기 / 진행 중 워커 조향
```bash
python3 scripts/tasks.py list                 # 일감 + 상태
python3 scripts/status.py get <id>            # status.json
cat state/tasks/<id>/progress.md              # 워커 진행(peek)
echo -e "## $(date -u +%FT%TZ)\n이렇게 해줘" >> state/tasks/<id>/steer.md   # 조향(append)
python3 scripts/tasks.py archive <id>         # 종료(done/failed) task 를 state/tasks-archive/ 로 정리
```
`archive` 는 종료 상태(done/failed)만 허용(활성 task 보호). worktree 는 **추적 파일 미커밋 변경이
없으면** 제거하고 이동한다(커밋은 브랜치에 남으니 안전; untracked 산출물만 버림). 미커밋 변경이
있으면 거부(진짜 unsaved 보호). **브랜치는 건드리지 않는다** — 미푸시 커밋 손실 방지.
머지된 브랜치 GC + 일일 다이제스트는 `morning.py` 가 매일 수행.

### 파일 레이아웃
```
state/inbox/{pending,processed}/   입력 큐
state/tasks/<id>/                  task.md · status.json · progress.md · steer.md · knowledge.md(지식 블록, harvest 대상) · review.md
state/reports/<날짜>.md             일자 리포트
state/worktrees/<id>/              워커 격리 작업트리
prompts/master/*.md                마스터 시스템 프롬프트(관심사별 분할: persona/tools/process/rules)
prompts/worker.md, knowledge-block.template.md
library/                           지식 라이브러리(harvest 생성물 — gitignored, 인스턴스별)
scripts/                           supervisor·status·tasks·inbox·cycle·config·prompt·report·
                                   checkpoint·finish·harvest_knowledge .py, *.sh
```

### 불변 규칙
- status.json 변경은 `scripts/status.py`로만(flock + atomic). 워커 기동은 `scripts/launch-worker.sh`로만.
- 타겟 레포 변경은 브랜치/PR만 — main 직접 push 금지.
- 자동화 도구는 Python 표준 라이브러리만(외부 패키지/jq 불사용).
- root 실행 시 claude 기동에 `IS_SANDBOX=1`. 인증은 구독 OAuth(종량 API 키 아님).

### 테스트
```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash tests/test_prepare_worktree.sh && bash tests/test_launch_worker.sh && bash tests/test_run_checks.sh
```
(레포 루트 `.tokendance-checks` 에 등록돼 있어 dogfood 시 `run-checks.sh`가 그대로 검증한다.)
