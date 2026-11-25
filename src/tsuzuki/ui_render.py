"""画面の HTML の断片。views の形を HTML の文字列にする（台帳は views の関数を通してだけ読む）。

断片はページに innerHTML で入るので、台帳から来た文字はすべてここでエスケープする（属性の値は引用符も）。
インラインのスクリプトと style 属性は出さない（ページの CSP で止まるため）。見た目は class だけで決める。
画面の JavaScript とは data-* 属性で約束する（data-session・data-place・data-doc・data-kind・data-action など）。
"""
import functools
import html
import os
import re
import sqlite3
from typing import Dict, Iterable, List, Optional, Pattern, Sequence, Set, Tuple

from tsuzuki import views
from tsuzuki.documents import KIND_LABELS
from tsuzuki.render import fmt_local, highlight, render_plain
from tsuzuki.retention import iso_utc
from tsuzuki.search import DocHit, SearchResult, SessionHit, Snippet
from tsuzuki.sources.live import LiveSession
from tsuzuki.views import (
    MOVED_LABEL, ChildRow, ConvItem, ConversationWindow, DocItem, DocumentDetail, PlaceItem, PlaceOverview,
    SessionDetail, SessionRow,
)

SEP = " ・ "
EXCERPT_CHARS = 300  # 状況カードの一致箇所に、1つの発言から出す字数
EXCERPT_REPLIES = 3  # 一致箇所に出す、語を含む Claude の応答の数
EXCERPT_TOOLS = 5  # 一致箇所に出す、語を含むツールの要約の数
TOOLS_IN_SUMMARY = 3  # 会話の表示で、畳んだツールの行に並べる数
NO_RESUME = "記録は削除済みのため再開できません"
HISTORY_LABEL = "入力履歴（記録は削除済み・再開不可）"
LIST_CUT = "新しい順に%s件まで表示しています（全%s件）"  # 一覧を上限（views.LIST_LIMIT・DOCS_LIMIT）で切ったとき
# 検索の一致が種類ごとの上限（search.RESULT_LIMIT）まで返ったとき。数えないので、全部の数は出さない
HITS_CUT = "%s順に%s件まで"
ORDER_WORDS = {"recent": "新しい", "count": "一致の多い"}
DOC_HITS_CUT = "一致は新しい順に%s件まで表示しています"
DOC_HITS_CUT_KIND = "新しい順に%s件までの一致を、種類で絞って表示しています"
SHORTEST_DAYS = 7  # 画面の期間の最も短い選択肢（app.js の DAY_CHOICES）。これより短くは絞れない
RESUME_BUTTONS = (("vscode", "VS Code で再開"), ("terminal", "Terminal で再開"))
# 拡張（vscode_ext）が入っているか、既定の再開にしているときだけ、VS Code で再開の次に出す
VSCODE_TERMINAL_BUTTON = ("vscode-terminal", "VS Code のターミナルで再開")
OPEN_BUTTONS = (("vscode", "VS Code で開く"), ("finder", "Finder"))  # 資料と場所
REL_PREFIXES = {"created": "作った：", "edited": "直した：", "read": "読んだ：", "named": "名指しした："}
# 長い会話を読み足すボタン（読み足す分、残り）
MORE_TURNS = {"before": "前の %s ターンを読む（残り %s）", "after": "後の %s ターンを読む（残り %s）"}
MORE_ITEMS = "後の %s 件を読む（残り %s）"
WHO = {"user": "あなた", "assistant": "Claude"}
_HTML_EXTS = (".html", ".htm")
_PR_NUMBER = re.compile(r"/pull/(\d+)")
_WHITESPACE = re.compile(r"\s+")

Attr = Tuple[str, object]


# 左：場所と資料の絞り込み

def frag_places(places: List[PlaceItem], *, selected: Optional[int],
                counts: Optional[Dict[int, int]] = None) -> str:
    """場所の一覧（「すべて」が最初）。counts があれば検索の状態で、一致のある場所だけを一致の数とともに出す
    （選んでいる場所は、一致がなくても出す）。"""
    if counts is None:
        head, total = "場所", sum(p.n_interactive for p in places)
        shown = [(p, _n(p.n_interactive) + (" ＋子" + _n(p.n_headless) if p.n_headless else "")) for p in places]
    else:
        head, total = "場所（一致がある所）", sum(counts.values())
        shown = [(p, _n(counts.get(p.id, 0))) for p in places if counts.get(p.id) or p.id == selected]
    out = ['<div class="ph">%s</div>' % head, _filter_row("data-place", "", "すべて", _n(total), selected is None)]
    out += [_filter_row("data-place", p.id, p.label, count, p.id == selected, title=p.display) for p, count in shown]
    return "".join(out)


def frag_docs_filters(kinds_counts: Dict[str, int], places: List[PlaceItem], *, kind: Optional[str],
                      place_id: Optional[int]) -> str:
    """資料の種類（KIND_LABELS の順、知らない種類は最後）と、資料のある場所。"""
    kinds = list(KIND_LABELS) + sorted((k for k in kinds_counts if k not in KIND_LABELS), key=str)
    out = ['<div class="ph">種類</div>',
           _filter_row("data-kind", "", "すべて", _n(sum(kinds_counts.values())), not kind)]
    out += [_filter_row("data-kind", k, _kind(k), _n(kinds_counts.get(k, 0)), k == kind) for k in kinds]
    out += ['<div class="ph">場所</div>', _filter_row("data-place", "", "すべて", None, place_id is None)]
    out += [_filter_row("data-place", p.id, p.label, _n(p.n_docs), p.id == place_id, title=p.display)
            for p in places if p.n_docs or p.id == place_id]
    return "".join(out)


def _filter_row(key: str, value: object, label: str, count: Optional[str], on: bool, *, title: str = "") -> str:
    """左の列の1行。場所は短い名前（label）で出し、表示名を title に入れる（狭い窓では app.js が場所の選択肢に写す）。"""
    em = "<em>%s</em>" % _e(count) if count else ""
    name = "<span%s>%s</span>" % (_attrs(("title", title)) if title else "", _e(label or title))
    return "<div%s>%s%s</div>" % (_attrs(("class", "place on" if on else "place"), (key, value)), name, em)


# 中央：一覧

def frag_session_list(rows: List[SessionRow], *, selected: Optional[str], total: Optional[int] = None,
                      place_id: Optional[int] = None, days: Optional[float] = None, headless: bool = False) -> str:
    """セッションの一覧。total（条件に合う数）が行の数より多ければ、上限で切ったことと絞り方を最後に1行出す。
    place_id・days・headless は今の絞り込みで、まだ絞れる方法だけを挙げるのに使う。"""
    if not rows:
        return '<div class="empty">セッションがありません</div>'
    ways = []
    if days is None or days > SHORTEST_DAYS:
        ways.append("期間を短くする")
    if place_id is None:
        ways.append("場所を選ぶ")
    if headless:
        ways.append("「ヘッドレスも並べる」を外す")
    return "".join(_session_row(r, r.id == selected) for r in rows) + _cut_note(len(rows), total, ways)


def _cut_note(shown: int, total: Optional[int], ways: List[str]) -> str:
    """一覧を上限で切ったときの最後の1行（total が行の数より多いときだけ）。ways は、まだ使える絞り方。"""
    if total is None or total <= shown:
        return ""
    text = LIST_CUT % (_n(shown), _n(total))
    if ways:
        text += "。" + "か、".join(ways) + "と絞れます"
    return '<div class="listnote">%s</div>' % _e(text)


def _session_row(r: SessionRow, on: bool) -> str:
    kids = ""
    if r.n_children:
        kids = _button("子" + _n(r.n_children), "kids", ("data-action", "children"), ("data-session", r.id))
    meta = _join([_place(r.place_label, r.place_display), _e(fmt_local(r.ended_at)), "入力%s回" % _n(r.n_prompts),
                  kids, _e(_pr_numbers(r.prs))])
    summary = ""
    if r.summary:
        if r.summary_is_recap or r.kind != "interactive":
            summary = '<div class="r">%s</div>' % _e(r.summary)
        else:  # 対話で要約がなければ、要約の代わりは最後の依頼
            summary = '<div class="r none">要約なし ― 最後の依頼：%s</div>' % _e(r.summary)
    out = '<div%s><div class="t">%s%s</div><div class="m">%s</div>%s</div>' % (
        _attrs(("class", "row on" if on else "row"), ("data-session", r.id)), _badges(r), _e(r.title), meta, summary)
    if r.n_children:  # 子の一覧（/frag/children）を入れる所
        out += "<div%s></div>" % _attrs(("class", "children"), ("id", "children-" + r.id))
    return out


def frag_children(rows: List[ChildRow], *, session_id: str, offset: int, has_more: bool) -> str:
    return _child_rows(rows, ("data-session", session_id), "children", offset, has_more, "子セッションはありません")


def frag_orphans(rows: List[ChildRow], *, place_id: int, offset: int, has_more: bool) -> str:
    return _child_rows(rows, ("data-place", place_id), "orphans", offset, has_more,
                       "親が分からないヘッドレスはありません")


def _child_rows(rows: List[ChildRow], owner: Attr, action: str, offset: int, has_more: bool, empty: str) -> str:
    if not rows and not offset:
        return '<div class="empty">%s</div>' % empty
    out = ['<div%s><span class="tiny">%s</span> %s</div>' % (
        _attrs(("class", "row child"), ("data-session", c.id)), _e(fmt_local(c.started_at)), _e(c.first_line))
        for c in rows]
    if has_more:
        out.append(_button("さらに表示", "more", ("data-action", action), owner, ("data-offset", offset + len(rows))))
    return "".join(out)


def frag_search(conn: sqlite3.Connection, result: SearchResult, *, terms: List[str], selected: Optional[str],
                live: Optional[Dict[str, LiveSession]] = None, home: Optional[str] = None,
                limit: Optional[int] = None, order: str = "recent") -> str:
    """検索の一致の一覧。会話・資料・入力履歴・ヘッドレスの順。limit（検索の種類ごとの上限）まで返った種類は、
    見出しの数に「新しい順に100件まで」を添える（会話は order の順）。"""
    if not (result.sessions or result.documents or result.history or result.headless):
        return '<div class="empty">一致するものはありませんでした</div>'

    def count(hits: Sequence[object], unit: str, by: str = "recent") -> str:
        text = _n(len(hits)) + unit
        if limit is not None and len(hits) >= limit:
            text += "（" + HITS_CUT % (ORDER_WORDS.get(by, ORDER_WORDS["recent"]), _n(limit)) + "）"
        return text
    head = "会話 %s ・ 資料 %s ・ 入力履歴 %s" % (
        count(result.sessions, "セッション", order), count(result.documents, "件"), count(result.history, "件"))
    if result.headless:
        head += " ・ ヘッドレス %s" % count(result.headless, "件")
    # 検索の後に台帳から消えた物は出さない
    rows = views.session_rows(conn, [h.session_id for h in result.sessions + result.headless], live=live or {})
    docs = views.doc_items(conn, [h.doc_id for h in result.documents], home=home)
    place_ids = [h.place_id for h in result.history if h.place_id is not None]
    places, labels = views.place_displays(conn, place_ids), views.place_labels(conn, place_ids)
    home = home if home is not None else os.path.expanduser("~")
    out = ['<div class="listhead">%s</div>' % _e(head)]
    out += [_hit_row(h, rows[h.session_id], h.session_id == selected, terms)
            for h in result.sessions if h.session_id in rows]
    out += [_doc_row(docs[h.doc_id], False, '<div class="r">%s</div>' % _doc_snippet(h, docs[h.doc_id], terms, home))
            for h in result.documents if h.doc_id in docs]
    for h in result.history:  # 記録の消えたセッションの入力。開く先がないのでクリックの操作はない
        display = places.get(h.place_id)
        where = _place(labels.get(h.place_id, ""), display) if display else _e(_tilde(h.project, home))
        out.append('<div class="row hist"><div class="t gone">%s</div><div class="m">%s</div><div class="r">%s</div>'
                   '</div>' % (HISTORY_LABEL, _join([where, _e(fmt_local(h.ts))]), _snippet(h.snippet, terms)))
    out += [_hit_row(h, rows[h.session_id], h.session_id == selected, terms)
            for h in result.headless if h.session_id in rows]
    return "".join(out)


def _hit_row(hit: SessionHit, r: SessionRow, on: bool, terms: Sequence[str]) -> str:
    pairs = [("class", "row on" if on else "row"), ("data-session", hit.session_id)]  # type: List[Attr]
    if hit.turns:  # 一致したターン（新しい順）。data-turn は最も新しいもの
        pairs += [("data-turn", hit.turns[0]), ("data-turns", _csv(hit.turns))]
    more = "ほか%sか所" % _n(hit.hit_count - 1) if hit.hit_count > 1 else ""
    meta = _join([_place(r.place_label, r.place_display), _e(fmt_local(hit.last_ts)), more])
    return '<div%s><div class="t">%s%s</div><div class="m">%s</div><div class="r">%s</div></div>' % (
        _attrs(*pairs), _badges(r), _e(r.title), meta, _snippet(hit.snippet, terms))


def frag_docs_list(items: List[DocItem], *, selected: Optional[int], total: Optional[int] = None,
                   kind: Optional[str] = None, place_id: Optional[int] = None, capped_at: Optional[int] = None,
                   hits: Optional[Dict[int, DocHit]] = None, terms: Sequence[str] = (),
                   home: Optional[str] = None) -> str:
    """資料の一覧。total（条件に合う数）が行の数より多ければ、セッションの一覧と同じく最後に1行で知らせ、
    まだ使える絞り方（種類・場所・検索）を挙げる。kind・place_id は今の絞り込み。検索の一致が上限（capped_at）
    まで返ったときも、最後に1行で知らせる（種類は、上限までの一致を絞っている）。検索のとき（hits）は、
    一致のある行に、検索結果と同じ抜粋と語の印を付ける。"""
    hits_note = ""
    if capped_at is not None:
        hits_note = '<div class="listnote">%s</div>' % _e((DOC_HITS_CUT_KIND if kind else DOC_HITS_CUT) % _n(capped_at))
    if not items:
        return '<div class="empty">資料がありません</div>' + hits_note
    ways = (["種類を選ぶ"] if not kind else []) + (["場所を選ぶ"] if place_id is None else []) + ["検索する"]
    def row(item: DocItem) -> str:
        hit = (hits or {}).get(item.id)
        snippet = '<div class="r">%s</div>' % _doc_snippet(hit, item, terms, home or "") if hit else ""
        return _doc_row(item, item.id == selected, snippet)

    return "".join(row(item) for item in items) + _cut_note(len(items), total, ways) + hits_note


def _doc_snippet(hit: DocHit, item: DocItem, terms: Sequence[str], home: str) -> str:
    """資料の一致の抜粋。パスにだけ語がある資料は、パス（ホームを ~ にしたもの）に語の印を付けて出す。"""
    if hit.path_only:
        return _snippet(Snippet(_tilde(item.path, home), [], hit.snippet.role, hit.snippet.ts), terms)
    return _snippet(hit.snippet, terms)


def _doc_row(item: DocItem, on: bool, snippet: str = "") -> str:
    ext = (item.ext or "").lstrip(".").upper()
    badge = '<span class="ty">%s</span> ' % _e(ext) if ext else ""
    task = '<span class="task">%s</span>' % _e("タスク：" + item.task[1]) if item.task else ""
    meta = _join([_e(_kind(item.kind)), task, _doc_where(item), _e(_mtime(item.mtime))])
    # タスクの表紙（フォルダーの直下の task.md）の行は data-doc のまま選べて、app.js が右のペインに資料でなく
    # タスクのカードを出す
    pairs = [("class", "row on" if on else "row"), ("data-doc", item.id)]  # type: List[Attr]
    if item.task and item.task_cover:
        pairs.append(("data-task", item.task[0]))
    return '<div%s><div class="t">%s%s</div><div class="m">%s</div>%s</div>' % (
        _attrs(*pairs), badge, _e(item.name), meta, snippet)


# 右：状況カード

def frag_card(d: SessionDetail, *, resume_default: str, hit_turns: Sequence[int] = (), hit_index: int = 0,
              excerpt: Optional[List[ConvItem]] = None, terms: Sequence[str] = (),
              task_choices: Sequence[Tuple[str, str]] = (), vscode_terminal: bool = False) -> str:
    """状況カード。hit_turns（一致したターン、新しい順）があれば検索の状態で、hit_index 番目の一致の箇所
    （excerpt、views.turn_excerpt）を再開のボタンの後に出す。vscode_terminal は、VS Code のターミナルで再開する
    拡張が入っているか（入っていれば、そのボタンも出す）。"""
    turns = [int(n) for n in hit_turns]
    index = min(max(int(hit_index), 0), len(turns) - 1) if turns else 0
    turn = turns[index] if turns else None
    branch = d.branch if d.branch != "HEAD" else ""  # HEAD は枝の名前ではない（切り離された状態）
    meta = _join([_place(d.place_label, d.place_display), _e(branch), _e(_period(d.started_at, d.ended_at)),
                  "入力%s回" % _n(d.n_prompts), "サブエージェント%s" % _n(d.n_agents) if d.n_agents else "",
                  "子%s" % _n(d.children_total) if d.children_total else ""])
    out = ["<div%s>" % _attrs(("class", "card"), ("data-session", d.id), ("data-turns", _csv(turns)),
                              ("data-index", index)),
           '<div class="dh"><div class="head"><h4>%s%s</h4><div class="meta">%s</div></div>%s</div>' % (
               _badges(d), _e(d.title), meta, _resume_buttons(d, resume_default, vscode_terminal))]
    if not d.transcript_exists:
        out.append('<div class="notice">%s</div>' % NO_RESUME)
    if turns:
        out.append(_hit_section(d.id, turns, index, excerpt, terms))
    out += [_recap(d), _kv("最初の依頼", d.first_prompt), _kv("最後の依頼", d.last_prompt),
            '<div class="go">%s</div>' % _open_conversation(d.id, turn), _card_tasks(d, task_choices),
            _card_docs(d), _card_relations(d), "</div>"]
    return "".join(out)


def _resume_buttons(d: SessionDetail, default: str, vscode_terminal: bool = False) -> str:
    command = d.resume_command if d.transcript_exists else None
    off = command is None  # 記録が消えた・ID の形でない・作業ディレクトリがない
    choices = list(RESUME_BUTTONS)
    if vscode_terminal or default == VSCODE_TERMINAL_BUTTON[0]:
        choices.insert(1, VSCODE_TERMINAL_BUTTON)
    buttons = [_button(label, "btn pri" if how == default else "btn", ("data-action", "resume"), ("data-how", how),
                       disabled=off) for how, label in choices]
    buttons.append(_button("コピー", "btn pri" if default == "copy" else "btn", ("data-action", "copy"),
                           ("data-copy", command or ""), disabled=off))
    return '<div class="btns">%s</div>' % "".join(buttons)


def _hit_section(session_id: str, turns: List[int], index: int, excerpt: Optional[List[ConvItem]],
                 terms: Sequence[str]) -> str:
    nav = _hit_buttons(index <= 0, index >= len(turns) - 1)
    out = ['<div class="sec">一致した箇所（%d/%d）%s</div>' % (index + 1, len(turns), nav)]
    body = _excerpt(excerpt or [], terms)
    if body:
        out.append('<div class="excerpt">%s</div>' % body)
    out.append('<div class="go">%s</div>' % _open_conversation(session_id, turns[index]))
    return "".join(out)


def _excerpt(items: List[ConvItem], terms: Sequence[str]) -> str:
    """一致したターン（views.turn_excerpt）の抜粋。本人の入力と、語を含む応答（EXCERPT_REPLIES まで）と、
    語を含むツールの要約（EXCERPT_TOOLS まで、1行ずつ）を、この順に出す。長いターンでも一致の周りだけを見せるため。
    応答にもツールにも語がなければ（入力にだけ一致したとき）、最初の応答を出す。"""
    words = [_collapse(w) for w in terms]
    prompt = [i for i in items if i.kind == "user"][:1]  # 1つのターンに本人の入力は1つ
    replies = [i for i in items if i.kind == "assistant"]
    shown = [i for i in replies if _marks(_collapse(i.text), words)][:EXCERPT_REPLIES]
    tools = [s for i in items if i.kind == "tools" for s in i.tools if _marks(_collapse(s), words)][:EXCERPT_TOOLS]
    if not shown and not tools:
        shown = replies[:1]
    out = ['<div class="who">%s</div><div class="x">%s</div>' % (
        _join([WHO[item.kind], _e(fmt_local(item.ts))]), _excerpt_text(item.text, terms)) for item in prompt + shown]
    if tools:
        out.append('<div class="who">ツール</div>')
        out += ['<div class="x tool">%s</div>' % _excerpt_text(s, terms) for s in tools]
    return "".join(out)


def _hit_buttons(first: bool = False, last: bool = False) -> str:
    """一致の移動（▲ 前・▼ 次）。first・last なら、その向きのボタンを止める。"""
    return (_button("▲", "btn nav", ("data-action", "hit"), ("data-dir", "prev"), ("aria-label", "前の一致"),
                    disabled=first)
            + _button("▼", "btn nav", ("data-action", "hit"), ("data-dir", "next"), ("aria-label", "次の一致"),
                      disabled=last))


def _recap(d: SessionDetail) -> str:
    if d.recap and d.recap.strip():
        when = fmt_local(d.recap_at)
        label = "直近の要約（%s に記録）" % when if when else "直近の要約"
        after = ""
        if d.prompts_after_recap > 0:
            after = '<div class="after">この要約の後の入力 %s 回</div>' % _n(d.prompts_after_recap)
        return '<div class="recap"><div class="lab">%s</div><div class="body">%s</div>%s</div>' % (
            _e(label), render_plain(d.recap), after)
    if d.last_reply:
        return ('<div class="recap reply"><div class="lab">最後の応答</div><div class="body">%s</div></div>'
                % render_plain(d.last_reply))
    return ""


def _kv(label: str, text: Optional[str]) -> str:
    value = '<span class="v">%s</span>' % render_plain(text) if text else '<span class="v none">なし</span>'
    return '<div class="kv"><span class="k">%s</span>%s</div>' % (label, value)


def _card_tasks(d: SessionDetail, choices: Sequence[Tuple[str, str]]) -> str:
    """「タスク」の行：結び付いたタスク（押すとタスクのカード）と、タスクに足す選択欄（choices は、まだ結び付いて
    いない足せるタスク。なければ欄を出さない）。"""
    if not d.tasks and not choices:
        return ""
    rows = ['<div%s><span class="p">%s</span><span class="w">%s</span></div>' % (
        _attrs(("class", "doc task"), ("data-action", "task-open"), ("data-task", name)), _e(title),
        _join([_e(rule), _e(confidence)])) for name, title, rule, confidence in d.tasks]
    return '<div class="sec">タスク</div>' + "".join(rows) + _task_adder(d.id, choices)


def _task_adder(session_id: str, choices: Sequence[Tuple[str, str]]) -> str:
    """「タスクに足す」：タスクの選択欄とボタン（セッションのカードと会話の画面）。choices がなければ空。"""
    if not choices:
        return ""
    options = "".join('<option value="%s">%s</option>' % (_e(name), _e(title)) for name, title in choices)
    return ('<div class="taskadd"><select class="pick-task" aria-label="タスク">%s</select>%s</div>' % (
        options, _button("タスクに足す", "btn", ("data-action", "task-link"), ("data-session", session_id))))


def _card_docs(d: SessionDetail) -> str:
    # 「すべて」は、そのセッションの場所で絞った資料の表示へ
    place = "" if d.place_id is None else d.place_id
    everything = _button("すべて", "more", ("data-action", "docs"), ("data-place", place))
    out = ['<div class="sec">資料 %s件%s</div>' % (_n(d.docs_total), everything)]
    for doc in d.docs:
        pairs = [("class", "doc")]  # type: List[Attr]
        if doc.doc_id is not None:  # 台帳の資料にあるものだけ開ける
            pairs.append(("data-doc", doc.doc_id))
        when = _join([_e(_size(doc.size)), _e(_mtime(doc.mtime))])
        out.append('<div%s><span class="kind">%s</span><span class="p">%s</span>%s</div>' % (
            _attrs(*pairs), _e(_kind(doc.kind)), _e(doc.display_path),
            '<span class="w">%s</span>' % when if when else ""))
    return "".join(out)


def _card_relations(d: SessionDetail) -> str:
    out = ['<div class="sec">つながり</div>']
    for r in d.relations:  # 推定のつながりの「（推定）」は views が言葉に付けてある
        info = _join([_place(r.peer_place_label, r.peer_place),
                      _e(_period(r.peer_started_at, r.peer_ended_at, days=True))])
        out.append('<div%s><span class="rel">%s</span>%s<span class="pt">%s</span>%s%s</div>' % (
            _attrs(("class", "link"), ("data-session", r.peer_id)), _e(r.label), _evidence(r.evidence),
            _e(r.peer_title), '<span class="tiny">（%s）</span>' % info if info else "",
            '<span class="badge moved">%s</span>' % MOVED_LABEL if r.cross_place else ""))
    if not d.relations:
        out.append('<div class="empty">なし</div>')
    return "".join(out)


def _open_conversation(session_id: str, turn: Optional[int]) -> str:
    return _button("会話で開く", "btn", ("data-action", "conversation"), ("data-session", session_id),
                   ("data-turn", "" if turn is None else turn))


# 右：場所の概要・資料・会話

def frag_place(ov: PlaceOverview) -> str:
    p = ov.place
    last = fmt_local(p.last_at)
    meta = _join(["対話" + _n(p.n_interactive), "ヘッドレス" + _n(p.n_headless), "資料" + _n(p.n_docs),
                  "最終 " + _e(last) if last else ""])
    buttons = "".join(_button(label, "btn", ("data-action", "open-place"), ("data-place", p.id), ("data-how", how))
                      for how, label in OPEN_BUTTONS)
    timeline = []
    for it in ov.timeline:
        if it.via_label:
            timeline.append('<div class="via">↓ %s%s</div>' % (_e(it.via_label), _evidence(it.via_evidence)))
        info = _join([_place(it.other_place_label or "", it.other_place or ""),
                      _e(_period(it.started_at, it.ended_at, days=True)), "入力%s回" % _n(it.n_prompts),
                      "子%s" % _n(it.n_children) if it.n_children else ""])
        timeline.append('<div%s><div class="t">%s</div><div class="tiny">%s</div></div>' % (
            _attrs(("class", "tli" if it.other_place is None else "tli other"), ("data-session", it.session_id)),
            _e(it.title), info))
    timeline_html = ('<div class="tl">%s</div>' % "".join(timeline) if timeline
                     else '<div class="empty">対話はありません</div>')
    folders = ['<div class="sec">資料（フォルダーごと）%s</div>'
               % _button("すべて", "more", ("data-action", "docs"), ("data-place", p.id))]
    for folder, items in ov.folders:
        folders.append('<div class="folder">%s</div>' % _e("./" if folder == "." else folder.rstrip("/") + "/"))
        folders += ['<div%s><span class="kind">%s</span><span class="p">%s</span></div>' % (
            _attrs(("class", "doc"), ("data-doc", item.id)), _e(_kind(item.kind)), _e(item.name)) for item in items]
    if not ov.folders:
        folders.append('<div class="empty">資料はありません</div>')
    orphans = ""
    if ov.orphans_total > 0:  # 押すと /frag/orphans の一覧を下の枠に入れる
        orphans = (_button("親が分からないヘッドレス %s件" % _n(ov.orphans_total), "more", ("data-action", "orphans"),
                           ("data-place", p.id))
                   + "<div%s></div>" % _attrs(("class", "children"), ("id", "orphans-%s" % p.id)))
    return ('<div class="place-ov"><div class="dh"><div class="head"><h4>%s</h4><div class="meta">%s</div></div>'
            '<div class="btns">%s</div></div><div class="cols"><div class="col"><div class="sec">つながり</div>%s</div>'
            '<div class="col">%s</div></div>%s</div>' % (_place(p.label, p.display), meta, buttons, timeline_html,
                                                          "".join(folders), orphans))


def frag_doc(d: DocumentDetail, *, terms: Sequence[str] = ()) -> str:
    """資料の表示。terms があれば、Markdown・テキストの本文に検索の語の印を付ける（HTML の資料の枠には触れない）。"""
    item = d.item
    is_html = (item.ext or "").lower() in _HTML_EXTS
    hows = OPEN_BUTTONS + ((("browser", "ブラウザで開く"),) if is_html else ())
    buttons = "".join(_button(label, "btn pri" if how == "vscode" else "btn", ("data-action", "open-doc"),
                              ("data-doc", item.id), ("data-how", how)) for how, label in hows)
    when = _mtime(item.mtime)
    meta = _join([_e(_kind(item.kind)), _e(_size(item.size)), _e(when + " 更新") if when else ""])
    location = _doc_location(item)
    links = "".join('<div%s>%s%s%s</div>' % (
        _attrs(("class", "link"), ("data-session", r.id)), _e(REL_PREFIXES.get(rel, "%s：" % rel)), _e(r.title),
        _badges(r)) for rel, r in d.links)
    if d.html is not None:  # Markdown とテキストは views が安全な HTML にしてある
        body = '<div class="md">%s</div>' % _mark_html(d.html, terms)
    elif is_html:  # HTML の資料は、スクリプトが画面に触れないよう隔離した枠で
        body = '<div class="docnote">外部の資料は読み込みません。元の表示は「ブラウザで開く」で見られます</div>'
        body += "<iframe%s></iframe>" % _attrs(("class", "docframe"), ("sandbox", "allow-scripts allow-popups"),
                                              ("src", "/doc-raw/%d" % item.id), ("title", item.name))
    else:
        body = '<div class="empty">この形式は外のアプリで開いてください</div>'
    return ('<div class="doc-view"><div class="dh"><div class="head"><h4>%s</h4><div class="path">%s</div></div>'
            '<div class="btns">%s</div></div><div class="meta">%s</div>%s%s</div>' % (
                _e(d.title or item.name), _e(location + "/" + item.name if location else item.name), buttons, meta,
                links, body))


def frag_conversation(w: ConversationWindow, *, title: str, terms: List[str], hit_turns: Sequence[int],
                      task_choices: Sequence[Tuple[str, str]] = ()) -> str:
    """会話。発言は Markdown にせず文字のまま出し、検索の語に印を付ける。続けて呼んだツールは畳む。
    長い会話は窓（views.conversation_window）で出し、前後に残りがあれば読み足すボタンを付ける。
    見出しには、題の下に「タスクに足す」を置く（task_choices があれば）。"""
    turns = [int(n) for n in hit_turns]
    tools = ""
    if turns:  # 移動は画面が <mark> の間で行うので、端でも止めない
        tools = '<span class="hits">一致 %sか所</span>' % _n(len(turns)) + _hit_buttons()
    tools += _button("中央を畳む", "btn", ("data-action", "fold"))
    back = _button("← 状況カードに戻る", "back", ("data-action", "card"), ("data-session", w.session_id))
    out = ["<div%s>" % _attrs(("class", "conv"), ("data-turns", _csv(turns))),
           '<div class="dh"><div class="head">%s<h4>%s ・ 会話</h4>%s</div><div class="btns">%s</div></div>' % (
               back, _e(title), _task_adder(w.session_id, task_choices), tools)]
    if w.source == "none":
        out.append('<div class="empty">会話の本文は残っていません</div>')
    out += [_read_more(w, "before"), _conversation_items(w, terms), _read_more(w, "after"), "</div>"]
    return "".join(out)


def frag_conversation_part(w: ConversationWindow, *, terms: Sequence[str], side: str) -> str:
    """会話に読み足す分（views.conversation_window の turns・item_offset で読んだ窓）。会話の枠と見出しは付けない。
    前に足す分（side="before"）は、まだ前に残りがあれば読み足すボタンを先に、後ろに足す分は後に付ける。"""
    more = _read_more(w, side)
    items = _conversation_items(w, terms)
    return more + items if side == "before" else items + more


def _read_more(w: ConversationWindow, side: str) -> str:
    """会話の窓の前（before）か後ろ（after）に残りがあれば、読み足すボタン。読み足す分と残りの数を書き、
    台帳の会話は読み込んである端のターンを data-turn に、ヘッドレスの記録は次の項目の番号を data-offset に持つ。"""
    left = w.before if side == "before" else w.after
    if w.source not in ("ledger", "transcript") or left <= 0:
        return ""
    if w.source == "transcript":
        if side == "before":  # 記録は先頭から読むので、前に読み足す分はない
            return ""
        edge, label = ("data-offset", w.offset + len(w.items)), MORE_ITEMS % (
            _n(min(views.TRANSCRIPT_ITEMS, left)), _n(left))
    else:
        edge, label = ("data-turn", w.first_turn if side == "before" else w.last_turn), MORE_TURNS[side] % (
            _n(min(views.CONVERSATION_TURNS, left)), _n(left))
    return _button(label, "conv-more", ("data-action", "read-more"), ("data-session", w.session_id),
                   ("data-dir", side), edge)


def _conversation_items(w: ConversationWindow, terms: Sequence[str]) -> str:
    """会話の窓の発言とツール。本人の入力には、ターンごとに最初の1つにだけ id="turn-N" を付ける
    （ヘッドレスの記録はターンがすべて 0 なので、続きの断片には付けない。ページの中で重なるため）。"""
    out = []
    anchored = set()  # type: Set[int]
    anchors = not (w.source == "transcript" and w.offset > 0)
    for item in w.items:
        if item.kind == "tools":
            out.append(_tools(item.tools, terms))
        elif item.kind in WHO:
            pairs = [("class", "turn u" if item.kind == "user" else "turn a")]  # type: List[Attr]
            if anchors and item.kind == "user" and item.turn not in anchored:
                anchored.add(item.turn)
                pairs.append(("id", "turn-%d" % item.turn))
            out.append('<div%s><div class="who">%s</div><div class="b">%s</div></div>' % (
                _attrs(*pairs), _join([WHO[item.kind], _e(fmt_local(item.ts))]), _marked_text(item.text, terms)))
    return "".join(out)


def _tools(summaries: List[str], terms: Sequence[str]) -> str:
    """「ツール N 回：…」の1行に畳む。語に当たる要約があれば開いておく。"""
    opened = " open" if any(_marks(s, terms) for s in summaries) else ""
    return '<details class="tools"%s><summary>ツール%s回：%s</summary><ol>%s</ol></details>' % (
        opened, _n(len(summaries)), _e(" ／ ".join(summaries[:TOOLS_IN_SUMMARY])),
        "".join("<li>%s</li>" % _marked_text(s, terms) for s in summaries))


# 部品

def _e(value: object) -> str:
    """文字と属性の値のエスケープ（引用符も）。"""
    return html.escape("" if value is None else str(value), quote=True)


def _attrs(*pairs: Attr) -> str:
    return "".join(' %s="%s"' % (name, _e(value)) for name, value in pairs)


def _button(label: str, cls: str, *pairs: Attr, disabled: bool = False) -> str:
    return "<button%s%s>%s</button>" % (_attrs(("class", cls), *pairs), " disabled" if disabled else "", _e(label))


def _join(parts: Iterable[str]) -> str:
    """空でない部分を「 ・ 」でつなぐ（部分はエスケープ済み）。"""
    return SEP.join(p for p in parts if p)


def _n(n: Optional[int]) -> str:
    return "{:,}".format(n or 0)


def _csv(numbers: Iterable[int]) -> str:
    return ",".join(str(int(n)) for n in numbers)


def _kind(kind: Optional[str]) -> str:
    return KIND_LABELS.get(kind or "", kind or "")


def _badges(r: SessionRow) -> str:
    out = []
    if r.kind == "headless":
        out.append('<span class="badge headless">ヘッドレス</span>')
    if r.live:
        out.append('<span class="badge live">実行中</span>')
    if not r.transcript_exists:
        out.append('<span class="badge gone">記録なし</span>')
    return "".join(out)


def _pr_numbers(urls: Sequence[str]) -> str:
    numbers = sorted({int(m.group(1)) for m in (_PR_NUMBER.search(u or "") for u in urls) if m})
    return "PR " + " ".join("#%d" % n for n in numbers) if numbers else ""


def _period(start: Optional[str], end: Optional[str], *, days: bool = False) -> str:
    """期間（days なら日付だけ）。始めと終わりが同じ表示なら1つにする。"""
    a, b = fmt_local(start), fmt_local(end)
    if days:
        a, b = a.split(" ")[0], b.split(" ")[0]
    if a and b and a != b:
        return a + ("〜" if days else " 〜 ") + b
    return a or b


def _mtime(ts: Optional[float]) -> str:
    """資料の更新時刻（UNIX 時刻）を地方時で。"""
    if ts is None:
        return ""
    try:
        return fmt_local(iso_utc(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def _size(n: Optional[int]) -> str:
    """ファイルの大きさ（500B・9KB・1.2MB）。"""
    if n is None or n < 0:
        return ""
    if n < 1024:
        return "%dB" % n
    if round(n / 1024) < 1024:
        return "%dKB" % round(n / 1024)
    if round(n / 1024 ** 2, 1) < 1024:
        return "%.1fMB" % (n / 1024 ** 2)
    return "%.1fGB" % (n / 1024 ** 3)


def _tilde(path: str, home: str) -> str:
    h = home.rstrip("/")
    if h and (path == h or path.startswith(h + "/")):
        return "~" + path[len(h):]
    return path


def _doc_location(item: DocItem) -> str:
    """資料のある所：場所の表示名とフォルダー（場所の外か場所がなければ、フォルダーの絶対パス）。資料の画面の
    パスの行に使う。"""
    folder = item.folder or ""
    if not item.place_display or folder.startswith(("~", "/")):
        return folder
    return item.place_display if folder in ("", ".") else item.place_display + "/" + folder


def _doc_where(item: DocItem) -> str:
    """一覧の行の、資料のある所（HTML）。_doc_location と同じ形で、場所だけを短い名前にする。"""
    folder = item.folder or ""
    if not item.place_display or folder.startswith(("~", "/")):
        return _e(folder)
    place = _place(item.place_label, item.place_display)
    return place if folder in ("", ".") else place + _e("/" + folder)


def _place(label: str, display: str) -> str:
    """場所の短い名前。表示名（ふつうはパスの全体）は title に入れ、マウスを乗せると見えるようにする。
    場所がなければ空。"""
    if not display:
        return ""
    return "<span%s>%s</span>" % (_attrs(("title", display)), _e(label or display))


def _evidence(text: Optional[str]) -> str:
    """つながりの根拠（relations.evidence）。種類の言葉のすぐ後に小さな字で。"""
    return '<span class="ev">%s</span>' % _e(text) if text else ""


@functools.lru_cache(maxsize=64)
def _term_pattern(terms: Tuple[str, ...]) -> Optional[Pattern[str]]:
    """語のどれかに当たる正規表現（検索と同じく、長い語を先に、英字だけ大文字小文字を区別しない）。"""
    words = sorted({w for w in terms if w}, key=lambda w: (-len(w), w))
    if not words:
        return None
    return re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE | re.ASCII)


def _marks(text: str, terms: Sequence[str]) -> List[Tuple[int, int]]:
    pattern = _term_pattern(tuple(terms))
    return [m.span() for m in pattern.finditer(text)] if pattern and text else []


_HTML_PART = re.compile(r"(<pre class=\"mermaid-src\">.*?</pre>|<[^>]*>)", re.S)


def _mark_html(body: str, terms: Sequence[str]) -> str:
    """views が安全にした HTML の、タグの外の文字だけに語の印を付ける。文字参照は戻してから当て、highlight で
    書き直す（&amp; などを壊さない）。Mermaid の元の文（pre.mermaid-src）とタグの中（属性）には付けない。
    印のない部分は元のまま返す。"""
    pattern = _term_pattern(tuple(terms))
    if pattern is None or not body:
        return body
    out = []
    for i, part in enumerate(_HTML_PART.split(body)):
        if i % 2 == 0 and part:  # 奇数番は、タグか Mermaid の元の文（そのまま）
            text = html.unescape(part)
            spans = [m.span() for m in pattern.finditer(text)]
            if spans:
                part = highlight(text, spans)
        out.append(part)
    return "".join(out)


def _snippet(s: Snippet, terms: Sequence[str]) -> str:
    """検索の抜粋。印がなければ検索の語で付ける。"""
    return highlight(s.text, s.marks or _marks(s.text, terms))


def _marked_text(text: str, terms: Sequence[str]) -> str:
    """文字のまま（Markdown にしない）語に印を付け、改行を <br> にする。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return highlight(text, _marks(text, terms)).replace("\n", "<br>")


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text or "").strip()


def _excerpt_text(text: str, terms: Sequence[str]) -> str:
    """空白をまとめ、最初に現れる語を中心に EXCERPT_CHARS 字を切り出して（切った側に …）、語に印を付ける。
    語がなければ先頭から切る。"""
    text = _collapse(text)
    terms = [_collapse(w) for w in terms]
    start, end = 0, len(text)
    if end > EXCERPT_CHARS:
        pattern = _term_pattern(tuple(terms))
        found = pattern.search(text) if pattern else None
        if found:
            start = found.start() - (EXCERPT_CHARS - (found.end() - found.start())) // 2
            start = min(max(start, 0), len(text) - EXCERPT_CHARS)
        end = start + EXCERPT_CHARS
    head = "…" if start > 0 else ""
    window = text[start:end]
    marks = [(a + len(head), b + len(head)) for a, b in _marks(window, terms)]
    return highlight(head + window + ("…" if end < len(text) else ""), marks)
