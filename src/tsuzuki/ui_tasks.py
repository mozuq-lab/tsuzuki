"""タスクの HTML の断片。taskviews の形を HTML の文字列にする（台帳は読まない）。

ui_render と同じく、台帳と task.md から来た文字はすべてここでエスケープし（属性の値は引用符も）、インラインの
スクリプトと style 属性は出さない。画面のコピーの @ の形もここで作る（tasks.at_reference。画面の JavaScript は
組み立てない）。画面の JavaScript とは data-* 属性で約束する：一覧の行の data-task、カードの data-task・
data-mtime・data-size、操作のボタンの data-action（前からある resume・copy・open-doc・docs・card に、
task-stage・task-waiting・task-conversation・task-unlink・copy-picked・new-task・task-open・task-link・
task-settings・task-trash・task-copy-doc・ticket-url-edit・tasks-root-edit・tasks-readme・task-prep・form-cancel を足す）、
選ぶ欄の data-copy、「Claude で使う準備」のボタンの data-item。
"""
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from tsuzuki.render import fmt_local, render_markdown, render_plain
from tsuzuki.retention import iso_utc
from tsuzuki.tasks import SHARED_DIR, STAGES, TaskFile, at_reference, no_waiting, stage_label, ticket_numbers
from tsuzuki.taskviews import (DOC_BUNDLE_LIMIT, STATUS_LABELS, WORK_FILES_LIMIT, TaskDetail, TaskFileItem, TaskRow,
                              TaskSessionItem)
from tsuzuki.taskwrite import short_name_of
from tsuzuki.ui_render import (Attr, _attrs, _badges, _button, _e, _filter_row, _join, _mtime, _n, _period, _size,
                                _tilde)

NO_PLACE_ID = 0  # 左の「場所なし」を選ぶときの場所の ID（台帳の場所の ID は 1 から）
# 左の状況：(サーバーの status, data-status の値, 言葉)。data-status の "" は既定（進行中と待ち）
STATUS_FILTERS = (("open", "", "進行中と待ち"), ("done", "done", "完了"), ("all", "all", "すべて"))
STATUS_HEADS = {"open": "進行中と待ち", "done": "完了", "all": "すべて"}
GROUPS = ("active", "waiting", "done")  # 一覧の見出しの順
HOW_LABELS = {"vscode": "VS Code", "vscode-terminal": "VS Code のターミナル", "terminal": "Terminal", "copy": "コピー"}
FROM_PR = "PR の記録から"
NO_CURRENT = "task.md に現在地がありません"  # taskviews の注意と同じ文。現在地の枠の中に添える
NOT_IN_LEDGER = "台帳にない資料はコピーできません"
NOT_INDEXED = "task.md はまだ台帳に入っていません"
_PR_NUMBER = re.compile(r"/(\d+)/?$")
_LINK_SCHEMES = ("http://", "https://")


# ワークスペースの入力欄の注意書き（保存すると、Tsuzuki がワークスペースの folders の先頭に "." を足す）
WORKSPACE_NOTE = (
    '新しく決めたときと変えたときに保存すると、このファイルの folders の先頭に {"path": "."}（ワークスペースのある'
    'フォルダー）を足します。先頭にすでにあれば何もしません。新しい会話は、VS Code のターミナル・Terminal・コピーでは'
    'ワークスペースのあるフォルダーで、VS Code のパネルでは最初のフォルダーで始まるので、こうするとどれでも'
    'ワークスペースのあるフォルダーで始まります。ワークスペースのファイルがリポジトリの中にあると、その変更が Git に出ます。'
    'このワークスペースを VS Code で開いているときは、VS Code が窓を読み込み直し、パネルの会話が途切れることがあります。')


# 設定の短い名前の欄の注意書き（%s はタスクの日付。日付で始まらないフォルダー名では SHORT_NAME_NOTE_PLAIN）
SHORT_NAME_NOTE = "フォルダー名は「%s-短い名前」になります。変えると、フォルダーの名前が変わります"
SHORT_NAME_NOTE_PLAIN = "フォルダー名は「短い名前」になります。変えると、フォルダーの名前が変わります"


# カードの「Claude で使う準備」の項目：このタスクの会話を始めるフォルダーに置く物（この順に出す）
CARD_README, CARD_RULE, CARD_SKILLS, CARD_ALLOW = "readme", "rule", "skills", "allow"
CARD_PREP_ORDER = (CARD_README, CARD_RULE, CARD_SKILLS, CARD_ALLOW)


@dataclass(frozen=True)
class CardPrep:
    """カードの「Claude で使う準備」に出すもの。missing はまだ整っていない項目（CARD_*）、stale はあるが古い項目
    （CARD_README・CARD_RULE）、あとは表示用のパス（ホームの下は ~）：start は会話を始めるフォルダー、readme は
    置き場所の README.md、rule_file・skills_dir・allow_file は置く先。rule_line は古い決まりの文に足す1行
    （最後の改行なし。「1行をコピー」でコピーする）。"""
    missing: Tuple[str, ...]
    start: str = ""
    readme: str = ""
    rule_file: str = ""
    skills_dir: str = ""
    allow_file: str = ""
    note: str = ""
    stale: Tuple[str, ...] = ()
    rule_line: str = ""


def _card_prep(prep: Optional[CardPrep]) -> str:
    """カードの畳んだ一覧（初めは閉じている）。足りない項目がなければ何も出さない。"""
    if prep is None:
        return ""
    if prep.note:  # 置けない理由を1行だけ出す（項目は出さない）
        return '<div class="prep"><p class="prep-intro">%s</p></div>' % _e(prep.note)
    items = {
        CARD_README: ("決まりのファイル", "%s がありません" % prep.readme,
                      _button("置く", "btn", ("data-action", "tasks-readme"))),
        CARD_RULE: ("CLAUDE.local.md の決まり", "%s に、タスクの決まりを読む文を足します" % prep.rule_file,
                    _button("足す", "btn", ("data-action", "task-prep"), ("data-item", "rule"))),
        CARD_SKILLS: ("スキル", "task-start・task-handoff を %s に置きます" % prep.skills_dir,
                      _button("置く", "btn", ("data-action", "task-prep"), ("data-item", "skills"))),
        CARD_ALLOW: ("書き込みの許可",
                     "%s の additionalDirectories に、タスクの置き場所を足します" % prep.allow_file,
                     _button("足す", "btn", ("data-action", "task-prep"), ("data-item", "allow"))),
    }
    stale = {
        CARD_README: ("決まりのファイル", "%s に「作業用のファイル」の節がありません。末尾に足します" % prep.readme,
                      _button("足す", "btn", ("data-action", "tasks-readme"), ("data-mode", "section"))),
        CARD_RULE: ("CLAUDE.local.md の決まり", "%s に、作業用のファイルの1行を足します" % prep.rule_file,
                    _button("足す", "btn", ("data-action", "task-prep"), ("data-item", "rule"))
                    + _button("1行をコピー", "btn", ("data-action", "copy"), ("data-copy", prep.rule_line))),
    }
    shown = [key for key in CARD_PREP_ORDER if key in prep.missing or key in prep.stale]
    if not shown:
        return ""
    out = ['<details class="prep"><summary>Claude で使う準備：あと %sつ</summary>' % len(shown),
           '<p class="prep-intro">%s</p>' % _e(
               "このタスクの会話を始めるフォルダー（%s）に置きます。そこで始めた会話にだけ効き、"
               "ほかのプロジェクトの会話には効きません。" % prep.start)]
    for key in shown:
        if key in prep.missing:
            name, what, button = items[key]
            mark = "✗ " + name
        else:
            name, what, button = stale[key]
            mark = "△ %s（古い）" % name
        out.append('<div class="prep-item"><div class="prep-name">%s</div><div class="prep-what">%s</div>'
                   '<div class="prep-btns">%s</div></div>' % (_e(mark), _e(what), button))
    out.append("</details>")
    return "".join(out)


# 左：状況・場所・共通の資料

def frag_task_filters(counts: Dict[str, int], places: List[Tuple[Optional[int], str, int]], *, status: str,
                      place: Optional[int], shared_place: Optional[int], ticket_url: Optional[str] = None,
                      tasks_root: Optional[str] = None, home: str = "", tasks_root_value: str = "") -> str:
    """状況（進行中と待ち・完了・すべて）、場所（「すべて」が最初。場所のないタスクは「場所なし」で、ID は
    NO_PLACE_ID）、共通の資料（押すと資料の画面を共通の資料の場所で絞って開く）。places の場所の ID が None なら
    場所なし。place は選んでいる場所（None はすべて）。最後に、チケットのリンク先（設定 ticket_url。None は
    リポジトリの Issue）と、それを変える入力欄（初めは hidden）。その下に、タスクの置き場所（表示用の
    絶対パスの tasks_root（ホームの下は ~ で出す。None なら出さない）と、入力欄に入れる設定の値 tasks_root_value。"""
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
    out.append('<div class="ph">チケットのリンク先</div>')
    out.append('<div class="turl"><span>%s</span>%s</div>' % (
        _e(ticket_url) if ticket_url else "リポジトリの Issue（GitHub）",
        _button("変える", "btn", ("data-action", "ticket-url-edit"))))
    out.append(
        '<form id="ticket-url" class="ticket-url" autocomplete="off" hidden>'
        '<input name="url"%s placeholder="https://redmine.example.com/issues" spellcheck="false" maxlength="500">'
        '<div class="btns"><button type="submit" class="btn pri">保存</button>%s</div></form>' % (
            _attrs(("value", ticket_url or "")), _button("やめる", "btn", ("data-action", "form-cancel"))))
    if tasks_root is not None:
        out.append('<div class="ph">タスクの置き場所</div>')
        out.append('<div class="turl"><span>%s</span>%s</div>' % (
            _e(_tilde(tasks_root, home)), _button("変える", "btn", ("data-action", "tasks-root-edit"))))
        out.append(
            '<form id="tasks-root" class="ticket-url" autocomplete="off" hidden>'
            '<input name="path"%s placeholder="~/tasks" spellcheck="false" maxlength="1000">'
            '<div class="btns"><button type="submit" class="btn pri">保存</button>%s</div></form>' % (
                _attrs(("value", tasks_root_value)), _button("やめる", "btn", ("data-action", "form-cancel"))))
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
                   vscode_terminal: bool = False, prep: Optional[CardPrep] = None) -> str:
    """タスクのカード：題と3つのボタン、段階と待ち、現在地と次の一手、待っていること・
    決めたこと・チケット・ブランチと PR、会話、資料（3つの束）、一番下の「設定を変える」と「ゴミ箱に入れる」、
    注意の順。data-mtime は task.md の更新時刻を
    そのままの精度で（repr）持ち、段階と待ちの操作がそれを送り返す。home の下のパスは ~ で出す。
    vscode_terminal は拡張 Tsuzuki Terminal を入れてあるか（入れてあれば、新しい会話のボタンが1つ増える）。
    prep は「Claude で使う準備」（まだ整っていない項目があるときだけ、フィールドの下に畳んで出す）。"""
    row = d.row
    boxed = [w for w in d.warnings if w == NO_CURRENT]
    notes = [w for w in d.warnings if w != NO_CURRENT]
    meta = _join([_e(row.ticket or "チケットなし"), _e(_tilde(d.path, home) if home else d.path) + _button(
                      "パスをコピー", "more", ("data-action", "copy"), ("data-copy", d.path))
                  + _button("Finder で開く", "more", ("data-action", "open-task")),
                  _e(_period(_date_iso(d.created), row.last_at, days=True)),
                  "会話 %s" % _n(row.n_sessions) if row.n_sessions else ""])
    out = ["<div%s>" % _attrs(("class", "task-card"), ("data-task", row.name), ("data-mtime", repr(d.md_mtime)),
                              ("data-size", d.md_size)),
           '<div class="dh"><div class="head"><h4>%s</h4><div class="meta">%s</div></div><div class="btns">%s</div>'
           '</div>' % (_e(row.title), meta, _card_buttons(d, resume_default, vscode_terminal)),
           _steps(row, d.sections.get("待っていること")), _status_box(d, boxed), _fields(d), _card_prep(prep), _sessions(d.sessions), _documents(d, home),
           _work_files(d)]
    out.append(_foot(d, home))
    out += ['<div class="notice">%s</div>' % _e(note) for note in notes]
    out.append("</div>")
    return "".join(out)


def _foot(d: TaskDetail, home: Optional[str]) -> str:
    """カードの一番下：「設定を変える」（題などの入力欄を開く）と「ゴミ箱に入れる」（押すと確かめてから、
    フォルダーごとゴミ箱へ動かす）。"""
    place = _tilde(d.path, home) if home else d.path
    confirm = "「%s」のフォルダー（%s）を、中のファイルごとゴミ箱に入れます。" % (d.row.title, place)
    if d.row.live:
        confirm += "このタスクの会話が実行中です。"
    return '<div class="foot">%s%s</div>' % (
        _button("設定を変える", "btn", ("data-action", "task-settings")),
        _button("ゴミ箱に入れる", "btn", ("data-action", "task-trash"), ("data-confirm", confirm)))


def _card_buttons(d: TaskDetail, default: str, vscode_terminal: bool = False) -> str:
    """新しい会話（拡張を入れてあり、既定がそれでなければ、VS Code のターミナルの分も）・@task.md をコピー・
    task.md を開く。会話は「会話」の行から開いて再開する。"""
    how = HOW_LABELS.get(default, default)
    buttons = [
        _button("新しい会話（%s）" % how, "btn", ("data-action", "task-conversation"), ("data-how", default)),
        *([_button("新しい会話（%s）" % HOW_LABELS["vscode-terminal"], "btn", ("data-action", "task-conversation"),
                   ("data-how", "vscode-terminal"))] if vscode_terminal and default != "vscode-terminal" else []),
        _button("@task.md をコピー", "btn pri", ("data-action", "copy"), ("data-copy", at_reference(d.task_md))),
        _button("task.md を開く", "btn", *([("title", NOT_INDEXED)] if d.task_md_doc_id is None else []),
                ("data-action", "open-doc"), ("data-doc", "" if d.task_md_doc_id is None else d.task_md_doc_id),
                ("data-how", "vscode"), disabled=d.task_md_doc_id is None),
    ]
    return "".join(buttons)


def _steps(row: TaskRow, waiting_text: Optional[str]) -> str:
    """7つの段階（今の段階に cur、前の段階に done）と、待ちの付け外し（完了したタスクには出さない）。待ちの
    ボタンは、待っていることの入力欄（初めは hidden）を開く。PR の記録から段階を出しているときは、そう添える
    （押せば task.md に書く）。"""
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
    if row.status != "done":
        out += _waiting_form(row.status == "waiting", "" if no_waiting(waiting_text) else (waiting_text or "").strip())
    return out


def _waiting_form(waiting: bool, reason: str) -> str:
    """待ちにする・待ちを解くときに、task.md の「待っていること」へ書く文を入れる欄（待ちを解くときは、今の文を直す）。"""
    if waiting:
        lab, submit = "待っていることを直してから解きます（task.md の『待っていること』に書きます）", "待ちを解く"
    else:
        lab, submit = "何を待っていますか（task.md の『待っていること』に書きます）", "待ちにする"
    return ('<form%s autocomplete="off"><div class="lab">%s</div>'
            '<textarea name="reason" rows="4" maxlength="4000">%s</textarea>'
            '<div class="btns"><button type="submit" class="btn pri">%s</button>'
            '<button type="button" class="btn" data-action="form-cancel">やめる</button></div></form>' % (
                _attrs(("class", "waiting-form"), ("data-waiting", "false" if waiting else "true")) + " hidden",
                _e(lab), _e(reason), submit))


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
    tickets = " ・ ".join((_link(label, url) if url else "") or _e(label) for label, url in d.ticket_links)
    prs = " ".join(_link(_pr_label(url), url) or _e(url) for url in d.prs)
    branch_pr = _join([_e(d.branch or "ブランチなし"), prs or "PR なし"])
    return "".join((
        _field("待っていること", "" if no_waiting(waiting) else _markdown(waiting.strip())),
        _field("決めたこと", _markdown(decided) if decided else ""),
        _field("チケット", tickets, empty="チケットなし"),
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
        _n(total), _button("選んだファイルの @ をまとめてコピー", "more", ("data-action", "copy-picked")))]
    bundles = (("このタスクのフォルダー", d.folder_files, lambda item: item.rel_path, 0),
               ("共通の資料", d.shared_docs, lambda item: os.path.relpath(item.path, root), d.shared_total),
               ("ほかの場所の資料", d.other_docs, lambda item: _tilde(item.path, home) if home else item.path,
                d.other_total))
    for heading, items, shown, bundle_total in bundles:
        if not items:
            continue
        out.append('<div class="fold">%s</div>' % heading)
        out += [_file_row(item, shown(item), copy_to_task=items is d.other_docs) for item in items]
        if bundle_total > len(items):  # 共通の資料とほかの場所の資料は、新しい順に上限まで
            out.append('<div class="listnote">新しい順に%s件まで並べています（ほか %s件）</div>'
                       % (_n(DOC_BUNDLE_LIMIT), _n(bundle_total - len(items))))
    if not total:
        out.append('<div class="empty">資料はありません</div>')
    return "".join(out)


def _file_row(item: TaskFileItem, name: str, *, copy_to_task: bool = False) -> str:
    """選ぶ欄・種類・題（台帳の資料なら押すと開く）と名前と冒頭・関わり方・@ コピー。見つからない資料は、
    そう書いてコピーと選ぶ欄を止める。copy_to_task（ほかの場所の資料）なら、@ コピーの前に「タスクへコピー」
    を置く（台帳にない資料と見つからない資料では押せない）。"""
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
    if copy_to_task and item.copied:
        title += '<span class="ex copied">コピー済み</span>'
    pairs = [("class", "p")] + ([("data-doc", item.doc_id)] if item.doc_id is not None else [])
    relation = _file_meta(item)
    copy_button = ""
    if copy_to_task:
        attrs = [("data-action", "task-copy-doc")]
        attrs += [("data-doc", item.doc_id)] if item.doc_id is not None else [("title", NOT_IN_LEDGER)]
        copy_button = _button("タスクへコピー", "cp", *attrs, disabled=item.missing or item.doc_id is None)
    return '<div class="doc">%s<span class="ty">%s</span><span%s>%s</span>%s%s%s</div>' % (
        pick, _e(item.kind_label), _attrs(*pairs), title, relation, copy_button,
        _button("@ コピー", "cp", ("data-action", "copy"), ("data-copy", ref), disabled=item.missing))


def _file_meta(item: TaskFileItem) -> str:
    """行の右の「関わり ・ 更新日時 ・ 大きさ」。どれもなければ空。"""
    meta = _join([_e(item.relation or ""), _e(_mtime(item.mtime)), _e(_size(item.size))])
    return '<span class="w">%s</span>' % meta if meta else ""


def _work_files(d: TaskDetail) -> str:
    """作業用のファイルの束：フォルダー（./ が最初）ごとに、新しい順（並びは taskviews が決める）。ないとき、
    フォルダーを読む上限で切ってもいないときは出さない。"""
    if not d.work_files and not d.folder_cut:
        return ""
    out = ['<div class="sec">作業用のファイル %s</div>' % _n(d.work_total)]
    folder = None  # type: Optional[str]
    for item in d.work_files:
        here = os.path.dirname(item.rel_path)
        if here != folder:
            folder = here
            out.append('<div class="fold">%s</div>' % _e(here + "/" if here else "./"))
        out.append(_work_row(item))
    if d.work_total > len(d.work_files):
        out.append('<div class="listnote">新しい順に%s件まで並べています（ほか %s件）</div>'
                   % (_n(WORK_FILES_LIMIT), _n(d.work_total - len(d.work_files))))
    if d.folder_cut:
        out.append('<div class="listnote">フォルダーのファイルが多いため、すべては並べていません</div>')
    return "".join(out)


def _work_row(item: TaskFileItem) -> str:
    """作業用のファイルの行：選ぶ欄・拡張子・名前（台帳の資料なら押すと開く）・関わりと更新日時と大きさ・@ コピー。"""
    ref = at_reference(item.path)
    name = os.path.basename(item.path)
    pick = "<input%s>" % _attrs(("type", "checkbox"), ("class", "pick"), ("data-copy", ref),
                                ("aria-label", "選ぶ：" + name))
    pairs = [("class", "p")] + ([("data-doc", item.doc_id)] if item.doc_id is not None else [])  # type: List[Attr]
    return '<div class="doc">%s<span class="ty">%s</span><span%s>%s</span>%s%s</div>' % (
        pick, _e(item.ext.lstrip(".").upper()), _attrs(*pairs), _e(name), _file_meta(item),
        _button("@ コピー", "cp", ("data-action", "copy"), ("data-copy", ref)))


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
    repos は（パス、表示名））・ワークスペース（任意。設定の欄と同じ形）。送るのは画面の JavaScript（/api/task-new）で、フォームそのものは送らない。"""
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
        '<label class="kv"><span class="k">チケット</span><input name="ticket" maxlength="500" '
        'placeholder="#123, #456（任意）"></label>'
        '<div class="note">複数あるときは , で区切ります</div>'
        '<label class="kv"><span class="k">リポジトリ</span><select name="repo">%s</select></label>'
        '<label class="kv"><span class="k">ワークスペース</span><input%s></label>'
        '<div class="note">%s</div>'
        '<div class="btns"><button type="submit" class="btn pri">作る</button></div>'
        '</form>' % ("".join(options), _attrs(("name", "workspace"), ("maxlength", 1000), ("spellcheck", "false"),
                                              ("placeholder", "~/dev/app.code-workspace（任意）")),
                     _e(WORKSPACE_NOTE)))


# 右：タスクの設定

def frag_task_settings(name: str, tf: TaskFile, title: str, *, repos: List[Tuple[str, str]], mtime: float,
                       size: int) -> str:
    """タスクの設定の入力欄：題・短い名前（フォルダー名が YYYY-MM-DD- で始まればその後ろ、そうでなければ名前の
    まま）・チケット・リポジトリ（場所の一覧から。今の値が一覧になければ、先頭の近くに選んだ状態で足す）・
    ワークスペース。tf は task.md の今の値、title は今の題。data-mtime・data-size は読んだときの task.md の
    更新時刻（repr）と大きさで、送り返して、その後に書き換わっていないか確かめる。"""
    short = short_name_of(name)
    date = name[:-len(short) - 1] if short != name else ""  # short_name_of と同じく、YYYY-MM-DD- で始まるときだけ
    repo = tf.repo or ""
    paths = [path for path, _ in repos]
    options = ['<option value=""%s>なし</option>' % ("" if repo else " selected")]
    if repo and repo not in paths:
        options.append("<option%s>%s</option>" % (_attrs(("value", repo), ("title", repo)) + " selected", _e(repo)))
    for path, display in repos:
        options.append("<option%s>%s</option>" % (
            _attrs(("value", path), ("title", path)) + (" selected" if path == repo else ""), _e(display)))
    return (
        '<form%s autocomplete="off">'
        '<div class="dh"><div class="head"><h4>設定を変える</h4></div></div>'
        '<label class="kv"><span class="k">題</span><input%s></label>'
        '<label class="kv"><span class="k">短い名前</span><input%s></label>'
        '<div class="note">%s</div>'
        '<label class="kv"><span class="k">チケット</span><input%s></label>'
        '<div class="note">複数あるときは , で区切ります</div>'
        '<label class="kv"><span class="k">リポジトリ</span><select name="repo">%s</select></label>'
        '<label class="kv"><span class="k">ワークスペース</span><input%s></label>'
        '<div class="note">%s</div>'
        '<div class="btns"><button type="submit" class="btn pri">保存</button>'
        '<button type="button" class="btn" data-action="form-cancel">やめる</button></div>'
        "</form>" % (
            _attrs(("id", "task-settings"), ("class", "new-task task-settings"), ("data-task", name),
                   ("data-mtime", repr(mtime)), ("data-size", size)),
            _attrs(("name", "title"), ("value", title), ("maxlength", 200), ("required", "")),
            _attrs(("name", "short_name"), ("value", short), ("maxlength", 80), ("spellcheck", "false"),
                   ("required", "")),
            _e(SHORT_NAME_NOTE % date if date else SHORT_NAME_NOTE_PLAIN),
            _attrs(("name", "ticket"), ("value", tf.ticket or ""), ("maxlength", 500),
                   ("placeholder", "#123, #456（任意）")),
            "".join(options),
            _attrs(("name", "workspace"), ("value", tf.workspace or ""), ("maxlength", 1000), ("spellcheck", "false"),
                   ("placeholder", "~/dev/app.code-workspace（任意）")),
            _e(WORKSPACE_NOTE)))


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
