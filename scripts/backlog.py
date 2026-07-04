#!/usr/bin/env python3
"""아이디어 백로그: state/backlog/<id>.json 에 엔트리(파일당 1건)를 쌓고, 태그로
분류·조회하고, 성숙하면 tasks.create_task 로 task 로 승격한다. stdlib only.

저장 패턴은 inbox.py(파일큐: 충돌 회피 파일명) + status.py(atomic write) 를 재사용한다.
엔트리 스키마: id, created(UTC iso), text, tags(list), status(open|promoted|dropped),
promoted_task_id(nullable). id = <ts>-<slug> 로 파일명이자 조회 키.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks as TK
import status as S

STATUSES = ("open", "promoted", "dropped")


def _dir(root):
    p = os.path.join(root, "state", "backlog")
    os.makedirs(p, exist_ok=True)
    return p


def _path(root, entry_id):
    return os.path.join(_dir(root), entry_id + ".json")


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:32] or "idea"


def _now():
    # (compact ts with microseconds → 충돌 없는 파일명, readable ISO → created 필드)
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"
    return ts, now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path, data):
    """status.py 와 동일: tempfile + fsync + os.replace 로 torn write 방지."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".backlog.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _load(root, entry_id):
    with open(_path(root, entry_id)) as f:
        return json.load(f)


def _save(root, entry):
    _atomic_write(_path(root, entry["id"]), entry)
    return entry


def add(root, text, tags=None):
    """엔트리 생성 → id 반환. 같은 순간 연속 add 도 파일명 충돌 없이 전부 보존."""
    ts, created = _now()
    base = f"{ts}-{_slug(text)}"
    entry_id = base
    n = 1
    while os.path.exists(_path(root, entry_id)):   # 동일 마이크로초 충돌 방어(덮어쓰기 금지)
        entry_id = f"{base}-{n}"
        n += 1
    entry = {
        "id": entry_id,
        "created": created,
        "text": text,
        "tags": _norm_tags(tags or []),
        "status": "open",
        "promoted_task_id": None,
    }
    _save(root, entry)
    return entry_id


def get(root, entry_id):
    """엔트리 전체를 반환. 없으면 ValueError(show 백엔드)."""
    try:
        return _load(root, entry_id)
    except FileNotFoundError:
        raise ValueError(f"no such backlog entry: {entry_id}")


def ls(root, tag=None, status=None):
    """엔트리 목록(id 오름차순 = 생성순). tag/status 필터는 AND 조합. 기본은 전체."""
    out = []
    d = _dir(root)
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(d, name)) as f:
            e = json.load(f)
        if tag is not None and tag not in e.get("tags", []):
            continue
        if status is not None and e.get("status") != status:
            continue
        out.append(e)
    return out


def _norm_tags(tags):
    """공백 정리 + 빈 태그 제거 + dedup(입력 순서 보존)."""
    seen, out = set(), []
    for t in tags:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def tag(root, entry_id, tags, remove=False):
    """태그 추가(dedup) 또는 제거. 갱신된 엔트리 반환."""
    e = get(root, entry_id)
    cur = e.get("tags", [])
    add_or_rm = _norm_tags(tags)
    if remove:
        e["tags"] = [t for t in cur if t not in add_or_rm]
    else:
        e["tags"] = _norm_tags(cur + add_or_rm)
    return _save(root, e)


def drop(root, entry_id):
    """status=dropped. 갱신된 엔트리 반환."""
    e = get(root, entry_id)
    e["status"] = "dropped"
    return _save(root, e)


def _title(text):
    """task.md 헤더용 단일 라인 title(첫 줄, 과하게 길면 컷)."""
    first = (text or "").strip().splitlines()[0] if text.strip() else "backlog idea"
    return first[:80]


def _task_md(entry, title):
    tags = ", ".join(entry.get("tags", [])) or "-"
    return (
        f"# {title}\n\n"
        f"## 출처\n"
        f"backlog {entry['id']} (tags: {tags})\n\n"
        f"## 내용\n"
        f"{entry['text']}\n\n"
        f"## 완료 기준\n"
    )


def promote(root, entry_id, repo, task_id=None):
    """엔트리를 task 로 승격: task 생성 + task.md 에 원문 심기 + 엔트리 표시. task_id 반환.

    이미 promoted 면 거부(중복 task 방지). dropped→promote 는 허용(되살리기).
    backlog.py 안에서 완결한다 — td.py 가 backlog 를 import 하므로 역방향 의존 금지.
    """
    e = get(root, entry_id)
    if e.get("status") == "promoted":
        raise ValueError(
            f"backlog {entry_id} is already promoted → task {e.get('promoted_task_id')}")
    title = _title(e["text"])
    if task_id is None:
        task_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{_slug(e['text'])}"
    TK.create_task(root, task_id, title=title, repo=os.path.abspath(repo))
    # create_task 가 스캐폴드한 task.md 를 backlog 원문+출처로 덮어쓴다.
    with open(os.path.join(S.task_dir(root, task_id), "task.md"), "w") as f:
        f.write(_task_md(e, title))
    e["status"] = "promoted"
    e["promoted_task_id"] = task_id
    _save(root, e)
    return task_id


def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="아이디어 백로그")
    ap.add_argument("--root", default=_default_root())
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("text")
    p.add_argument("--tag", action="append", default=[], dest="tags")

    p = sub.add_parser("ls")
    p.add_argument("--tag")
    p.add_argument("--status", choices=STATUSES)

    p = sub.add_parser("show")
    p.add_argument("id")

    p = sub.add_parser("tag")
    p.add_argument("id")
    p.add_argument("tags", nargs="+")
    p.add_argument("--remove", action="store_true")

    p = sub.add_parser("promote")
    p.add_argument("id")
    p.add_argument("--repo", required=True)
    p.add_argument("--id", dest="task_id", default=None)

    p = sub.add_parser("drop")
    p.add_argument("id")

    args = ap.parse_args(argv)
    if args.cmd == "add":
        print(add(args.root, args.text, args.tags))
    elif args.cmd == "ls":
        for e in ls(args.root, tag=args.tag, status=args.status):
            print(f"{e['id']}\t{e['status']}\t{','.join(e['tags'])}\t{e['text']}")
    elif args.cmd == "show":
        print(json.dumps(get(args.root, args.id), ensure_ascii=False, indent=2))
    elif args.cmd == "tag":
        print(json.dumps(tag(args.root, args.id, args.tags, remove=args.remove),
                         ensure_ascii=False))
    elif args.cmd == "promote":
        print(promote(args.root, args.id, args.repo, task_id=args.task_id))
    elif args.cmd == "drop":
        print(json.dumps(drop(args.root, args.id), ensure_ascii=False))


if __name__ == "__main__":
    main()
