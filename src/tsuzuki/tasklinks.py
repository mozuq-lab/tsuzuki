"""セッションをタスクに結び付ける。

台帳の行（sessions・places・doc_links・relations・derived_from・tasks・task_overrides）だけから計算し、
task_sessions を作り直して tasks.repo_resolved を書く。ファイルは読まない。
"""
import os
import re
import sqlite3
from typing import Dict, List, NamedTuple, Optional, Pattern, Sequence, Set, Tuple

from tsuzuki.store import set_meta
from tsuzuki.tasks import TASK_MD, ticket_numbers, within

# （規則、確かさ、画面の言葉）を強い順に。自動の規則は1つのセッションを1つのタスクにだけ結び付け、
# 複数のタスクに当たったら上の規則のタスクを選ぶ
RULES: Tuple[Tuple[str, str, str], ...] = (
    ("manual", "confirmed", "手で足した"),
    ("cwd", "confirmed", "フォルダーで開始"),
    ("wrote", "confirmed", "資料を書いた"),
    ("named_task_md", "confirmed", "task.md を渡した"),
    ("named_file", "strong", "資料を渡した"),
    ("read_task_md", "strong", "task.md を読んだ"),
    ("from_source", "strong", "元の会話から"),
    ("from_child", "strong", "子の会話から"),
    ("from_clear", "weak", "/clear の前の会話から"),
    ("same_branch", "weak", "同じブランチ"),
    ("ticket", "weak", "チケット番号"),
)
RULE_LABELS: Dict[str, str] = {rule: label for rule, _, label in RULES}
CONFIDENCE_LABELS = {"confirmed": "確定", "strong": "推定（強）", "weak": "推定（弱）"}
NAMED_PROMPTS = 3  # 最初のこの回数までの入力が名指したファイルだけを根拠にする
RELINK_KEY = "tasks:relink"  # meta のキー。手で足した・外した後、次の更新で結び付けを計算し直す

_RANK = {rule: i for i, (rule, _, _) in enumerate(RULES)}
_CONFIDENCE = {rule: confidence for rule, confidence, _ in RULES}
_CONFIDENCE_ORDER = ("confirmed", "strong", "weak")
# 「資料を書いた」に数える doc_links の rel。task_file は、タスクの置き場所の中の資料でないファイルへの書き込み
_WROTE_RELS = ("created", "edited", "task_file")
# /branch が custom_title と agent_name に書く自動の題名（ingest の choose_title と同じ見分け方）
_AUTO_TITLE = re.compile(r"\(Branch(?: \d+)?\)$")

Link = Tuple[str, str, str]  # (タスク, 規則, 確かさ)
Edges = Dict[str, List[Tuple[str, str]]]  # セッション → [(相手のセッション, つながりの確かさ)]


class _Task(NamedTuple):
    path: str  # フォルダーの絶対パス
    repo: Optional[str]  # task.md の repo
    ticket: Optional[str]
    created: Optional[str]


class _Session(NamedTuple):
    cwd: Optional[str]
    place: Optional[str]  # 場所のパス
    branch: Optional[str]
    started: Optional[str]
    ended: Optional[str]
    titles: Tuple[str, ...]  # 手動の題名（/branch の自動の題名を除く）


def link_tasks(conn: sqlite3.Connection, *, tasks_root: str, default_branches: Sequence[str]) -> int:
    """task_sessions を作り直し、tasks.repo_resolved を書いて、task_sessions の行の数を返す。
    計算の順は次のとおり：直接の根拠、引き継ぎ、リポジトリ、推定（弱）の規則。手で足した組（add）は表の
    1行目の直接の根拠（確定）で、引き継ぎの元、リポジトリ、同じブランチに数える。1つのセッションを1つのタスクに
    だけ結び付けるのは自動の規則だけで、手で足した組はその上に重ねる（自動の行と同じタスクなら置き換える）。"""
    root = os.path.normpath(tasks_root)
    tasks = {r[0]: _Task(os.path.normpath(r[1]), r[2], r[3], r[4])
             for r in conn.execute("SELECT name, path, repo, ticket, created FROM tasks")}
    sessions = _load_sessions(conn)
    overrides = conn.execute("SELECT task, session_id, action, at FROM task_overrides").fetchall()
    removed = {(task, sid) for task, sid, action, _ in overrides if action == "remove"}
    manual = {}  # type: Dict[str, List[Tuple[str, str]]]  セッション → [(手で足したタスク, 足した時刻)]
    for task, sid, action, at in overrides:
        if action == "add" and task in tasks and sid in sessions:
            manual.setdefault(sid, []).append((task, at))

    direct = _direct_links(conn, root, tasks, sessions, removed)
    linked = _inherit_links(conn, sessions, direct, manual, removed)
    trusted = _trusted_links(linked, manual)
    repos = _resolve_repos(root, tasks, sessions, trusted)
    weak = _weak_links(tasks, sessions, linked, trusted, repos, removed, default_branches)

    rows = {(task, sid): (rule, confidence)
            for sid, (task, rule, confidence) in list(linked.items()) + list(weak.items())}
    for sid, adds in manual.items():
        for task, _ in adds:
            rows[(task, sid)] = ("manual", "confirmed")  # 自動の行と同じタスクなら置き換える
    with conn:
        conn.execute("DELETE FROM task_sessions")
        conn.executemany("INSERT INTO task_sessions(task, session_id, rule, confidence) VALUES (?, ?, ?, ?)",
                         [key + value for key, value in sorted(rows.items())])
        conn.executemany("UPDATE tasks SET repo_resolved = ? WHERE name = ?",
                         [(repos[name], name) for name in sorted(repos)])
    return len(rows)


def set_override(conn: sqlite3.Connection, task: str, session_id: str, action: str, *, now_iso: str) -> None:
    """利用者が画面で足した（add）・外した（remove）組を残す。同じ組の前の指定は置き換える。
    結び付けは、次の更新（または呼び出し側の link_tasks）で計算し直す。"""
    if action not in ("add", "remove"):
        raise ValueError("action は add か remove にしてください: %r" % (action,))
    with conn:
        conn.execute("INSERT OR REPLACE INTO task_overrides(task, session_id, action, at) VALUES (?, ?, ?, ?)",
                     (task, session_id, action, now_iso))
        set_meta(conn, RELINK_KEY, "1")


def _load_sessions(conn: sqlite3.Connection) -> Dict[str, _Session]:
    """結び付けの対象のセッション。種類が empty のセッションは結び付けない。"""
    out = {}  # type: Dict[str, _Session]
    for sid, cwd, place, branch, started, ended, custom, agent in conn.execute(
            "SELECT s.id, s.cwd, p.path, s.branch, s.started_at, s.ended_at, s.custom_title, s.agent_name"
            " FROM sessions s LEFT JOIN places p ON p.id = s.place_id WHERE s.kind != 'empty'"):
        titles = tuple(x for x in (custom, agent) if x and not _AUTO_TITLE.search(x))
        out[sid] = _Session(os.path.normpath(cwd) if cwd else None, os.path.normpath(place) if place else None,
                            branch, started, ended, titles)
    return out


def _direct_links(conn: sqlite3.Connection, root: str, tasks: Dict[str, _Task], sessions: Dict[str, _Session],
                  removed: Set[Tuple[str, str]]) -> Dict[str, Link]:
    """直接の根拠（表の上から、手で足した以外の5つ）。セッションごとに最も強い規則のタスクを1つ選び、同じ規則なら
    根拠の時刻（cwd はセッションの開始）が最も新しいタスクを選ぶ。shared/ はタスクではないので数えない。"""
    folders = {t.path: name for name, t in tasks.items()}
    found = {}  # type: Dict[str, List[Tuple[int, str, str]]]  セッション → [(規則の順位, 根拠の時刻, タスク)]

    def add(sid: str, rule: str, ts: Optional[str], task: Optional[str]) -> None:
        if task is not None and (task, sid) not in removed:
            found.setdefault(sid, []).append((_RANK[rule], ts or "", task))

    for sid, s in sessions.items():
        if s.cwd:
            add(sid, "cwd", s.started, _folder_task(s.cwd, folders))
    low, high = root.rstrip("/") + "/", root.rstrip("/") + "0"  # root の下のパス（"/" の次の文字が "0"）
    for sid, path, rel, ts, index in conn.execute(
            "SELECT session_id, path, rel, ts, prompt_index FROM doc_links WHERE path >= ? AND path < ?", (low, high)):
        task = _folder_task(path, folders) if sid in sessions else None
        if task is None:
            continue
        cover = path == os.path.join(tasks[task].path, TASK_MD)
        if rel in _WROTE_RELS:
            add(sid, "wrote", ts, task)
        elif rel == "named" and index is not None and index <= NAMED_PROMPTS:
            add(sid, "named_task_md" if cover else "named_file", ts, task)
        elif rel == "read" and cover:
            add(sid, "read_task_md", ts, task)
    direct = {}  # type: Dict[str, Link]
    for sid, cands in found.items():
        best = min(rank for rank, _, _ in cands)
        _, _, task = max(c for c in cands if c[0] == best)
        rule = RULES[best][0]
        direct[sid] = (task, rule, _CONFIDENCE[rule])
    return direct


def _folder_task(path: str, folders: Dict[str, str]) -> Optional[str]:
    """path を含む（path そのものでもよい）タスクのフォルダーの名前。"""
    d = path
    while True:
        name = folders.get(d)
        if name is not None:
            return name
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _inherit_links(conn: sqlite3.Connection, sessions: Dict[str, _Session], direct: Dict[str, Link],
                   manual: Dict[str, List[Tuple[str, str]]], removed: Set[Tuple[str, str]]) -> Dict[str, Link]:
    """引き継ぎの規則を、自動の直接の根拠がないセッションに当てる。相手が確定か推定（強）で結び付いているときだけ
    引き継ぎ、確かさは表の確かさ、相手の確かさ、つながり（relations）自体の確かさのうち最も低いものにする
    （前後5分の推定（弱）の親子からは推定（弱）になり、ほかへは引き継がない）。分岐と続きの会話は何段でもさかのぼり、
    親子と /clear は1段だけ見る（親から子へは親の分岐の結び付きまで、/clear は前の会話の子までの結び付きを渡す）。
    候補が複数なら、引き継いだ確かさの高い順、表の順、分岐を親より先、相手の根拠の規則の順（子が複数のタスクに
    分かれたとき）、相手の新しい順に選ぶ（推定（弱）が、推定（強）の候補を差し置いて1つの枠を取らない）。
    手で足した組も直接の根拠（確定）として引き継がせる。手で足したセッション自身の自動の結び付きは、自動の規則だけで
    決める（手で足しても、自動の結び付きは消えない）。"""
    # 直接の根拠を持つ会話が引き継がせる結び付き：自動の直接の根拠のタスク、なければ最も新しく手で足したタスク
    evidence = dict(direct)
    for sid, adds in manual.items():
        if sid not in evidence:
            task, _ = max(adds, key=lambda add: (add[1], add[0]))
            evidence[sid] = (task, "manual", "confirmed")
    # 相手のセッションとつながりの確かさ。並びを決めておく（元・親・前の会話が複数あっても、毎回同じものを選ぶ）
    sources, parents, children = {}, {}, {}  # type: Edges, Edges, Edges
    for src, dst, typ, confidence in conn.execute(
            "SELECT src, dst, type, confidence FROM relations"
            " WHERE type IN ('fork', 'continued', 'child') ORDER BY src, dst"):
        if typ == "child":
            parents.setdefault(dst, []).append((src, confidence))
            children.setdefault(src, []).append((dst, confidence))
        else:
            sources.setdefault(dst, []).append((src, confidence))
    cleared = {}  # type: Edges  /clear の後の会話 → 前の会話（記録の derived-from なので確定）
    for sid, source in conn.execute("SELECT session_id, source_id FROM derived_from WHERE via = 'clear'"
                                    " ORDER BY session_id, source_id"):
        cleared.setdefault(sid, []).append((source, "confirmed"))

    todo = sorted(sid for sid in sessions if sid not in direct)
    cands = {}  # type: Dict[str, List[Tuple[Tuple[int, ...], str, str, Link]]]  セッション → [(鍵, 相手の開始, 相手, 結び付き)]

    def offer(sid: str, link: Link, rule: str, relation: str, step: int, partner: str) -> None:
        """相手 partner の結び付き link を、つながりの確かさ relation で引き継ぐ候補にする。step は規則を当てる順。"""
        task, partner_rule, confidence = link
        if confidence == "weak" or (task, sid) in removed:
            return
        inherited = _weakest(_CONFIDENCE[rule], confidence, relation)
        key = (_CONFIDENCE_ORDER.index(inherited), _RANK[rule], step, _RANK[partner_rule])
        cands.setdefault(sid, []).append((key, sessions[partner].started or "", partner, (task, rule, inherited)))

    def chosen() -> Dict[str, Link]:
        """ここまでの候補から、セッションごとに1つ（鍵の小さい順、同じなら相手の新しい順）。"""
        out = {}  # type: Dict[str, Link]
        for sid, options in cands.items():
            top = min(option[0] for option in options)
            out[sid] = max((o for o in options if o[0] == top), key=lambda o: (o[1], o[2]))[3]
        return out

    # 分岐元・続きの会話の元：直接の根拠を持つ会話まで
    for sid in todo:
        found = _nearest_direct(sid, sources, evidence)
        if found is not None:
            partner, link, relation = found
            offer(sid, link, "from_source", relation, 0, partner)
    # 親の結び付き（直接の根拠か、分岐の結び付きまで）を子へ
    known = chosen()
    for sid in todo:
        for parent, relation in parents.get(sid, ()):
            link = evidence.get(parent) or known.get(parent)
            if link is not None:
                offer(sid, link, "from_source", relation, 1, parent)
    # 直接の根拠を持つ子から親へ
    for sid in todo:
        for kid, relation in children.get(sid, ()):
            if kid in evidence:
                offer(sid, evidence[kid], "from_child", relation, 2, kid)
    # /clear の前の会話（直接の根拠か、子の会話までの結び付き）から
    known = chosen()
    for sid in todo:
        for source, relation in cleared.get(sid, ()):
            link = evidence.get(source) or known.get(source)
            if link is not None:
                offer(sid, link, "from_clear", relation, 3, source)
    linked = dict(direct)
    linked.update(chosen())
    return linked


def _nearest_direct(sid: str, sources: Edges, evidence: Dict[str, Link]) -> Optional[Tuple[str, Link, str]]:
    """分岐元・続きの会話の元を近い順にたどり、直接の根拠を持つ最初の会話と、それが引き継がせる結び付きと、そこまでの
    つながりの確かさ（途中で最も低いもの）を返す。一度見た会話には戻らない（輪になっていても止まり、手で足した
    セッションが自分の結び付きを引き継ぐこともない）。"""
    seen, level = {sid}, [(sid, "confirmed")]
    while level:
        upper = []  # type: List[Tuple[str, str]]
        for s, path in level:
            for source, relation in sources.get(s, ()):
                if source in seen:
                    continue
                reach = _weakest(path, relation)
                if source in evidence:
                    return source, evidence[source], reach
                seen.add(source)
                upper.append((source, reach))
        level = upper
    return None


def _trusted_links(linked: Dict[str, Link], manual: Dict[str, List[Tuple[str, str]]]) -> Dict[Tuple[str, str], str]:
    """確定か推定（強）の結び付き（自動の規則と手で足した組）。(タスク, セッション) → 確かさ。"""
    trusted = {(task, sid): confidence for sid, (task, _, confidence) in linked.items() if confidence != "weak"}
    for sid, adds in manual.items():
        for task, _ in adds:
            trusted[(task, sid)] = "confirmed"
    return trusted


def _weakest(*confidences: str) -> str:
    return max(confidences, key=_CONFIDENCE_ORDER.index)


def _resolve_repos(root: str, tasks: Dict[str, _Task], sessions: Dict[str, _Session],
                   trusted: Dict[Tuple[str, str], str]) -> Dict[str, Optional[str]]:
    """タスクのリポジトリ。task.md の repo があればそれ。なければ、確定か推定（強）で結び付いたセッション
    （手で足したものを含む）の場所（タスクの置き場所の中の場所と「一時」を除く）のうち、セッションの数が最も多いもの、
    同じ数なら最後の動き（ended_at）が新しいもの。どれもなければ None。"""
    tally = {}  # type: Dict[str, Dict[str, Tuple[int, str]]]  タスク → 場所 → (セッションの数, 最後の動き)
    for task, sid in trusted:
        s = sessions[sid]
        if not s.place or not os.path.isabs(s.place) or within(s.place, root):
            continue
        count, last = tally.setdefault(task, {}).get(s.place, (0, ""))
        tally[task][s.place] = (count + 1, max(last, s.ended or ""))
    repos = {}  # type: Dict[str, Optional[str]]
    for name, t in tasks.items():
        places = tally.get(name)
        if t.repo:
            repos[name] = os.path.normpath(t.repo)
        elif places:
            repos[name] = max(places, key=lambda p: places[p] + (p,))
        else:
            repos[name] = None
    return repos


def _weak_links(tasks: Dict[str, _Task], sessions: Dict[str, _Session], linked: Dict[str, Link],
                trusted: Dict[Tuple[str, str], str], repos: Dict[str, Optional[str]], removed: Set[Tuple[str, str]],
                default_branches: Sequence[str]) -> Dict[str, Link]:
    """推定（弱）の規則（同じブランチ、チケット番号）を、自動の規則でまだどのタスクにも結び付いていないセッションに
    当てる。セッションの場所のパスがタスクのリポジトリと同じときだけ見る。"""
    by_repo = {}  # type: Dict[str, List[str]]
    for name in sorted(repos):
        repo = repos[name]
        if repo:
            by_repo.setdefault(repo, []).append(name)
    confirmed = {}  # type: Dict[str, Set[Tuple[str, str]]]  タスク → 確定で結び付いたセッションの (場所, ブランチ)
    for (task, sid), confidence in trusted.items():
        s = sessions[sid]
        if confidence == "confirmed" and s.place and s.branch:
            confirmed.setdefault(task, set()).add((s.place, s.branch))
    numbers = {name: _number_pattern(ticket_numbers(t.ticket)) for name, t in tasks.items()}
    defaults = set(default_branches) | {"HEAD"}
    out = {}  # type: Dict[str, Link]
    for sid in sorted(sessions):
        s = sessions[sid]
        if sid in linked or not s.place:
            continue
        hits = {}  # type: Dict[str, str]  タスク → 規則
        for task in by_repo.get(s.place, ()):
            # 手で足したタスクは見ない（ここでの trusted の組は手で足したものだけ。自分の結び付きを根拠にしない）
            if (task, sid) in removed or (task, sid) in trusted:
                continue
            pattern = numbers[task]
            if s.branch and s.branch not in defaults and (s.place, s.branch) in confirmed.get(task, ()):
                hits[task] = "same_branch"
            elif pattern is not None and any(pattern.search(text) for text in (s.branch or "",) + s.titles):
                hits[task] = "ticket"
        task = _choose_weak(hits, s.started, tasks)
        if task is not None:
            out[sid] = (task, hits[task], "weak")
    return out


def _number_pattern(numbers: List[str]) -> Optional[Pattern[str]]:
    """チケット番号のどれかが、前後が数字でない並びとしてある所（fix/123-x の 123 は当たり、1234 は当たらない）。"""
    if not numbers:
        return None
    return re.compile(r"(?<!\d)(?:%s)(?!\d)" % "|".join(re.escape(n) for n in numbers))


def _choose_weak(hits: Dict[str, str], started: Optional[str], tasks: Dict[str, _Task]) -> Optional[str]:
    """推定（弱）の規則で当たったタスク。表の上の規則（同じブランチ）の当たりがあれば、それだけを残す。それでも
    複数なら、作った日（created）がセッションの開始の日付以前のタスクのうち最も新しいもの（なければ結び付けない）。"""
    if hits:
        best = min(_RANK[rule] for rule in hits.values())
        hits = {task: rule for task, rule in hits.items() if _RANK[rule] == best}
    if len(hits) <= 1:
        return next(iter(hits), None)
    day = (started or "")[:10]
    before = [t for t in hits if tasks[t].created and tasks[t].created[:10] <= day]
    return max(before, key=lambda t: (tasks[t].created[:10], t)) if before else None
