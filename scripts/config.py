#!/usr/bin/env python3
"""인스턴스 설정/경로/상수의 단일 소스.

`config.local.md`(gitignore)의 ``` 코드블록 안 KEY=VALUE 들을 읽는다.
프롬프트(md)에 환경/설정을 줄글로 적지 않고, 코드·도구가 여기서 조회한다.

  python3 scripts/config.py get SLACK_CHANNEL      # 값(없으면 빈 줄)
  python3 scripts/config.py get MAX_WORKERS --int  # 정수(미설정 시 기본)
"""
import argparse
import os

# 기본값(설정 미지정 시).
DEFAULTS = {
    "MAX_WORKERS": "1",        # worktree 격리 있으니 1 이상 가능
    "POLL_INTERVAL": "1800",   # base 틱 주기(초); idle 백오프는 supervisor 가 처리
    "SLACK_CHANNEL": "",       # 비면 Slack 연동 skip
    "MASTER_SESSION_MAX_CYCLES": "20",  # 마스터 세션을 이만큼 이어간 뒤 리셋(맥락은 롤링노트가 인계)
    "LIBRARIAN_HOUR_KST": "3",  # 사서(지식 큐레이션) 패스를 도는 KST 시각(새벽). idle 일 때만.
    "MASTER_MORNING_HOUR_KST": "7",  # 마스터 아침 루틴(완료 worktree GC + 일일 다이제스트) KST 시각.
    "POOL_MAX_TREES": "4",            # warm worktree slots per target repo
    "POOL_SHARED_SYMLINKS": ".venv",  # comma-sep dirs symlinked (shared) into each slot
    "POOL_TARGET_MAX_GB": "0",          # total target/ budget (GB) across slots; 0 = no size cap
    "POOL_TARGET_LOWWATER_GB": "0",     # evict down to this (GB); 0 = derive 0.8 * max
    "POOL_TARGET_IDLE_DAYS": "14",      # idle-sweep: evict idle slot target/ untouched ≥ this many days; 0 = off
    "POOL_TARGET_USE_CARGO_SWEEP": "0", # 1 = use `cargo sweep` for idle sweep when available
    "POOL_LEASE_TTL_HOURS": "48",  # reclaim a leased slot whose holder is gone or heartbeat older than this; 0 = off
}


def root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def paths(r=None):
    r = r or root()
    j = os.path.join
    return {
        "root": r,
        "state": j(r, "state"),
        "tasks": j(r, "state", "tasks"),              # active(queued/running/.../failed)
        "tasks_done": j(r, "state", "tasks-done"),    # done 전용(분리 보관)
        "tasks_archive": j(r, "state", "tasks-archive"),  # 수동 archive 만
        "inbox_pending": j(r, "state", "inbox", "pending"),
        "inbox_processed": j(r, "state", "inbox", "processed"),
        "reports": j(r, "state", "reports"),
        "workers": j(r, "state", "workers"),
        "worktrees": j(r, "state", "worktrees"),
        "pool": j(r, "state", "pool"),
        "library": j(r, "library"),
        "config_local": j(r, "config.local.md"),
        "slack_cursor": j(r, "state", "slack.cursor"),
        "master_notes": j(r, "state", "master-notes.md"),
    }


def _parse(text):
    """config.local.md 텍스트에서 KEY=VALUE 들을 추출(주석/공백/코드펜스 무시)."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("```"):
            continue
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.split("#", 1)[0].strip()
    return out


def load(r=None):
    p = paths(r)["config_local"]
    values = dict(DEFAULTS)
    try:
        with open(p) as f:
            values.update(_parse(f.read()))
    except FileNotFoundError:
        pass
    return values


def get(key, default="", r=None):
    return load(r).get(key, default)


def get_int(key, r=None):
    try:
        return int(get(key, DEFAULTS.get(key, "0"), r))
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, "0"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("key")
    g.add_argument("--int", action="store_true")
    p = sub.add_parser("paths")
    p.add_argument("name", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd == "get":
        print(get_int(args.key) if args.int else get(args.key))
    elif args.cmd == "paths":
        pp = paths()
        if args.name:
            print(pp[args.name])
        else:
            for k, v in pp.items():
                print(f"{k}\t{v}")


if __name__ == "__main__":
    main()
