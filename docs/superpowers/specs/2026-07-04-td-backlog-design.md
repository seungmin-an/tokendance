# td backlog — 아이디어 백로그 설계

task: `2026-07-04-td-backlog`. 이 문서는 task.md 명세를 구현 가능한 수준으로 확정한다(스키마·인터페이스·모호점 해소).

## 목표
random-thoughts/아이디어를 1급 백로그로 쌓고, 태그로 분류·조회하고, 성숙하면 task 로 승격하는
`td backlog` 커맨드 그룹 + stdlib 백엔드 `scripts/backlog.py`. 지금은 master-notes 비공식 섹션이 유일한 백로그라 이를 대체/보완.

## 저장 모델
- 위치: `state/backlog/<id>.json` — 엔트리 1건당 파일 1개. `state/` 는 gitignore → 미추적.
- 파일명/`id` 규칙: `<ts>-<slug>` (예 `20260704T031200123456Z-fix-rope-kernel`).
  - `ts` = `%Y%m%dT%H%M%S` + 마이크로초 6자리 + `Z` — inbox.py 처럼 같은 순간 연속 add 도 충돌 없음. 충돌 시 `-<n>` 접미(덮어쓰기 금지).
  - `slug` = text 를 소문자화·비영숫자→`-`·32자 컷(`td._slug` 스타일).
- 쓰기: status.py 의 atomic write(tempfile + `os.replace`) 재사용 — 태그/승격/드롭이 read-modify-write 라 torn write 방지. flock 은 생략(운영자 단일 writer, YAGNI; inbox 수준 단순성).

### 엔트리 스키마
```json
{
  "id": "20260704T031200123456Z-fix-rope-kernel",
  "created": "2026-07-04T03:12:00Z",   // 읽기용 ISO(UTC), status._now() 포맷
  "text": "…원문…",
  "tags": ["kernel", "perf"],           // dedup, 입력 순서 보존
  "status": "open",                      // open | promoted | dropped
  "promoted_task_id": null               // promote 시 생성된 task id
}
```

## 인터페이스 (`td backlog <sub>`)
- `add "<text>" [--tag t ...]` → 엔트리 생성, id 출력.
- `ls [--tag t] [--status s]` → 표(id, created age, status, tags, text 요약). 필터는 AND 조합.
  - **기본(필터 없음) = 전체 표시**. `td task ls` 가 모든 state 를 보여주는 것과 동일한 관례. `--status open` 으로 좁힌다.
- `show <id>` → 전체 필드 표시.
- `tag <id> <tag ...> [--remove]` → 태그 추가/제거(멱등, dedup).
- `promote <id> --repo <path> [--id <task-id>]` → task 생성 + task.md 에 text 심기 + 엔트리 status=promoted, promoted_task_id 기록.
- `drop <id>` → status=dropped.
- `td backlog -h` / `td help` 트리에 backlog 그룹 노출.

## backlog.py 공개 함수 (test_backlog.py 가 직접 호출)
- `add(root, text, tags=None) -> id`
- `ls(root, tag=None, status=None) -> [entry]` (id 오름차순 = 생성순)
- `get(root, id) -> entry` (없으면 `ValueError`) — show 백엔드
- `tag(root, id, tags, remove=False) -> entry`
- `promote(root, id, repo, task_id=None) -> task_id`
- `drop(root, id) -> entry`
- 내부: `_dir/_path/_load/_save/_slug/_gen_id/_now/_default_root/main`

## promote 상세
backlog.py 안에서 완결(td 의존 없음 — td 가 backlog 를 import 하므로 역방향 금지):
1. `e = get(root, id)`. `status == "promoted"` 면 `ValueError`(이미 task 존재 → 중복 방지).
2. `title` = text 첫 줄을 단일 라인으로 잘라 헤더용으로.
3. `task_id` 미지정 시 `_gen_id(text)`(ts-slug, cmd_spawn 규칙과 동형).
4. `tasks.create_task(root, task_id, title=title, repo=abspath(repo))` — status.json init + 스캐폴딩.
5. `task.md` 를 backlog 원문+출처로 **덮어쓰기**(스캐폴드의 출처/완료기준 섹션 유지 + `## 내용` 에 원문):
   ```
   # {title}

   ## 출처
   backlog {id} (tags: {tags}) — promoted {now}

   ## 내용
   {text}

   ## 완료 기준
   ```
6. 엔트리 `status=promoted`, `promoted_task_id=task_id` 저장.

## 확정된 모호점 (assumptions)
1. **ls 기본 = 전체**(open 만 아님). 근거: `[--status open]` 은 옵션 필터 예시이고, sibling `td task ls` 도 전체를 보여준다.
2. **저장 포맷 = JSON**(inbox 의 `.md` 원문 대신). 근거: 스키마가 구조화 필드라 JSON 이 자연스럽고 read-modify-write 안전.
3. **promote 는 open 에서만**(이미 promoted 면 거부). dropped→promote 는 허용(되살리기).
4. **flock 미사용**. 운영자 단일 writer + atomic replace 로 충분(inbox 수준).

## 테스트
- `tests/test_backlog.py`(신규, `unittest_` 접두): add→ls(필터)→tag→show→promote→drop 라운드트립. tmp root, 부작용 없음. promote 가 실제 task 생성 + 엔트리 promoted 표시 검증.
- `tests/test_td.py` 에 `td backlog` 통합 테스트 추가(기존 스타일: tmp root, `td.main(["--root", tmp, "backlog", ...])`).
- 전체 통과: `python3 -m unittest discover -s tests -p 'test_*.py'`.

## 제약
- Python stdlib only. inbox/tasks/status 패턴 재사용.
- 변경은 worktree 브랜치 `tokendance/2026-07-04-td-backlog`. **동시 작업**: `td-review-cmd` 가 같은 td.py 를 만질 수 있음 → 새 블록 추가 위주, 기존 라인 수정 최소.
