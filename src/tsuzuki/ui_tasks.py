"""タスクの HTML の断片。taskviews の形を HTML の文字列にする（台帳は読まない）。

ui_render と同じく、台帳と task.md から来た文字はすべてここでエスケープし（属性の値は引用符も）、インラインの
スクリプトと style 属性は出さない。画面のコピーの @ の形もここで作る（tasks.at_reference。画面の JavaScript は
組み立てない）。画面の JavaScript とは data-* 属性で約束する：一覧の行の data-task、カードの data-task・
data-mtime・data-size、操作のボタンの data-action（前からある resume・copy・open-doc・docs・card に、
task-stage・task-waiting・task-conversation・task-unlink・copy-picked・new-task を足す）、選ぶ欄の data-copy。
"""
import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from tsuzuki.render import fmt_local, render_markdown, render_plain
from tsuzuki.retention import iso_utc
from tsuzuki.tasks import SHARED_DIR, STAGES, at_reference, no_waiting, stage_label, ticket_numbers
from tsuzuki.taskviews import (DOC_BUNDLE_LIMIT, STATUS_LABELS, TASK_FILES_LIMIT, TaskDetail, TaskFileItem, TaskRow,
                              TaskSessionItem)
from tsuzuki.ui_render import NO_RESUME, _attrs, _badges, _button, _e, _filter_row, _join, _n, _period, _tilde

NO_PLACE_ID = 0  # 左の「場所なし」を選ぶときの場所の ID（台帳の場所の ID は 1 から）
# 左の状況：(サーバーの status, data-status の値, 言葉)。data-status の "" は既定（進行中と待ち）
STATUS_FILTERS = (("open", "", "進行中と待ち"), ("done", "done", "完了"), ("all", "all", "すべて"))
STATUS_HEADS = {"open": "進行中と待ち", "done": "完了", "all": "すべて"}
GROUPS = ("active", "waiting", "done")  # 一覧の見出しの順
HOW_LABELS = {"vscode": "VS Code", "terminal": "Terminal", "copy": "コピー"}
FROM_PR = "PR の記録から"
NO_CURRENT = "task.md に現在地がありません"  # taskviews の注意と同じ文。現在地の枠の中に添える
NO_SESSION_TO_RESUME = "続きから開ける会話がありません"
CANNOT_RESUME = "この会話は再開できません"
NOT_INDEXED = "task.md はまだ台帳に入っていません"
_PR_NUMBER = re.compile(r"/(\d+)/?$")
_LINK_SCHEMES = ("http://", "https://")


# 左：状況・場所・共通の資料

def frag_task_filters(counts: Dict[str, int], places: List[Tuple[Optional[int], str, int]], *, status: str,
                      place: Optional[int], shared_place: Optional[int]) -> str:
    """状況（進行中と待ち・完了・すべて）、場所（「すべて」が最初。場所のないタスクは「場所なし」で、ID は
    NO_PLACE_ID）、共通の資料（押すと資料の画面を共通の資料の場所で絞って開く）。places の場所の ID が None なら
    場所なし。place は選んでいる場所（None はすべて）。"""
    out = ['<div class="ph">状況</div>']
    out += [_filter_row("data-status", value, label, _n(counts.get(key, 0)), key == status)
            for key, value, label in STATUS_FILTERS]
    out += ['<div class="ph">場所</div>',
            _filter_row("data-place", "", "すべて", _n(sum(n for _, _, n in places)), place is None)]
    for pid, label, n in places:
        value = NO_PLACE_ID if pid is None else pid
        out.append(_filter_row("data-place", value, label, _n(n), value == place))
    out.append('<div class="ph">共通の資料</div>')
    if shared_place is None:
        out.append('<div class="empty">%s/ はまだありません</div>' % SHARED_DIR)
    else:
        out.append('<div%s><span title="資料の画面で開く">%s</span></div>' % (
            _attrs(("class", "place"), ("data-action", "docs"), ("data-place", shared_place)), SHARED_DIR))
    return "".join(out)


# 中央：一覧

def frag_task_list(rows: List[TaskRow], *, selected: Optional[str], status: str) -> str:
    """上に件数と「＋ 新しいタスク」、状況ごとの見出し（進行中・待ち・完了）の下に、rows の順（最後の動きの
    新しい順）で並べる。タスクがなくても「＋ 新しいタスク」は出す。"""
    head = "%s %s件 ・ 最近動いた順" % (STATUS_HEADS.get(status, STATUS_HEADS["open"]), _n(len(rows)))
    out = ['<div class="listhead"><span>%s</span>%s</div>' % (
        _e(head), _button("＋ 新しいタスク", "btn", ("data-action", "new-task")))]
    if not rows:
        out.append('<div class="empty">タスクがありません</div>')
    for group in GROUPS:
        members = [r for r in rows if r.status == group]
        if members:
            out.append('<div class="grp">%s %s</div>' % (STATUS_LABELS[group], _n(len(members))))
            out += [_task_row(r, r.name == selected) for r in members]
    return "".join(out)


def _task_row(r: TaskRow, on: bool) -> str:
    """題・チケット・段階、場所・最後の動き・会話の数・PR・実行中、2行目（次の一手、待ちなら待っていること、
    どちらもなければ最新の要約）。"""
    if r.ticket:
        ticket = '<span class="chip tk">%s</span>' % _e(_ticket_short(r.ticket))
    else:
        ticket = '<span class="chip tk none">チケットなし</span>'
    meta = _join([_e(r.place_label), _e(fmt_local(r.last_at)), "会話 %s" % _n(r.n_sessions) if r.n_sessions else "",
                  _e("PR " + " ".join("#%s" % n for n in r.pr_numbers)) if r.pr_numbers else ""])
    if r.live:
        meta += '<span class="badge live">実行中</span>'
    return '<div%s><div class="t">%s%s%s</div><div class="m">%s</div>%s</div>' % (
        _attrs(("class", "row on" if on else "row"), ("data-task", r.name)), _e(r.title), ticket, _stage_chip(r),
        meta, _line2(r))


def _ticket_short(ticket: str) -> str:
    """一覧のチケット：番号が取れれば「#番号」を並べ、取れなければ書かれたまま。"""
    numbers = ticket_numbers(ticket)
    return " ".join("#" + n for n in numbers) if numbers else ticket


def _stage_chip(r: TaskRow) -> str:
    pairs = [("class", "chip stage")]
    if r.stage_from_pr:  # task.md の段階より先の「PR」を、PR の記録から出している
        pairs.append(("title", FROM_PR))
    return "<span%s>%s</span>" % (_attrs(*pairs), _e(r.stage_label))


def _line2(r: TaskRow) -> str:
    wait = '<span class="chip wait">待ち</span>' if r.status == "waiting" else ""
    if not r.line2:
        return '<div class="r">%s</div>' % wait if wait else ""
    # 待ちの行の「待っていること」は印で分かるので、言葉を付けない
    label = "" if not r.line2_label or r.line2_label == "待っていること" else r.line2_label + "："
    return '<div class="r">%s%s</div>' % (wait, _e(label + r.line2))


# 右：タスクのカード

def frag_task_card(d: TaskDetail, *, resume_default: str, home: Optional[str] = None,
                   resume_command: Optional[str] = None) -> str:
    """タスクのカード：題と4つのボタン、段階と待ち、現在地と次の一手、待っていること・
    決めたこと・チケット・ブランチと PR、資料（3つの束）、会話、注意。data-mtime は task.md の更新時刻を
    そのままの精度で（repr）持ち、段階と待ちの操作がそれを送り返す。resume_command は、既定の再開がコピーの
    ときに「続きから」がコピーする再開のコマンド（サーバーが台帳から作る）。home の下のパスは ~ で出す。"""
    row = d.row
    boxed = [w for w in d.warnings if w == NO_CURRENT]
    notes = [w for w in d.warnings if w != NO_CURRENT]
    meta = _join([_e(row.ticket or "チケットなし"), _e(_tilde(d.path, home) if home else d.path),
                  _e(_period(_date_iso(d.created), row.last_at, days=True)),
                  "会話 %s" % _n(row.n_sessions) if row.n_sessions else ""])
    out = ["<div%s>" % _attrs(("class", "task-card"), ("data-task", row.name), ("data-mtime", repr(d.md_mtime)),
                              ("data-size", d.md_size)),
           '<div class="dh"><div class="head"><h4>%s</h4><div class="meta">%s</div></div><div class="btns">%s</div>'
           '</div>' % (_e(row.title), meta, _card_buttons(d, resume_default, resume_command)),
           _steps(row), _status_box(d, boxed), _fields(d), _documents(d, home), _sessions(d.sessions)]
    out += ['<div class="notice">%s</div>' % _e(note) for note in notes]
    out.append("</div>")
    return "".join(out)


def _card_buttons(d: TaskDetail, default: str, command: Optional[str]) -> str:
    """続きから・新しい会話・@task.md をコピー・task.md を開く。続きからは、確定か推定（強）で結び付いた最新の
    対話セッションを既定の再開の操作で開く（コピーなら再開のコマンドをコピーする）。続きから開けなければ
    @task.md のコピーを目立たせる（作ったばかりのタスク）。"""
    how = HOW_LABELS.get(default, default)
    sid = d.resume_session
    found = next((item.row for item in d.sessions if item.row.id == sid), None) if sid else None
    if not sid:
        reason = NO_SESSION_TO_RESUME
    elif found is not None and not found.transcript_exists:
        reason = NO_RESUME
    elif default == "copy" and not command:  # 作業ディレクトリがないなど、再開のコマンドを作れない
        reason = CANNOT_RESUME
    else:
        reason = ""
    if default == "copy":
        action = [("data-action", "copy"), ("data-copy", command or "")]
    else:
        action = [("data-action", "resume"), ("data-session", sid or ""), ("data-how", default)]
    resumable = not reason
    buttons = [
        _button("続きから（%s）" % how, "btn pri" if resumable else "btn", *([("title", reason)] if reason else []),
                *action, disabled=not resumable),
        _button("新しい会話（%s）" % how, "btn", ("data-action", "task-conversation"), ("data-how", default)),
        _button("@task.md をコピー", "btn" if resumable else "btn pri", ("data-action", "copy"),
                ("data-copy", at_reference(d.task_md))),
        _button("task.md を開く", "btn", *([("title", NOT_INDEXED)] if d.task_md_doc_id is None else []),
                ("data-action", "open-doc"), ("data-doc", "" if d.task_md_doc_id is None else d.task_md_doc_id),
                ("data-how", "vscode"), disabled=d.task_md_doc_id is None),
    ]
    return "".join(buttons)


def _steps(row: TaskRow) -> str:
    """7つの段階（今の段階に cur、前の段階に done）と、待ちの付け外し（完了したタスクには出さない）。PR の記録から
    段階を出しているときは、そう添える（押せば task.md に書く）。"""
    current = STAGES.index(row.stage) if row.stage in STAGES else -1
    steps = []
    for i, stage in enumerate(STAGES):
        cls = "stp cur" if i == current else "stp done" if i < current else "stp"
        steps.append(_button(stage_label(stage), cls, ("data-action", "task-stage"), ("data-stage", stage)))
    toggle = ""
    if row.status != "done":
        waiting = row.status == "waiting"
        toggle = _button("待ちを解く" if waiting else "待ちにする", "btn", ("data-action", "task-waiting"),
                         ("data-waiting", "false" if waiting else "true"))
    out = '<div class="steps">%s%s</div>' % ('<span class="arrow">›</span>'.join(steps), toggle)
    if row.stage_from_pr:
        out += '<div class="note">%s「PR」と出しています。段階を押すと task.md に書きます</div>' % FROM_PR
    return out


def _status_box(d: TaskDetail, notes: Sequence[str]) -> str:
    """現在地と次の一手（task.md の欄と、書いた日時）。task.md の後にセッションが動いていれば、その要約を下に
    足す。現在地がなければ、最新の要約を出してそのことを添える。"""
    current = (d.sections.get("現在地") or "").strip()
    following = (d.sections.get("次の一手") or "").strip()
    out = []
    if current:
        when = fmt_local(d.updated_at)
        label = "現在地（task.md ・ %s に更新）" % when if when else "現在地（task.md）"
        out.append('<div class="lab">%s</div>%s' % (_e(label), _markdown(current)))
    if current or following:
        out.append('<div class="lab">次の一手</div>%s' % (_markdown(following) if following
                                                          else '<div class="none">なし</div>'))
    if d.after:
        text, at = d.after
        when = fmt_local(at)
        word = "その後の会話の要約" if current else "最新の会話の要約"
        out.append('<div class="after"><div class="lab">%s</div><div class="body">%s</div></div>' % (
            _e("%s（%s）" % (word, when) if when else word), render_plain(text)))
    out += ['<div class="note">%s</div>' % _e(note) for note in notes]
    return '<div class="box">%s</div>' % "".join(out)


def _fields(d: TaskDetail) -> str:
    """待っていること・決めたこと・チケット（http・https のときだけ開くリンク）・ブランチと PR。"""
    waiting = d.sections.get("待っていること")
    decided = (d.sections.get("決めたこと") or "").strip()
    ticket = d.row.ticket or ""
    link = _link("チケットを開く", d.ticket_url) if ticket else ""
    prs = " ".join(_link(_pr_label(url), url) or _e(url) for url in d.prs)
    branch_pr = _join([_e(d.branch or "ブランチなし"), prs or "PR なし"])
    return "".join((
        _field("待っていること", "" if no_waiting(waiting) else _markdown(waiting.strip())),
        _field("決めたこと", _markdown(decided) if decided else ""),
        _field("チケット", _e(ticket) + (" " + link if link else "") if ticket else "", empty="チケットなし"),
        _field("ブランチ・PR", branch_pr if d.branch or d.prs else "", empty="ブランチなし ・ PR なし")))


def _field(label: str, value_html: str, *, empty: str = "なし") -> str:
    if not value_html:
        return '<div class="kv"><div class="k">%s</div><div class="v none">%s</div></div>' % (label, _e(empty))
    return '<div class="kv"><div class="k">%s</div><div class="v">%s</div></div>' % (label, value_html)


def _documents(d: TaskDetail, home: Optional[str]) -> str:
    """資料の3つの束（このタスクのフォルダー・共通の資料・ほかの場所の資料）。行ごとの @ コピーと選ぶ欄、
    選んだ資料の @ をまとめてコピーするボタン。"""
    root = os.path.dirname(d.path)
    # 共通の資料とほかの場所の資料は50件までしか並べないので、見出しの数は全体の数で出す
    total = len(d.folder_files) + d.shared_total + d.other_total
    out = ['<div class="sec">資料 %s%s</div>' % (
        _n(total), _button("選んだ資料の @ をまとめてコピー", "more", ("data-action", "copy-picked")))]
    bundles = (("このタスクのフォルダー", d.folder_files, lambda item: item.rel_path, 0),
               ("共通の資料", d.shared_docs, lambda item: os.path.relpath(item.path, root), d.shared_total),
               ("ほかの場所の資料", d.other_docs, lambda item: _tilde(item.path, home) if home else item.path,
                d.other_total))
    for heading, items, shown, bundle_total in bundles:
        if not items:
            continue
        out.append('<div class="fold">%s</div>' % heading)
        out += [_file_row(item, shown(item)) for item in items]
        if items is d.folder_files and d.folder_cut:
            out.append('<div class="listnote">フォルダーのファイルが多いため、%s件まで並べています</div>'
                       % _n(TASK_FILES_LIMIT))
        if bundle_total > len(items):  # 共通の資料とほかの場所の資料は、新しい順に上限まで
            out.append('<div class="listnote">新しい順に%s件まで並べています（ほか %s件）</div>'
                       % (_n(DOC_BUNDLE_LIMIT), _n(bundle_total - len(items))))
    if not total:
        out.append('<div class="empty">資料はありません</div>')
    return "".join(out)


def _file_row(item: TaskFileItem, name: str) -> str:
    """選ぶ欄・種類・題（台帳の資料なら押すと開く）と名前と冒頭・関わり方・@ コピー。見つからない資料は、
    そう書いてコピーと選ぶ欄を止める。"""
    ref = at_reference(item.path)
    pick = "<input%s%s>" % (_attrs(("type", "checkbox"), ("class", "pick"), ("data-copy", ref),
                                   ("aria-label", "選ぶ：" + name)), " disabled" if item.missing else "")
    title = _e(item.title)
    if name != item.title:
        title += '<span class="fn">%s</span>' % _e(name)
    if item.excerpt:
        title += '<span class="ex">%s</span>' % _e(item.excerpt)
    if item.missing:
        title += '<span class="ex missing">見つかりません</span>'
    pairs = [("class", "p")] + ([("data-doc", item.doc_id)] if item.doc_id is not None else [])
    relation = '<span class="w">%s</span>' % _e(item.relation) if item.relation else ""
    return '<div class="doc">%s<span class="ty">%s</span><span%s>%s</span>%s%s</div>' % (
        pick, _e(item.kind_label), _attrs(*pairs), title, relation,
        _button("@ コピー", "cp", ("data-action", "copy"), ("data-copy", ref), disabled=item.missing))


def _sessions(items: List[TaskSessionItem]) -> str:
    """結び付いたセッション（新しい順）。押すとセッションのカードを開く。根拠と確かさ（推定（弱）を含む）と、
    畳んだヘッドレスの子の数、「外す」。"""
    out = ['<div class="sec">会話 %s</div>' % _n(len(items))]
    for item in items:
        r = item.row
        when = fmt_local(r.ended_at)
        kids = '<span class="tiny">＋ヘッドレス %s</span>' % _n(item.n_children) if item.n_children else ""
        out.append('<div%s>%s<span class="pt">%s</span>%s<span class="ev">%s</span>%s%s</div>' % (
            _attrs(("class", "link"), ("data-action", "card"), ("data-session", r.id)), _badges(r), _e(r.title),
            '<span class="tiny">%s</span>' % _e(when) if when else "",
            _e(_join([item.rule_label, item.confidence_label])), kids,
            _button("外す", "more", ("data-action", "task-unlink"), ("data-session", r.id))))
    if not items:
        out.append('<div class="empty">まだ会話はありません</div>')
    return "".join(out)


# 右：新しいタスク

def frag_new_task(repos: List[Tuple[str, str]]) -> str:
    """新しいタスクの入力欄：題（必須）・短い名前（必須）・チケット番号・リポジトリ（場所の一覧から。
    repos は（パス、表示名））。送るのは画面の JavaScript（/api/task-new）で、フォームそのものは送らない。"""
    options = ['<option value="">なし</option>']
    options += ["<option%s>%s</option>" % (_attrs(("value", path), ("title", path)), _e(display))
                for path, display in repos]
    return (
        '<form id="new-task" class="new-task" autocomplete="off">'
        '<div class="dh"><div class="head"><h4>新しいタスク</h4></div></div>'
        '<label class="kv"><span class="k">題</span><input name="title" maxlength="200" required></label>'
        '<label class="kv"><span class="k">短い名前</span><input name="short_name" maxlength="80" '
        'placeholder="login-timeout" spellcheck="false" required></label>'
        '<div class="note">フォルダー名は「今日の日付-短い名前」になります。英小文字・数字・ハイフンがおすすめです'
        '（空白はハイフンにします）</div>'
        '<label class="kv"><span class="k">チケット</span><input name="ticket" maxlength="200" '
        'placeholder="#123（任意）"></label>'
        '<label class="kv"><span class="k">リポジトリ</span><select name="repo">%s</select></label>'
        '<div class="btns"><button type="submit" class="btn pri">作る</button></div>'
        '</form>' % "".join(options))


# 部品

def _markdown(text: str) -> str:
    """task.md の欄（Markdown。render_markdown は入力をすべてエスケープしてから決まった書式だけをタグにする）。"""
    return '<div class="md">%s</div>' % render_markdown(text)


def _link(label: str, url: Optional[str]) -> str:
    """http・https の URL だけを、新しいタブで開くリンクにする（ほかは空。呼び出し側が文字で出す）。"""
    if not url or not url.lower().startswith(_LINK_SCHEMES):
        return ""
    return "<a%s>%s</a>" % (_attrs(("href", url), ("rel", "noopener noreferrer"), ("target", "_blank")), _e(label))


def _pr_label(url: str) -> str:
    m = _PR_NUMBER.search(url)
    return "PR #%s" % m.group(1) if m else url


def _date_iso(day: Optional[str]) -> Optional[str]:
    """task.md の日付（地方時の YYYY-MM-DD）を、その日の0時の UTC の ISO にする（_period が同じ日付で出す）。"""
    try:
        return iso_utc(time.mktime(time.strptime(day or "", "%Y-%m-%d")))
    except (ValueError, OverflowError, OSError):
        return None
