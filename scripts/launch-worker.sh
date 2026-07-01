#!/usr/bin/env bash
# 워커를 detached OS 프로세스로 기동. 마스터의 유일한 워커 기동 통로.
# 사용법: launch-worker.sh <task-id> [--resume]
#   --resume: 기록된 worker_session_id 로 직전 세션을 이어서 기동(--resume).
#             세션 id 가 없거나 세션파일이 사라졌으면(만료) 깨끗이 fresh 기동으로 폴백한다.
set -euo pipefail
TASK_ID=""
RESUME=0
while [ $# -gt 0 ]; do
  case "$1" in
    --resume) RESUME=1; shift ;;
    -*) echo "[launch-worker] unknown flag: $1" >&2; exit 2 ;;
    *) if [ -z "$TASK_ID" ]; then TASK_ID="$1"; else echo "[launch-worker] extra arg: $1" >&2; exit 2; fi; shift ;;
  esac
done
[ -n "$TASK_ID" ] || { echo "[launch-worker] usage: launch-worker.sh <task-id> [--resume]" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${TOKENDANCE_CLAUDE:?TOKENDANCE_CLAUDE (claude 바이너리 경로) 미설정}"

TASK_DIR="$ROOT/state/tasks/$TASK_ID"
LOG="$ROOT/state/workers/$TASK_ID.log"
mkdir -p "$ROOT/state/workers"

status_field() {  # status.json 의 한 필드(없으면 빈 문자열). "None" 도 빈 문자열로 정규화.
  local v
  v="$(python3 "$ROOT/scripts/status.py" get "$TASK_ID" --field "$1" 2>/dev/null || true)"
  [ "$v" = "None" ] && v=""
  printf '%s' "$v"
}

# 0) 멱등 가드: 기록된 워커 pid 가 아직 살아있으면 중복 기동하지 않는다(재투입 안전, 완료기준 #4).
EXISTING_PID="$(status_field worker_pid)"
if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
  echo "[launch-worker] worker already alive (pid=$EXISTING_PID) for $TASK_ID; skip relaunch" >&2
  echo "$EXISTING_PID"
  exit 0
fi

# 1) worktree 셋업 (실패하면 blocked 처리).
#    prepare-worktree 는 stdout 마지막 줄에 worktree 경로만 인쇄하고 진단은 stderr 로 보낸다.
#    stdout 을 캡처하고 stderr 만 LOG 로 흘린다.
if ! WORKTREE="$("$ROOT/scripts/prepare-worktree.sh" "$TASK_ID" 2>>"$LOG")"; then
  python3 "$ROOT/scripts/status.py" set "$TASK_ID" --state blocked
  echo "[launch-worker] prepare-worktree failed for $TASK_ID" >&2
  exit 1
fi

# 2) 세션 모드 결정: --resume 면 기록된 세션을 이어가고(--resume), 아니면/만료면 새 세션을 민팅한다.
#    세션 캡처 = 민팅: uuid 를 우리가 만들어 --session-id 로 넘기고 status 에 기록한다(완료기준 #1).
#    출력/세션파일 추적보다 신뢰성 높음(캡처 실패 지점 없음). --resume 분기는 완료기준 #2.
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
session_file_exists() {  # sid 의 세션 jsonl 이 존재하는가(slug 추론 없이 전역 유일 UUID 로 탐색).
  [ -n "$1" ] || return 1
  [ -n "$(find "$CLAUDE_DIR/projects" -name "$1.jsonl" -print -quit 2>/dev/null)" ]
}

SESSION_ARG1=""; SESSION_ARG2=""
RESUMING=0
if [ "$RESUME" -eq 1 ]; then
  SID="$(status_field worker_session_id)"
  if [ -z "$SID" ]; then
    echo "[launch-worker] $TASK_ID: --resume 요청됐으나 기록된 세션 id 없음 → fresh 기동" >&2
  elif ! session_file_exists "$SID"; then
    echo "[launch-worker] $TASK_ID: 세션 $SID 의 파일이 없음(만료) → fresh 기동으로 폴백" >&2
  else
    SESSION_ARG1="--resume"; SESSION_ARG2="$SID"; RESUMING=1
    echo "[launch-worker] $TASK_ID: 세션 $SID 이어받아 --resume 기동" >&2
  fi
fi
if [ "$RESUMING" -eq 0 ]; then
  SID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  python3 "$ROOT/scripts/status.py" set "$TASK_ID" --session "$SID"   # 기동 전 기록(크래시해도 보존)
  SESSION_ARG1="--session-id"; SESSION_ARG2="$SID"
  echo "[launch-worker] $TASK_ID: 새 세션 $SID 민팅 → --session-id 기동" >&2
fi

# 3) 워커 기동 — 격리된 worktree 를 cwd 로 (detached: setsid + IS_SANDBOX=1 + stdin 차단 + disown).
#    IS_SANDBOX=1 은 root에서 --dangerously-skip-permissions 를 허용하기 위해 필수(스파이크 확인).
#    ROOT/LOG/worker.md 경로는 모두 절대경로라 cd 후에도 안전하다.
#
#    실제 워커 pid 캡처: setsid 가 새 세션을 위해 fork 하므로 $!(=setsid)는 실제 워커 pid 와 어긋난다
#    (스파이크). 그래서 내부 bash 가 exec 직전 자기 pid($$)를 pidfile 에 기록하고, exec 로 claude 가
#    같은 pid 를 물려받게 한다 → status 의 worker_pid 가 진짜 워커 pid 가 되어 즉사 감지의 보조
#    신호(supervisor.detect_fast_crash)로 신뢰성 있게 쓰인다. (PROMPT/sysprompt 는 위치인자로 넘겨
#    재평가/쿼팅 문제를 피한다.)
# tokendance ROOT 절대경로를 워커에 전달(완료기준 #1): 워커 cwd 는 타겟 레포 worktree 라
# 상대경로 `scripts/`·`state/` 가 비-tokendance 레포에선 존재하지 않는다. 환경변수로 상속시키고
# (env 가 어떤 이유로 비어도) PROMPT 에도 병기해 이중화한다. checkpoint.py/finish.py 등은
# 스크립트 파일 위치로 root 를 잡으므로 절대경로 호출이면 cwd 와 무관하게 정상 동작한다.
export TOKENDANCE_ROOT="$ROOT"
# 타겟 레포가 제공하는 worker 환경(.tokendance-worktree.env)을 있으면 주입한다.
#   - 메인 레포 체크아웃($REPO)에서 읽는다 — manifest 와 같은 소스(추적/미추적 무관).
#     커밋하면 worktree 에도 보이지만, 호스트 운영 설정으로 미추적 드롭만 해도 동작한다.
#   - worktree 생성 후 source 하므로 $WORKTREE(이 워커의 worktree 절대경로)를 참조할 수 있다.
#   - set -a 로 export → setsid→env→claude 로 상속되어 워커의 모든 Bash 호출이 env 를 본다.
#   - 파일이 없는 레포(tokendance 도그푸딩 등)는 무영향(폴백 동작 그대로).
#   - 임의 코드 실행이지만 워커가 이미 같은 레포 빌드를 신뢰 실행하므로 트러스트 경계는 동일.
#     (예: npu-tools 는 이 파일로 LIBTORCH 를 자동 주입 → 워커가 수동 export 없이 libtorch 사용.)
export WORKTREE
REPO="$(status_field repo)"
WORKER_ENV_FILE="$REPO/.tokendance-worktree.env"
if [ -n "$REPO" ] && [ -f "$WORKER_ENV_FILE" ]; then
  set -a; . "$WORKER_ENV_FILE"; set +a
  echo "[launch-worker] $TASK_ID: 워커 환경 주입 ($WORKER_ENV_FILE)" >&2
fi
if [ "$RESUMING" -eq 1 ]; then
  PROMPT="이어서 진행하라. 너는 tokendance 워커이고 task id=${TASK_ID} 다. 직전 세션을 이어받았다. tokendance ROOT(절대경로)=${ROOT} (환경변수 TOKENDANCE_ROOT 에도 있음). cwd 는 타겟 레포 worktree 다. ${TASK_DIR}/steer.md 의 새 피드백과 ${TASK_DIR}/progress.md 를 먼저 확인하고 남은 일을 계속하라. 규칙은 ${ROOT}/prompts/worker.md."
else
  PROMPT="너는 tokendance 워커다. task id=${TASK_ID}. tokendance ROOT(절대경로)=${ROOT} (환경변수 TOKENDANCE_ROOT 에도 있음). cwd 는 타겟 레포 worktree 다. ${ROOT}/prompts/worker.md 를 읽고 그대로 따르라. 일감 명세: ${TASK_DIR}/task.md"
fi
SYSPROMPT="$(python3 "$ROOT/scripts/prompt.py" build worker)"
# Auto-recall: prepend accumulated library knowledge for this repo (best-effort — never block launch).
RECALL="$(python3 "$ROOT/scripts/harvest_knowledge.py" --root "$ROOT" --recall "$REPO" 2>/dev/null || true)"
if [ -n "$RECALL" ]; then
  SYSPROMPT="$SYSPROMPT

$RECALL"
fi
PIDFILE="$ROOT/state/workers/$TASK_ID.pid"
rm -f "$PIDFILE"   # 이전 (재)기동의 잔여 pidfile 제거
cd "$WORKTREE"
setsid bash -c '
  echo $$ > "$1"
  exec env IS_SANDBOX=1 "$2" -p "$3" \
    --append-system-prompt "$4" \
    "$5" "$6" \
    --dangerously-skip-permissions
' _ "$PIDFILE" "$TOKENDANCE_CLAUDE" "$PROMPT" "$SYSPROMPT" "$SESSION_ARG1" "$SESSION_ARG2" \
  >>"$LOG" 2>&1 < /dev/null &
WRAPPER_PID=$!
disown 2>/dev/null || true

# pidfile 이 써질 때까지 잠깐 대기(없으면 wrapper pid 로 fallback — 드리프트하지만 best-effort).
PID=""
for _ in $(seq 1 20); do
  if [ -s "$PIDFILE" ]; then PID="$(cat "$PIDFILE")"; break; fi
  sleep 0.1
done
[ -n "$PID" ] || PID="$WRAPPER_PID"

# 4) 상태를 running 으로 기록(+launched_at: 즉사 grace window 기준점) + 즉시 heartbeat
#    (갓 띄운 워커가 stale 로 오판되지 않게).
python3 "$ROOT/scripts/status.py" set "$TASK_ID" --state running --pid "$PID" --launched-now
python3 "$ROOT/scripts/status.py" heartbeat "$TASK_ID"
echo "$PID"
