#!/usr/bin/env python3
"""워커 knowledge.md 의 "## 지식:" 블록을 수확해 library 로 승격한다.

진실의 원천은 ledger(`library/.harvest-ledger.json`). library 파일들
(playbooks/<slug>.md, repos/<repo>.md, index.md)은 ledger 의 순수 투영으로
매 실행 재렌더되므로 멱등하다. knowledge.md 는 읽기만 하고 건드리지 않는다(파일 소유 규약).

블록 형식 (worker.md 와 일치):
    ## 지식: <제목>
    scope: repo | playbook        (선택, 메타)
    repo: <레포명>                 (선택, 메타)
    tags: a, b                     (선택, 메타)
    summary: 한 줄 요약            (선택, 메타)
    <빈 줄>
    <본문 markdown ...>
헤딩 바로 아래 연속하는 `key: value` 라인만 메타로 인식하며, 첫 빈 줄(또는
메타가 아닌 줄) 이후부터 본문이다. 블록은 다음 `## ` 헤딩 또는 EOF 에서 끝난다.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re

try:
    from scripts import config
except ImportError:
    import config

# ── tier (지식 등급) ──
# 확실한 1급은 primary, 사서가 코드기반으로 채운 불확실 신규는 candidate(격리, 사람 승인 전).
# 하위호환: tier 필드가 없는(레거시) 엔트리는 primary 로 취급한다.
TIER_PRIMARY = "primary"
TIER_CANDIDATE = "candidate"

# harvest 와 librarian(사서)이 ledger 를 동시에 만지지 않도록 직렬화하는 락 파일명.
LEDGER_LOCK_NAME = ".harvest-ledger.lock"

_HEADING_RE = re.compile(r"^##\s+지식:\s*(.*?)\s*$")
_META_KEYS = ("scope", "repo", "tags", "summary")
_META_RE = re.compile(r"^([A-Za-z_]+):\s*(.*?)\s*$")


def parse_knowledge_blocks(text):
    """knowledge.md 텍스트에서 지식 블록 목록을 추출한다."""
    lines = text.splitlines()
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        m = _HEADING_RE.match(lines[i])
        if not m:
            i += 1
            continue
        title = m.group(1).strip()
        i += 1
        # 본문 라인 모으기: 다음 `## ` 헤딩 또는 EOF 까지.
        section = []
        while i < n and not lines[i].startswith("## "):
            section.append(lines[i])
            i += 1
        meta, body_lines = _split_meta(section)
        blocks.append({
            "title": title,
            "meta": meta,
            "body": "\n".join(body_lines).strip(),
        })
    return blocks


def _split_meta(section):
    """헤딩 직후 연속하는 메타 라인을 분리한다."""
    meta = {}
    idx = 0
    for idx in range(len(section)):
        line = section[idx]
        mm = _META_RE.match(line)
        if mm and mm.group(1).lower() in _META_KEYS:
            meta[mm.group(1).lower()] = mm.group(2).strip()
            continue
        break
    else:
        idx = len(section)
        return meta, []
    return meta, section[idx:]


# --- 분류 / 슬러그 ----------------------------------------------------------

def slugify(title):
    """파일명용 슬러그. 유니코드(한글) 단어 문자는 유지하고 나머지는 하이픈."""
    s = title.strip().lower()
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE)
    s = s.strip("-")
    return s or "untitled"


def anchor(title):
    """GitHub 스타일 in-file 앵커(repos/<repo>.md#앵커 링크용)."""
    a = title.strip().lower()
    a = re.sub(r"[^\w\s-]", "", a, flags=re.UNICODE)
    a = re.sub(r"\s+", "-", a)
    return a


def classify(block, task_repo=""):
    """블록을 (scope, repo) 로 분류한다.

    메타 scope 우선. scope 미지정이면 휴리스틱: meta repo 있으면 repo,
    아니면 playbook(기본=범용). scope=repo 인데 repo 미지정이면 task 의
    status.json repo 필드로 폴백.
    """
    meta = block.get("meta", {})
    scope = meta.get("scope", "").strip().lower()
    repo = meta.get("repo", "").strip()
    if not scope:
        scope = "repo" if repo else "playbook"
    if scope == "repo":
        repo = repo or (task_repo or "").strip()
        if not repo:
            # repo 를 알 수 없으면 안전하게 playbook 으로 강등.
            return "playbook", ""
        return "repo", repo
    return "playbook", ""


def _entry_key(scope, repo, slug):
    if scope == "repo":
        return f"repo:{repo}:{slug}"
    return f"playbook:{slug}"


# --- tier 헬퍼 ---------------------------------------------------------------

def entry_tier(entry):
    """엔트리의 tier 를 정규화해 반환. 누락/미지정/알수없음 → primary(하위호환)."""
    t = (entry.get("tier") or TIER_PRIMARY)
    return TIER_CANDIDATE if t == TIER_CANDIDATE else TIER_PRIMARY


def is_candidate(entry):
    return entry_tier(entry) == TIER_CANDIDATE


# --- recall (읽기 전용, 워커 기동 시 library 리콜) ----------------------------

def _recall_line(e):
    tags = f" [{e['tags']}]" if e.get("tags") else ""
    summ = f" — {e['summary']}" if e.get("summary") else ""
    return f"- {e['title']}{tags}{summ}  (→ library/{e['dest']})"


def _repo_key(name):
    return os.path.basename(os.path.normpath(name or "")).lower()


def recall_block(root, repo):
    """Read-only, best-effort. Compact library recall for a worker on `repo`.
    Returns "" if the ledger is empty/unreadable or nothing matches."""
    try:
        entries = load_ledger(root).get("entries", {})
    except (OSError, ValueError):
        return ""
    target = _repo_key(repo)
    primary = [e for e in entries.values() if not is_candidate(e)]
    repo_es = sorted([e for e in primary if e.get("scope") == "repo"
                      and _repo_key(e.get("repo")) == target],
                     key=lambda x: x.get("slug", ""))
    pb_es = sorted([e for e in primary if e.get("scope") == "playbook"],
                   key=lambda x: x.get("slug", ""))
    if not repo_es and not pb_es:
        return ""
    try:
        cap = config.get_int("RECALL_MAX_ENTRIES", r=root)
    except Exception:
        cap = 20
    chosen_repo = repo_es[:cap]                      # repo-scoped prioritized
    chosen_pb = pb_es[:max(0, cap - len(chosen_repo))]
    dropped = (len(repo_es) - len(chosen_repo)) + (len(pb_es) - len(chosen_pb))
    out = ["## 참고 지식 (library) — 관련 있어 보이면 해당 파일을 Read 하라"]
    if chosen_repo:
        out.append(f"[이 레포: {os.path.basename(os.path.normpath(repo or ''))}]")
        out += [_recall_line(e) for e in chosen_repo]
    if chosen_pb:
        out.append("[공통 플레이북]")
        out += [_recall_line(e) for e in chosen_pb]
    if dropped:
        out.append(f"(+{dropped}개 더 — library/index.md 참고)")
    return "\n".join(out)


# --- ledger 락 (harvest ↔ librarian 직렬화) ----------------------------------

def _lock_path(root):
    return os.path.join(root, "library", LEDGER_LOCK_NAME)


@contextlib.contextmanager
def ledger_lock(root):
    """ledger 읽기-수정-재렌더 구간을 flock(LOCK_EX)으로 직렬화한다.

    harvest 와 librarian 이 같은 락 파일(`library/.harvest-ledger.lock`)을 잡으므로
    한쪽이 ledger 를 편집/재렌더하는 동안 다른 쪽은 대기한다(경합 시 ledger 손상 방지).
    """
    os.makedirs(os.path.join(root, "library"), exist_ok=True)
    f = open(_lock_path(root), "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


# --- ledger -----------------------------------------------------------------

def _ledger_path(root):
    return os.path.join(root, "library", ".harvest-ledger.json")


def load_ledger(root):
    p = _ledger_path(root)
    if not os.path.exists(p):
        return {"version": 1, "entries": {}}
    with open(p) as f:
        return json.load(f)


def save_ledger(root, ledger):
    p = _ledger_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
        f.write("\n")


# --- 수확 -------------------------------------------------------------------

def _iter_task_logs(root):
    # active(tasks/) + done(tasks-done/) 양쪽의 knowledge.md 를 수확한다. done 은 전용
    # 디렉토리로 분리되므로 여기서도 양쪽을 봐야 완료 task 의 지식을 놓치지 않는다.
    seen = set()
    for base_name in ("tasks", "tasks-done"):
        base = os.path.join(root, "state", base_name)
        if not os.path.isdir(base):
            continue
        for tid in sorted(os.listdir(base)):
            if tid in seen:
                continue
            log = os.path.join(base, tid, "knowledge.md")
            status = os.path.join(base, tid, "status.json")
            if not (os.path.exists(log) and os.path.exists(status)):
                continue
            seen.add(tid)
            repo = ""
            try:
                with open(status) as f:
                    repo = json.load(f).get("repo", "") or ""
            except (OSError, ValueError):
                repo = ""
            with open(log) as f:
                yield tid, repo, f.read()


def _build_entry(block, scope, repo, slug):
    meta = block.get("meta", {})
    if scope == "repo":
        dest = os.path.join("repos", f"{slugify(repo)}.md")
    else:
        dest = os.path.join("playbooks", f"{slug}.md")
    return {
        "title": block["title"],
        "scope": scope,
        "repo": repo or None,
        "slug": slug,
        "dest": dest,
        "anchor": anchor(block["title"]) if scope == "repo" else None,
        "summary": meta.get("summary", ""),
        "tags": meta.get("tags", ""),
        "body": block["body"],
        "tier": TIER_PRIMARY,   # 워커 log 에서 수확한 지식은 1급(사람이 검증한 작업 산출)
        "sources": [],
    }


def harvest(root):
    """모든 워커 knowledge.md 를 스캔해 ledger 를 갱신하고 library 를 재렌더한다.

    librarian(사서)과 같은 flock 으로 직렬화한다 — 사서가 ledger 를 편집/재렌더하는
    중간에 harvest 가 끼어들어 서로의 변경을 덮어쓰지 않게.
    """
    with ledger_lock(root):
        return _harvest_locked(root)


def _harvest_locked(root):
    ledger = load_ledger(root)
    entries = ledger.setdefault("entries", {})
    summary = {"created": [], "updated": [], "skipped": []}

    for tid, task_repo, text in _iter_task_logs(root):
        for block in parse_knowledge_blocks(text):
            scope, repo = classify(block, task_repo)
            slug = slugify(block["title"])
            key = _entry_key(scope, repo, slug)
            new = _build_entry(block, scope, repo, slug)
            existing = entries.get(key)
            if existing is None:
                new["sources"] = [tid]
                entries[key] = new
                summary["created"].append(key)
            elif existing.get("body") != new["body"]:
                sources = list(existing.get("sources", []))
                if tid not in sources:
                    sources.append(tid)
                new["sources"] = sources
                entries[key] = new
                summary["updated"].append(key)
            else:
                sources = existing.setdefault("sources", [])
                if tid not in sources:
                    sources.append(tid)
                summary["skipped"].append(key)

    save_ledger(root, ledger)
    _render_library(root, ledger)
    return summary


# --- 렌더링 -----------------------------------------------------------------

def _render_library(root, ledger):
    lib = os.path.join(root, "library")
    entries = ledger.get("entries", {})
    auto = "> ⚙️ 자동 생성: scripts/harvest_knowledge.py. 직접 편집하지 말 것.\n"
    # candidate tier 는 1급 라이브러리(playbooks/repos)에서 제외하고 candidates.md 로 격리한다.
    primary = [e for e in entries.values() if not is_candidate(e)]

    # playbooks: 엔트리당 파일 하나.
    playbooks = [e for e in primary if e["scope"] == "playbook"]
    if playbooks:
        os.makedirs(os.path.join(lib, "playbooks"), exist_ok=True)
    for e in playbooks:
        body = f"# {e['title']}\n\n{auto}\n"
        if e.get("tags"):
            body += f"*태그: {e['tags']}*\n\n"
        body += f"{e['body']}\n\n---\n*출처: {', '.join(e['sources'])}*\n"
        with open(os.path.join(lib, e["dest"]), "w") as f:
            f.write(body)

    # repos: dest 파일별로 엔트리들을 섹션으로 묶는다.
    repos = {}
    for e in primary:
        if e["scope"] == "repo":
            repos.setdefault(e["dest"], []).append(e)
    if repos:
        os.makedirs(os.path.join(lib, "repos"), exist_ok=True)
    for dest, es in repos.items():
        es = sorted(es, key=lambda x: x["slug"])
        repo_name = es[0]["repo"]
        out = [f"# {repo_name} 지식 베이스\n", auto]
        for e in es:
            out.append(f"\n## {e['title']}\n")
            if e.get("tags"):
                out.append(f"*태그: {e['tags']}*\n")
            out.append(f"\n{e['body']}\n")
            out.append(f"\n*출처: {', '.join(e['sources'])}*\n")
        with open(os.path.join(lib, dest), "w") as f:
            f.write("".join(out))

    _render_candidates(root, ledger)
    _render_index(root, ledger)


def _render_candidates(root, ledger):
    """candidate tier 엔트리들을 단일 `library/candidates.md` 로 집계 렌더(사람 검토용).

    1급 라이브러리와 분리해 사람이 승인하기 전 지식을 한곳에서 본다. 항상 덮어써
    승격(promote)된 항목이 잔존하지 않게 한다.
    """
    lib = os.path.join(root, "library")
    cands = sorted([e for e in ledger.get("entries", {}).values() if is_candidate(e)],
                   key=lambda x: x["slug"])
    auto = "> ⚙️ 자동 생성: scripts/harvest_knowledge.py. 직접 편집하지 말 것.\n"
    out = ["# 후보 지식 (검토 대기)\n", auto,
           "\n사서(librarian)가 코드 기반으로 보강했으나 불확실한 신규 지식. "
           "사람 승인 시 1급으로 승격된다.\n"]
    if not cands:
        out.append("\n(아직 없음)\n")
    for e in cands:
        out.append(f"\n## {e['title']}\n")
        scope_label = (f"repo:{e['repo']}" if e["scope"] == "repo" else "playbook")
        out.append(f"*scope: {scope_label}*")
        if e.get("tags"):
            out.append(f" · *태그: {e['tags']}*")
        out.append("\n")
        if e.get("summary"):
            out.append(f"\n> {e['summary']}\n")
        out.append(f"\n{e['body']}\n")
        out.append(f"\n*출처: {', '.join(e.get('sources', []))}*\n")
    with open(os.path.join(lib, "candidates.md"), "w") as f:
        f.write("".join(out))


def _render_index(root, ledger):
    entries = ledger.get("entries", {})
    primary = [e for e in entries.values() if not is_candidate(e)]
    pb = sorted([e for e in primary if e["scope"] == "playbook"],
                key=lambda x: x["slug"])
    repo_entries = [e for e in primary if e["scope"] == "repo"]
    by_repo = {}
    for e in repo_entries:
        by_repo.setdefault(e["repo"], []).append(e)
    cands = sorted([e for e in entries.values() if is_candidate(e)],
                   key=lambda x: x["slug"])

    lines = [
        "# tokendance 지식 라이브러리 — 목차",
        "",
        "필요할 때 필요한 항목만 펼쳐 본다. (점진 탐색)",
        "",
        "> ⚙️ 이 파일은 scripts/harvest_knowledge.py 가 자동 생성한다. 직접 편집하지 말 것.",
        "",
        "## playbooks/   재사용 노하우",
    ]
    if pb:
        for e in pb:
            suffix = f" — {e['summary']}" if e.get("summary") else ""
            lines.append(f"- [{e['title']}]({e['dest']}){suffix}")
    else:
        lines.append("(아직 없음)")
    lines += ["", "## repos/       레포별 지식 베이스"]
    if by_repo:
        for repo in sorted(by_repo):
            lines.append(f"### {repo}")
            for e in sorted(by_repo[repo], key=lambda x: x["slug"]):
                suffix = f" — {e['summary']}" if e.get("summary") else ""
                link = f"{e['dest']}#{e['anchor']}"
                lines.append(f"- [{e['title']}]({link}){suffix}")
    else:
        lines.append("(아직 없음)")
    lines += ["", "## candidates    검토 대기 (사서 보강, 사람 승인 전)"]
    if cands:
        for e in cands:
            suffix = f" — {e['summary']}" if e.get("summary") else ""
            lines.append(f"- [{e['title']}](candidates.md#{anchor(e['title'])}){suffix}")
    else:
        lines.append("(아직 없음)")
    lines.append("")
    with open(os.path.join(root, "library", "index.md"), "w") as f:
        f.write("\n".join(lines))


# --- CLI --------------------------------------------------------------------

def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="워커 knowledge.md 의 '## 지식:' 블록을 library 로 승격(멱등).")
    ap.add_argument("--root", default=_default_root())
    ap.add_argument("--recall", metavar="REPO", default=None,
                    help="print the library recall block for REPO and exit (read-only)")
    args = ap.parse_args(argv)
    if args.recall is not None:
        print(recall_block(args.root, args.recall))
        return
    s = harvest(args.root)
    print(f"created={len(s['created'])} updated={len(s['updated'])} "
          f"skipped={len(s['skipped'])}")
    for kind in ("created", "updated"):
        for key in s[kind]:
            print(f"  {kind}: {key}")


if __name__ == "__main__":
    main()
