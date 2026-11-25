"""画面の資材（index.html・app.js・app.css）と、偽のデータを作る scripts/make_demo.py のテスト。"""
import hashlib
import json
import os
import pkgutil
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from tsuzuki import taskviews, views
from tsuzuki.config import load_config, resolve_tasks_root
from tsuzuki.indexer import update
from tsuzuki.search import SearchQuery, search
from tsuzuki.store import LEDGER_FILENAME, open_ledger
from tests.fixtures import allow_system_tmp

ROOT = Path(__file__).resolve().parent.parent
MAKE_DEMO = ROOT / "scripts" / "make_demo.py"
# 断片の data-action（タスクの操作を含む）。app.js はすべてを受け持つ
ACTIONS = {"resume", "copy", "conversation", "card", "hit", "children", "orphans", "open-doc", "open-place", "docs",
           "fold", "read-more", "task-stage", "task-waiting", "task-settings", "copy-picked", "task-unlink", "task-conversation",
           "new-task", "task-open", "task-link", "task-trash", "task-copy-doc", "form-cancel", "ticket-url-edit"}
# URL の # に残す状態の鍵（qdays・role・order は検索の期間・発言者・並び順。task と status はタスクの選択と状況）
HASH_KEYS = ["v", "p", "s", "q", "i", "t", "c", "d", "k", "days", "h", "qdays", "role", "order", "task", "status"]
# 断片（ui_render）が使う class。見た目はすべて app.css に置く
FRAGMENT_CLASSES = (
    "a", "after", "b", "back", "badge", "body", "btn", "btns", "card", "child", "children", "col", "cols", "conv",
    "conv-more", "dh",
    "doc", "doc-view", "docframe", "empty", "ev", "excerpt", "folder", "foot", "go", "gone", "head", "headless", "hist",
    "hits", "k", "kids", "kind", "kv", "lab", "link", "listhead", "listnote", "live", "m", "md", "meta", "more",
    "moved", "nav", "none", "notice", "on", "other", "p", "path", "ph", "place", "place-ov", "pri", "pt", "r", "recap",
    "rel", "reply", "row", "sec", "t", "tiny", "tl", "tli", "tool", "tools", "turn", "ty", "u", "v", "via", "w", "who",
    "x",
    "waiting-form",
)
COLOR_VARIABLES = ("--bg-primary", "--bg-secondary", "--bg-tertiary", "--border", "--text-primary", "--text-secondary",
                   "--text-tertiary", "--accent", "--selected-bg")


def _asset(name: str) -> str:
    return pkgutil.get_data("tsuzuki", "ui/" + name).decode("utf-8")


def _function(js: str, name: str) -> str:
    """app.js の最上位の関数の本体。"""
    found = re.search(r"\n(?:async )?function %s\([^)]*\) \{\n(.*?)\n\}\n" % re.escape(name), js, re.S)
    if found is None:
        raise AssertionError("app.js に関数 %s がない" % name)
    return found.group(1)


def _rule(css: str, selector: str) -> str:
    """app.css の最上位（@media の外）の、ちょうどその選択子の規則の中身。"""
    found = re.search(r"^%s \{(.*?)\}" % re.escape(selector), css, re.S | re.M)
    if found is None:
        raise AssertionError("app.css に %s の規則がない" % selector)
    return found.group(1)


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []  # (タグ, 属性の辞書)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class _Options(HTMLParser):
    """index.html の select ごとの選択肢 [(value, 文字)] と、select を包む label の文字（span）。"""

    def __init__(self):
        super().__init__()
        self.selects, self.labels = {}, {}
        self._select = self._option = self._span = None
        self._label_text = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "label":
            self._label_text = ""
        elif tag == "span":
            self._span = ""
        elif tag == "select":
            self._select = attrs.get("id")
            self.selects[self._select] = []
            self.labels[self._select] = self._label_text
        elif tag == "option" and self._select:
            self._option = [attrs.get("value"), ""]

    def handle_endtag(self, tag):
        if tag == "span" and self._span is not None:
            self._label_text, self._span = self._span, None
        elif tag == "option" and self._option is not None:
            self.selects[self._select].append(tuple(self._option))
            self._option = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data.strip()
        elif self._span is not None:
            self._span += data.strip()


class UiAssetsTest(unittest.TestCase):
    def test_shell_has_panes_and_meta(self):
        html = pkgutil.get_data("tsuzuki", "ui/index.html").decode()
        for part in ('id="pane-left"', 'id="pane-list"', 'id="pane-detail"', 'id="q"', 'name="tsuzuki-csrf"',
                     'src="/static/app.js"', 'id="place-select"'):
            self.assertIn(part, html)
        self.assertNotIn("<script>", html)          # インラインのスクリプトを書かない
        self.assertNotIn('style="', html)

    def test_trash_asks_first(self):
        body = _function(_asset("app.js"), "trashTask")
        for part in ("askToContinue(button.dataset.confirm", "/api/task-trash", "busyWriting()"):
            self.assertIn(part, body)

    def test_trash_goes_to_the_tasks_view(self):
        body = _function(_asset("app.js"), "trashTask")
        self.assertIn('go({ v: "tasks", task: "", d: "", q: "", s: "", c: "", t: "", i: "" }, false)', body)

    def test_untouched_waiting_reason_is_not_sent(self):
        body = _function(_asset("app.js"), "onSubmit")
        self.assertIn("if (reason.value !== reason.defaultValue) body.reason = reason.value;", body)

    def test_script_avoids_dialogs_and_eval(self):
        js = pkgutil.get_data("tsuzuki", "ui/app.js").decode()
        for bad in ("eval(", "alert(", "confirm(", "prompt(", "new Function"):
            self.assertNotIn(bad, js)
        self.assertIn("X-Tsuzuki-CSRF", js)

    def test_shell_parts_from_the_brief(self):
        html = _asset("index.html")
        for part in ('<meta name="tsuzuki-csrf" content="{{CSRF}}">',
                     '<meta name="tsuzuki-resume-default" content="{{RESUME_DEFAULT}}">',
                     '<meta name="tsuzuki-headless-in-list" content="{{HEADLESS_IN_LIST}}">',
                     '<span class="logo">Tsuzuki</span>',
                     '<button data-view="sessions">セッション</button>', '<button data-view="docs">資料</button>',
                     '<input id="q" placeholder="セッションと資料を検索（/ で移動）"', '<span id="status">',
                     '<button id="reindex">更新</button>', '<select id="place-select"', 'id="pane-left"',
                     'id="pane-list"', 'id="pane-detail"', 'id="toast"', 'id="confirm"', 'id="banner"',
                     '<link rel="stylesheet" href="/static/app.css">',
                     '<script src="/static/app.js" defer></script>'):
            self.assertIn(part, html)

    def test_shell_has_no_inline_code_or_style(self):
        parser = _Tags()
        parser.feed(_asset("index.html"))
        self.assertTrue(parser.tags)
        for tag, attrs in parser.tags:
            self.assertNotIn("style", attrs, tag)
            self.assertEqual([a for a in attrs if a.startswith("on")], [], tag)
            self.assertNotEqual(tag, "style")
            if tag == "script":
                self.assertEqual(attrs.get("src"), "/static/app.js")

    def test_task_md_rows_show_the_task_card_in_the_current_view(self):
        js = _asset("app.js")
        url = re.search(r"\nfunction detailUrl\(st\) \{\n(.*?)\n\}\n", js, re.S).group(1)
        self.assertIn('findRow("doc", st.d)', url)
        self.assertIn('"/frag/task/" + encodeURIComponent(row.dataset.task)', url)
        self.assertLess(url.index("row.dataset.task"), url.index('"/frag/doc/"'))
        self.assertNotIn("openTask(picked", js)  # 行を押してもタスクの表示へは移らない

    def test_script_handles_every_action_and_keeps_state_in_the_hash(self):
        js = _asset("app.js")
        table = re.search(r"\nconst ACTIONS = \{\n(.*?)\n\};", js, re.S)
        self.assertIsNotNone(table, "app.js に ACTIONS の表がない")
        self.assertEqual(set(re.findall(r'^ {2}"([a-z-]+)":', table.group(1), re.M)), ACTIONS)
        keys = re.search(r"\nconst HASH_KEYS = (\[[^\]]*\]);", js)
        self.assertIsNotNone(keys, "app.js に HASH_KEYS がない")
        self.assertEqual(json.loads(keys.group(1)), HASH_KEYS)
        for part in ("history.pushState", "history.replaceState", "hashchange", "popstate", "application/json",
                     "navigator.clipboard.writeText", "/api/status", "/api/reindex", "/api/resume", "/api/open",
                     "/api/open-place", "/api/ticket-url", "location.reload()"):
            self.assertIn(part, js)
        # 見た目は class と hidden で切り替える（style 属性は CSP で止まる）
        self.assertNotIn(".style", js)
        self.assertNotIn("setAttribute(\"style\"", js)

    def test_script_selects_only_rows_whose_id_fits_the_hash(self):
        # 台帳のセッション ID は UUID とは限らない。URL の # に残せない ID の行を選ぶと normalize で消え、
        # 最初の行の自動選択が同じ描画をくり返して画面が止まる。そうした行は ↑↓・クリック・自動選択で選ばない
        js = _asset("app.js")
        selectable = _function(js, "isSelectable")
        self.assertIn("SESSION_ID.test(", selectable)
        self.assertIn("NUMBER.test(", selectable)
        self.assertIn(".filter(isSelectable)", _function(js, "selectableRows"))
        self.assertIn("isSelectable(row)", _function(js, "rowChanges"))
        self.assertIn("rowChanges(row)", _function(js, "selectRow"))
        # 自動選択は、状態が変わるときだけ描画し直す
        self.assertIn("normalize(", _function(js, "changesState"))
        self.assertRegex(_function(js, "render"), r"if \(changes && changesState\(changes\)\) go\(changes, false\);")

    def test_stylesheet(self):
        css = _asset("app.css")
        narrow = re.search(r"@media \(max-width: 1199\.98px\) \{\n(.*?)\n\}", css, re.S)
        self.assertIsNotNone(narrow, "幅 1,200px 未満の @media がない")
        self.assertRegex(narrow.group(1), r"#pane-left\s*\{[^}]*display: none")
        self.assertRegex(narrow.group(1), r"#place-select")
        self.assertRegex(css, r"@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme=\"light\"\]\) \{")
        root = re.search(r"^:root \{(.*?)\}", css, re.S | re.M)
        self.assertIsNotNone(root)
        for var in COLOR_VARIABLES:
            self.assertIn(var + ":", root.group(1))
        for cls in FRAGMENT_CLASSES:
            self.assertRegex(css, r"\.%s(?![\w-])" % re.escape(cls), "app.css に .%s の見た目がない" % cls)
        for selector in ("#back", "#forward"):
            self.assertRegex(css, r"%s[,\s{:]" % re.escape(selector), "app.css に %s の見た目がない" % selector)
        self.assertNotIn("@import", css)  # 外から読まない
        self.assertNotRegex(css, r"url\(\s*['\"]?(https?:)?//")

    def test_back_and_forward_buttons(self):
        html = _asset("index.html")
        head = re.search(r'<header id="top">\s*(<button[^>]*>.*?</button>)\s*(<button[^>]*>.*?</button>)', html, re.S)
        self.assertIsNotNone(head, "header#top の最初の2つが button ではない")
        for button, ident in zip(head.groups(), ("back", "forward")):
            self.assertIn('id="%s"' % ident, button)
            self.assertIn('type="button"', button)
            self.assertRegex(button, r"\bdisabled\b")
        js = _asset("app.js")
        for part in ("history.back()", "history.forward()", "{ n:"):
            self.assertIn(part, js)
        self.assertIn('addEventListener("popstate", fromPopState)', js)
        self.assertIn("event.state", _function(js, "fromPopState"))
        self.assertIn("syncHistoryButtons()", _function(js, "fromPopState"))
        # 新しい項目を積むと先の項目は消えるので、「→」の限りは今の番号になる
        self.assertIn("furthest = position;", _function(js, "go"))
        remember = _function(js, "rememberPosition")
        self.assertRegex(remember, r"\} else \{[^}]*furthest = position;")
        self.assertIn("furthest = Math.max(furthest, position);", remember.split("} else {")[0])
        self.assertIn("function syncHistoryButtons()", js)
        self.assertIn("function rememberPosition(", js)

    def test_clicked_rows_are_kept_in_history(self):
        js = _asset("app.js")
        self.assertIn("selectRow(picked, true)", _function(js, "onClick"))
        self.assertIn("selectRow(rows[next], false)", _function(js, "moveSelection"))

    def test_long_paths_do_not_scroll_the_panes_sideways(self):
        # パスのように切れ目のない文字は、どこででも折り返す（どのペインも横には動かない）
        css = _asset("app.css")
        self.assertIn("overflow-wrap: anywhere", _rule(css, "#pane-left, #pane-list, #pane-detail"))
        # 印は1行のまま。つながりの行は、入りきらない部分を次の行へ回し、印も場所と期間も縮めない・縮めすぎない
        self.assertIn("white-space: nowrap", _rule(css, ".badge"))
        self.assertIn("flex-wrap: wrap", _rule(css, ".link"))
        self.assertIn("flex-shrink: 0", _rule(css, ".link .badge"))
        self.assertNotRegex(css, r"\.link \.tiny \{[^}]*flex-shrink: 0")  # 長い場所の名前が行からはみ出さない
        # 左の列の場所の短い名前は、省略（…）せずに折り返す
        name = _rule(css, ".place span")
        self.assertIn("white-space: normal", name)
        self.assertNotIn("text-overflow", name)
        # 一覧を上限で切ったことの知らせと、つながりの根拠の小さな字
        self.assertIn("font-size", _rule(css, ".listnote"))
        self.assertIn("font-size", _rule(css, ".ev"))

    def test_search_can_be_narrowed_by_period_and_speaker_and_sorted(self):
        # 検索は期間・発言者（本人・Claude）で絞れ、並び順（新しい順・一致の多い順）を選べる
        parser = _Options()
        parser.feed(_asset("index.html"))
        self.assertEqual(parser.selects["opt-qdays"], [("7", "7日"), ("30", "30日"), ("90", "90日"), ("all", "すべて")])
        self.assertEqual(parser.selects["opt-role"], [("", "すべて"), ("user", "本人"), ("assistant", "Claude")])
        self.assertEqual(parser.selects["opt-order"], [("recent", "新しい順"), ("count", "一致の多い順")])
        self.assertEqual(parser.labels["opt-qdays"], "期間")
        self.assertEqual(parser.labels["opt-role"], "発言者")
        self.assertEqual(parser.labels["opt-order"], "並び順")
        # 3つは検索のときだけ出し、一覧の期間は検索のときに隠す
        css = _asset("app.css")
        self.assertRegex(css, r"body:not\(\.searching\) \.search-opt \{[^}]*display: none")
        self.assertRegex(css, r"body\.searching #opt-days-row \{[^}]*display: none")
        js = _asset("app.js")
        # 既定（すべて・新しい順）は # に書かない。一覧の期間（既定 30日）とは別の鍵
        normalize = _function(js, "normalize")
        self.assertIn('st.qdays = DAY_CHOICES.includes(st.qdays) ? st.qdays : "all";', normalize)
        self.assertIn('st.role = ROLES.includes(st.role) ? st.role : "";', normalize)
        self.assertIn('st.order = st.order === "count" ? "count" : "recent";', normalize)
        self.assertIn('(key === "qdays" && value === "all")', _function(js, "toHash"))
        self.assertIn('(key === "order" && value === "recent")', _function(js, "toHash"))
        # 一覧の検索と左の場所の数は、同じ絞り込みで頼む（サーバーは同じ検索を1回で済ませる）
        params = _function(js, "searchParams")
        for part in ("q: st.q", "place: st.p", "targets: searchTargets(st)", 'days: st.qdays === "all" ? "" : st.qdays',
                     "role: st.role", 'order: st.order === "recent" ? "" : st.order'):
            self.assertIn(part, params)
        self.assertIn('if (st.q) return "/frag/search" + query(searchParams(st));', _function(js, "listUrl"))
        self.assertIn('if (st.q) return "/frag/places" + query(searchParams(st));', _function(js, "leftUrl"))
        sync = _function(js, "syncChrome")
        for part in ("ui.qdays.value = st.qdays;", "ui.role.value = st.role;", "ui.order.value = st.order;"):
            self.assertIn(part, sync)
        # 絞り込みを変えたら、新しい検索と同じく最初の一致を選び直す
        for key, node in (("qdays", "ui.qdays"), ("role", "ui.role"), ("order", "ui.order")):
            self.assertIn('%s.addEventListener("change", () => refine({ %s: %s.value }));' % (node, key, node), js)
        self.assertIn('go(Object.assign({ s: "", d: "", c: "", t: "", i: "" }, changes), false);',
                      _function(js, "refine"))

    def test_long_conversation_reads_more_without_losing_the_place(self):
        # 「前を読む」「後を読む」で隣の窓を取りに行き、会話に足す。前に足すときは、読んでいた発言が
        # 画面の同じ高さに残るようにスクロールを戻す。足した後も一致の印（▲▼・一致 i/n）は今の印のまま数え直す
        js = _asset("app.js")
        self.assertIn('"read-more": (button) => readMore(button),', js)
        body = _function(js, "readMore")
        for part in ('params.offset = button.dataset.offset;',
                     'params[button.dataset.dir === "before" ? "before" : "after"] = button.dataset.turn;',
                     'fetchText("/frag/conversation/" + encodeURIComponent(button.dataset.session) + query(params))',
                     "button.disabled = true;", "if (!button.isConnected) return;",
                     'const anchor = button.dataset.dir === "before" ? button.nextElementSibling : null;',
                     'button.insertAdjacentHTML("beforebegin", text);', "button.remove();",
                     "ui.detail.scrollTop += anchor.getBoundingClientRect().top - top;", "refreshMarks();"):
            self.assertIn(part, body)
        self.assertLess(body.index("const top = "), body.index("insertAdjacentHTML"))
        marks = _function(js, "refreshMarks")
        for part in ("const current = marks[markIndex] || null;", 'marks = Array.from(conv.querySelectorAll("mark"));',
                     "const index = current ? marks.indexOf(current) : -1;", "showMark(index < 0 ? 0 : index, false);"):
            self.assertIn(part, marks)
        css = _asset("app.css")
        self.assertIn("display: block", _rule(css, ".conv-more"))

    def test_list_fills_in_while_indexing(self):
        # 初回の更新の間も、読み終えたセッションから一覧に出す。更新の時刻は終わったときにしか
        # 変わらないので、更新の間は状態を見るたびに一覧を取り直す（中身が同じなら入れ替えない）
        poll = _function(_asset("app.js"), "pollStatus")
        self.assertIn("if (lastUpdate !== undefined && (data.last_update_at !== lastUpdate || indexing)) refresh();",
                      poll)

    def test_narrow_place_select_keeps_the_full_path(self):
        # 狭い窓の場所の選択肢も短い名前。左の列の名前の title（表示名）を、選択肢と選択の title に写す
        body = _function(_asset("app.js"), "fillSelect")
        self.assertIn("option.title = name.title", body)
        self.assertIn("select.title = ", body)


class TaskViewAssetsTest(unittest.TestCase):
    """タスクの表示の画面の資材。"""


    def test_nav_has_tasks_first(self):
        html = _asset("index.html")
        self.assertLess(html.index('data-view="tasks"'), html.index('data-view="sessions"'))
        self.assertIn('name="tsuzuki-has-tasks"', html)

    def test_app_knows_task_keys(self):
        js = _asset("app.js")
        for word in ('"task"', '"status"', '"task-stage"', '"task-waiting"', '"copy-picked"', '"task-unlink"',
                     '"task-conversation"', "/frag/tasks", "/frag/task/", "/api/task-new"):
            self.assertIn(word, js)

    # 足したテスト

    def test_shell_offers_tasks(self):
        html = _asset("index.html")
        self.assertIn('<meta name="tsuzuki-has-tasks" content="{{HAS_TASKS}}">', html)
        self.assertIn('<button data-view="tasks">タスク</button>', html)
        self.assertLess(html.index('data-view="sessions"'), html.index('data-view="docs"'))
        self.assertIn('<select id="status-select" aria-label="状況">', html)  # 狭い窓で状況を選ぶ

    def test_first_view_follows_the_tasks_and_is_not_written_to_the_hash(self):
        # # に表示がなければ、タスクがあればタスク、なければセッション。既定の表示は # に書かない
        js = _asset("app.js")
        self.assertIn('const DEFAULT_VIEW = metaContent("tsuzuki-has-tasks") === "1" ? "tasks" : "sessions";', js)
        normalize = _function(js, "normalize")
        # v がない # （選んだセッション・資料、検索の語、一覧の期間・ヘッドレス）は、これまでどおり
        # セッションの表示で開く（days と h だけの # も）
        self.assertIn('if (!VIEWS.includes(st.v)) st.v = !st.task && (st.s || st.d || st.q || st.days || st.h) ? '
                      '"sessions" : DEFAULT_VIEW;', normalize)
        # days と h は、既定の値を入れる前の # の値で見る
        self.assertLess(normalize.index("if (!VIEWS.includes(st.v))"), normalize.index("st.days = DAY_CHOICES"))
        self.assertLess(normalize.index("if (!VIEWS.includes(st.v))"), normalize.index("st.h = st.h ==="))
        self.assertLess(normalize.index("st.task = isTaskName"), normalize.index("if (!VIEWS.includes(st.v))"))
        self.assertLess(normalize.index("if (!SESSION_ID.test(st.s))"), normalize.index("if (!VIEWS.includes(st.v))"))
        self.assertIn("st.task = isTaskName(st.task) ? st.task : \"\";", normalize)
        self.assertIn("st.status = STATUSES.includes(st.status) ? st.status : \"\";", normalize)
        self.assertIn('(key === "v" && value === DEFAULT_VIEW)', _function(js, "toHash"))
        self.assertIn('const STATUSES = ["", "done", "all"];', js)
        # タスクのフォルダー名：/ を含まず、. で始まらない
        name = _function(js, "isTaskName")
        self.assertIn('!name.includes("/")', name)
        self.assertIn('!name.startsWith(".")', name)

    def test_task_view_loads_the_task_fragments(self):
        js = _asset("app.js")
        self.assertIn('if (st.v === "tasks") return "/frag/tasks" + query({ status: st.status, place: st.p });',
                      _function(js, "listUrl"))
        self.assertIn('if (st.v === "tasks") return "/frag/task-filters" + query({ status: st.status, place: st.p });',
                      _function(js, "leftUrl"))
        detail = _function(js, "detailUrl")
        self.assertIn('"/frag/task/" + encodeURIComponent(st.task)', detail)
        # カードの中の資料の題（data-doc）は、タスクの表示のまま資料を開く
        self.assertLess(detail.index("if (st.d)"), detail.index('if (st.v === "tasks")'))
        # 一覧の行の data-task も、↑↓・押す・最初の行の自動の選択の対象にする
        self.assertIn(".row[data-task]", _function(js, "selectableRows"))
        self.assertIn("isTaskName(row.dataset.task", _function(js, "isSelectable"))
        self.assertIn("task: row.dataset.task", _function(js, "rowChanges"))

    def test_doc_pane_gets_query_and_scrolls(self):
        js = _asset("app.js")
        detail = _function(js, "detailUrl")
        # 資料の URL に q を付ける（タスクの表示では付けない）
        self.assertIn('st.v === "tasks" ? "" : query({ q: st.q })', detail)
        self.assertIn('"/frag/doc/" + st.d +', detail)
        # 資料の断片の後で、最初の印へ送る（印がなければ送らない）
        setup = _function(js, "setupDetail")
        self.assertIn('ui.detail.querySelector(".doc-view .md mark")', setup)
        self.assertIn('scrollIntoView({ block: "center" })', setup)

    def test_task_writes_send_the_card_stamp_and_reload(self):
        # 段階と待ち：カードの data-mtime（repr の文字）を Number で送れば、サーバーの float にそのまま戻る
        js = _asset("app.js")
        write = _function(js, "writeTask")
        for part in ('button.closest(".task-card")', "mtime: Number(card.dataset.mtime)",
                     "size: Number(card.dataset.size)"):
            self.assertIn(part, write)
        self.assertIn('"task-stage": (button) => writeTask("/api/task-stage", button, '
                      '{ stage: button.dataset.stage }),', js)
        self.assertIn('"task-waiting": (button) => openWaitingForm(button)', js)
        submit = _function(js, "onSubmit")
        for part in ('"waiting-form"', '"/api/task-waiting"', "body.reason"):
            self.assertIn(part, submit)
        self.assertIn('"form-cancel": (button) => cancelForm(button)', js)
        self.assertIn('"task-settings": (button) => openSettings(button)', js)
        self.assertIn('load("detail", "/frag/task-settings/" + encodeURIComponent(task))', _function(js, "openSettings"))
        self.assertIn("saveSettings(form)", submit)
        save = _function(js, "saveSettings")
        for part in ('"/api/task-settings"', "mtime: Number(", "size: Number(", "workspace:", "reloadTasks()"):
            self.assertIn(part, save)
        cancel = _function(js, "cancelForm")
        self.assertIn('"task-settings"', cancel)
        self.assertIn('load("detail", detailUrl(state), true)', cancel)
        self.assertIn("form.hidden = true", cancel)
        unlink = _function(js, "unlinkSession")
        self.assertIn('sendTask("/api/task-link", { task: card.dataset.task, session_id: button.dataset.session, '
                      'action: "remove" })', unlink)
        # 書けなかったとき（ok が false）も文をそのまま知らせ、サーバーが読み直したタスクを取り直す
        send = _function(js, "sendTask")
        for part in ("if (busyWriting()) return;", "const answer = await postWrite(path, body);",
                     "toast(answer.message);", "await reloadTasks();"):
            self.assertIn(part, send)
        reload = _function(js, "reloadTasks")
        for part in ('load("list", listUrl(st), true)', 'load("left", leftUrl(st), true)', 'load("detail", url, true)'):
            self.assertIn(part, reload)

    def test_copies_are_built_by_the_server(self):
        # @ の形はサーバーが作る（app.js は data-copy をつなぐだけ。@ を組み立てない）
        js = _asset("app.js")
        picked = _function(js, "copyPicked")
        self.assertIn("input.pick:checked", picked)
        self.assertIn('.join(" ")', picked)
        self.assertNotRegex(js, r"""["'`]@""")
        conversation = _function(js, "startConversation")
        self.assertIn('postWrite("/api/task-conversation", { task: card.dataset.task, how: how })', conversation)
        self.assertIn("copyText(answer.command)", conversation)

    def test_new_conversation_is_sent_once(self):
        # 「新しい会話」を続けて押しても、会話を2つ始めない（ほかの書く操作と同じく、答えを待つ間は送らない）
        conversation = _function(_asset("app.js"), "startConversation")
        self.assertIn("if (busyWriting()) return;", conversation)
        self.assertLess(conversation.index("if (busyWriting()) return;"), conversation.index("writing = true;"))
        self.assertLess(conversation.index("writing = true;"),
                        conversation.index('postWrite("/api/task-conversation"'))
        self.assertIn("} finally {\n    writing = false;\n  }", conversation)

    def test_slow_writes_and_dropped_clicks_are_told(self):
        # 書く操作は、更新の間は台帳の鍵を待つ（初回で1分ほど）。1.5秒たっても答えがなければ待っていると知らせ、
        # 答えを待つ間に押された操作は送らずにそう知らせる
        js = _asset("app.js")
        self.assertIn("const WRITE_WAIT_MS = 1500;", js)
        self.assertIn('const WRITE_WAIT_TEXT = "更新の終わりを待っています…";', js)
        self.assertIn('const WRITE_BUSY_TEXT = "前の操作の終わりを待っています";', js)
        post = _function(js, "postWrite")
        for part in ("setTimeout(", "WRITE_WAIT_MS", "toast(WRITE_WAIT_TEXT, { wait: true });",
                     "return await post(path, body);", "clearTimeout(timer);", "if (toastWaits) hideToast();"):
            self.assertIn(part, post)
        busy = _function(js, "busyWriting")
        self.assertIn("if (!writing) return false;", busy)
        self.assertIn("toast(WRITE_BUSY_TEXT", busy)
        # 待っている知らせは、答えが来るまで消さない
        self.assertIn("if (!sticky && !opts.wait) toastTimer = setTimeout(hideToast, TOAST_MS);", _function(js, "toast"))
        # タスクに書く操作（段階・待ち・外す・足す・新しいタスク・新しい会話）はすべて、この2つを通す
        for name in ("sendTask", "linkToTask", "createTask", "startConversation"):
            with self.subTest(name=name):
                body = _function(js, name)
                self.assertIn("busyWriting()", body)
                self.assertIn("postWrite(", body)
                self.assertNotIn("await post(", body)
                self.assertNotIn("if (writing) return;", body)

    def test_add_to_task_from_the_card_and_the_conversation(self):
        # 「タスクに足す」はセッションのカードと会話の画面の見出しにある。欄は .taskadd の中。
        # 会話は読んでいる所を保つため取り直さず、足したタスクを選択肢から外す（なくなれば欄ごと外す）
        link = _function(_asset("app.js"), "linkToTask")
        self.assertIn('button.closest(".taskadd")', link)
        self.assertIn('box.closest(".conv")', link)
        self.assertIn("option.remove()", link)
        self.assertIn("box.remove()", link)
        self.assertIn('load("detail", url, true)', link)

    def test_new_task_form(self):
        js = _asset("app.js")
        self.assertIn('const NEW_TASK_URL = "/frag/new-task";', js)
        self.assertIn('"new-task": () => openNewTask(),', js)
        create = _function(js, "createTask")
        self.assertIn('postWrite("/api/task-new", ', create)
        self.assertIn('go({ v: "tasks", task: answer.task,', create)
        self.assertIn('document.addEventListener("submit", onSubmit);', js)

    def test_phase1_handlers_fit_the_task_card(self):
        js = _asset("app.js")
        # 再開はタスクのカードから送らない（続きからのボタンはない。⌘Enter もタスクの表示では何もしない）
        self.assertNotIn("resumeTask", js)
        self.assertNotIn("data-session", _function(js, "cardSession"))
        self.assertNotIn("task-card", js[js.index('const ACTIONS = {'):js.index('"copy": (button)')])
        self.assertNotIn("task", _function(js, "resumeSession").split("{", 1)[0])
        self.assertNotIn("task:", _function(js, "resumeSession"))
        self.assertNotIn("tasks", _function(js, "defaultResume"))
        # 会話の行は、セッションの表示へ移ってカードを開く
        self.assertIn('"card": (button) => openCard(button.dataset.session),', js)
        self.assertIn('v: "sessions"', _function(js, "openCard"))
        # 共通の資料の行（data-action を持つ data-place）は、狭い窓の場所の選択肢に入れない
        sync = _function(js, "syncSelects")
        self.assertIn('".place[data-place]:not([data-action])"', sync)
        self.assertIn('".place[data-status]"', sync)
        # タスクのカードの外枠は、行の選択として扱わない
        self.assertIn('"task-card"', _function(js, "onClick"))

    def test_task_stylesheet(self):
        css = _asset("app.css")
        for selector in (".stp.cur", ".stp.done", ".chip.stage", ".chip.wait", ".chip.tk", ".task-card .md",
                         ".new-task input"):
            self.assertRegex(css, r"(^|[\s,])%s[\s,{:]" % re.escape(selector), selector)
        self.assertRegex(css, r"body\.tasks #list-opts \{[^}]*display: none")
        narrow = re.search(r"@media \(max-width: 1199\.98px\) \{\n(.*?)\n\}", css, re.S).group(1)
        self.assertRegex(narrow, r"body\.tasks #status-select \{[^}]*display: inline-block")

    def test_every_class_of_the_task_fragments_has_a_style(self):
        # ui_tasks の断片が使う class はすべて app.css に見た目がある（見本の A に合わせる）
        from tsuzuki.ui_tasks import frag_new_task, frag_task_card, frag_task_filters, frag_task_list
        from tests.test_ui_tasks import file_item, make_detail, make_row
        rows = [make_row(live=True, pr_numbers=["24"]), make_row(name="b", status="waiting", ticket=None),
                make_row(name="c", status="done", stage="完了")]
        detail = make_detail(stage="PR", stage_from_pr=True, after=("その後の要約", "2026-11-21T09:00:00Z"),
                             folder_cut=True, warnings=["task.md に現在地がありません", "段階の値を読めません: x"])
        detail.folder_files.append(file_item("/Users/me/tasks/2026-11-20-a/gone.md", missing=True))
        fragments = (frag_task_filters({"open": 2}, [(3, "web-app", 2)], status="open", place=None, shared_place=7),
                     frag_task_list(rows, selected="b", status="all"),
                     frag_task_card(detail, resume_default="vscode"), frag_new_task([("/r", "r")]))
        parser = _Tags()
        parser.feed("".join(fragments))
        classes = {c for _, attrs in parser.tags for c in (attrs.get("class") or "").split()}
        self.assertTrue({"task-card", "stp", "chip", "pick", "cp", "new-task"} <= classes)
        css = _asset("app.css")
        for cls in sorted(classes):
            self.assertRegex(css, r"\.%s(?![\w-])" % re.escape(cls), "app.css に .%s の見た目がない" % cls)


class SmallFixesAssetsTest(unittest.TestCase):
    """狭い窓のカードの見出し、検索窓の消去、表示の切り替え。"""

    def test_clear_and_theme_buttons(self):
        html = _asset("index.html")
        self.assertIn('id="q-clear"', html)
        self.assertIn('aria-label="検索語を消す"', html)
        self.assertIn('id="theme"', html)

    def test_theme_css(self):
        css = _asset("app.css")
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn(':root:not([data-theme="light"])', css)

    def test_theme_js(self):
        js = _asset("app.js")
        for word in ('"tsuzuki-theme"', "effectiveTheme", "prefers-color-scheme: dark", "q-clear"):
            self.assertIn(word, js)
        self.assertRegex(js, r"try\s*\{[^}]*localStorage")

    def test_theme_choice_is_sent_to_the_app(self):
        # 表示を切り替えるたびアプリへ送る。ブラウザ（window.webkit がない）では try の中で何もしない
        body = _function(_asset("app.js"), "cycleTheme")
        call = "window.webkit.messageHandlers.tsuzukiTheme.postMessage(themeChoice)"
        self.assertIn(call, body)
        self.assertRegex(body, r"try\s*\{\s*" + re.escape(call) + r";\s*\}\s*catch")

    def test_card_header_wraps_outside_the_narrow_media_query(self):
        # 見出しの折り返しは窓の幅でなく見出しの幅で決める。規則はメディアクエリの外にある
        css = _asset("app.css")
        match = re.search(r"@media \(max-width: 1199\.98px\) \{\n(.*?)\n\}", css, re.S)
        self.assertIsNotNone(match)
        narrow = match.group(1)
        outside = css[:match.start()] + css[match.end():]
        for rule in (
            ".task-card > .dh, .card > .dh, .doc-view > .dh, .place-ov > .dh { flex-wrap: wrap; }",
            ".task-card > .dh .head, .card > .dh .head, .doc-view > .dh .head, .place-ov > .dh .head { flex: 1 1 18rem; }",
            ".dh .btns { max-width: 100%; }",
        ):
            self.assertIn(rule, outside)
        self.assertNotIn(".dh", narrow)


class MermaidAssetsTest(unittest.TestCase):
    """Markdown の資料の Mermaid の図。"""

    def test_mermaid_client(self):
        js = _asset("app.js")
        for word in ("/mermaid-frame", '"tsuzuki-mermaid"', "event.source", "effectiveTheme"):
            self.assertIn(word, js)
        self.assertIn(".mermaid-frame", _asset("app.css"))
        # 枠は、サーバーが覚えさせる版つきの URL で開く
        from tsuzuki.server import MERMAID_FRAME_URL
        self.assertIn('const MERMAID_URL = "%s";' % MERMAID_FRAME_URL, js)

    def test_conversation_diagrams_are_drawn_and_kept_when_reading_more(self):
        js = _asset("app.js")
        body = _function(js, "setupMermaid")
        self.assertIn(".conv .md .mermaid-block", body)
        self.assertIn('block.querySelector("iframe.mermaid-frame")', body)
        self.assertNotIn("mermaidFrames = [];", body)
        self.assertIn("setupMermaid();", _function(js, "readMore"))

    def test_client_isolates_the_frames_and_checks_the_replies(self):
        js = _asset("app.js")
        # 枠は scripts だけを許す隔離（同じ起源にはしない）。描くのは資料の画面の Markdown の図だけ
        self.assertIn('frame.setAttribute("sandbox", "allow-scripts")', js)
        self.assertNotIn("allow-same-origin", js)
        self.assertIn('".doc-view .md .mermaid-block, .conv .md .mermaid-block"', js)
        # 返事は、自分の作った枠（contentWindow）からの、決まった形のものだけを受け取る
        receive = _function(js, "receiveMermaid")
        self.assertIn("event.source", receive)
        self.assertIn(".contentWindow === event.source", receive)
        self.assertIn('"tsuzuki-mermaid-result"', receive)
        self.assertNotIn("innerHTML", receive)
        self.assertNotIn("innerHTML", _function(js, "failMermaid"))
        self.assertIn("textContent", _function(js, "failMermaid"))
        # 表示が変わったら描き直す。枠の高さには上限がある
        self.assertRegex(js, r'document\.addEventListener\("tsuzuki-theme", ')
        self.assertRegex(js, r"const MERMAID_MAX_HEIGHT = \d+;")

    def test_mermaid_stylesheet(self):
        # 図を描けたらコードを隠し、枠を出す。描けなければコードと失敗の文
        css = _asset("app.css")
        for cls in ("mermaid-block", "mermaid-src", "mermaid-frame", "mermaid-error"):
            self.assertRegex(css, r"\.%s(?![\w-])" % re.escape(cls), "app.css に .%s の見た目がない" % cls)
        self.assertIn("display: none", _rule(css, ".mermaid-block.drawn .mermaid-src"))
        # 描けた枠の高さは height 属性で決める（style は使わない）ので、描けた枠には CSS で高さを書かない
        self.assertIn("height: 0", _rule(css, ".mermaid-block:not(.drawn) .mermaid-frame"))
        self.assertNotIn("height", _rule(css, ".mermaid-frame"))
        self.assertIn('setAttribute("height", ', _function(_asset("app.js"), "showMermaid"))

    def test_frame_page(self):
        page = pkgutil.get_data("tsuzuki", "ui/mermaid-frame.html").decode("utf-8")
        self.assertEqual(page.count("{{MERMAID}}"), 1)
        for word in ('securityLevel: "strict"', "startOnLoad: false", 'theme === "dark" ? "dark" : "default"',
                     "mermaid.render(", '"tsuzuki-mermaid"', '"tsuzuki-mermaid-result"', "event.source !== window.parent",
                     'postMessage('):
            self.assertIn(word, page)
        # 外のものは読み込まず、知らせる先も親の画面だけ
        self.assertNotRegex(page, r"\b(src|href)=|@import|url\(|fetch\(|XMLHttpRequest|eval\(|new Function")
        self.assertNotIn("window.top", page)

    def test_frame_cleans_up_only_its_own_render(self):
        # mermaid は描画を順に行い、次の描画の作業用の要素（body の #d<ID>）を、前の描画の結果がこちらに届く前に
        # 足す。後始末は、その描画の ID の要素だけを消す（body の要素をまとめて消すと、次の描画が描けなくなる）。
        # 古い描画（count !== drawing）は、自分の要素を消すだけで、図も返事も変えない
        page = pkgutil.get_data("tsuzuki", "ui/mermaid-frame.html").decode("utf-8")
        self.assertNotIn("document.body.children", page)
        self.assertNotIn("clearStray", page)
        self.assertIn('var id = "tsuzuki-mermaid-" + count;', page)
        self.assertIn("mermaid.render(id, code)", page)
        self.assertIn('document.getElementById("d" + id)', page)
        for callback in ("}).then(function (result) {", "}, function (error) {"):
            with self.subTest(callback=callback):
                start = page.index(callback) + len(callback)
                lines = [line.strip() for line in page[start:].splitlines() if line.strip()]
                self.assertEqual(lines[:2], ["removeOwn(id);", "if (count !== drawing) return;"])

    def test_vendored_library(self):
        vendor = ROOT / "src" / "tsuzuki" / "ui" / "vendor"
        library = (vendor / "mermaid.min.js").read_bytes()
        record = (vendor / "VENDOR.md").read_text(encoding="utf-8")
        # 記録した版の sha256 と一致する（書き換えていない）
        self.assertIn(hashlib.sha256(library).hexdigest(), record)
        self.assertIn("https://registry.npmjs.org/mermaid/-/mermaid-11.", record)
        self.assertIn("MIT License", (vendor / "mermaid-LICENSE.txt").read_text(encoding="utf-8"))
        # 普通の <script> で読むと大域の mermaid を作る形。<script> の中にそのまま入れられる
        self.assertTrue(library.rstrip().endswith(b'globalThis["mermaid"] = globalThis.__esbuild_esm_mermaid_nm'
                                                  b'["mermaid"].default;'))
        self.assertNotRegex(library.decode("utf-8"), r"(?i)</?script")


class MakeDemoTest(unittest.TestCase):
    """scripts/make_demo.py が作る偽のデータを、本物の更新で台帳に入れて確かめる。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.out = os.path.join(self.tmp, "demo")

    def make_demo(self, out):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # スクリプトが自分でパスを足す
        return subprocess.run([sys.executable, str(MAKE_DEMO), out], capture_output=True, text=True, cwd=self.tmp,
                              env=env, timeout=120)

    def index_demo(self):
        """偽のデータを作り、tsuzuki serve と同じく、設定のタスクの置き場所も渡して台帳に入れる。"""
        proc = self.make_demo(self.out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ledger, claude = Path(self.out) / "ledger", Path(self.out) / "claude"
        home = Path(self.out) / "home"  # メモリの資料は ~/.claude の下で見分けるので、サーバーには偽のホームを渡す
        cfg = load_config(ledger)
        tasks_root = resolve_tasks_root(cfg, home=str(home), claude_dir=claude)
        with allow_system_tmp():
            conn = open_ledger(ledger / LEDGER_FILENAME)
            self.addCleanup(conn.close)
            report = update(conn, cfg, claude_dir=claude, ledger_dir=ledger, home=str(home), tasks_root=tasks_root)
        self.assertEqual((report.files_failed, report.format_warning, report.errors), (0, None, []))
        return proc, cfg, conn, tasks_root

    def test_demo_is_indexable(self):
        proc, cfg, conn, _ = self.index_demo()
        ledger, claude = Path(self.out) / "ledger", Path(self.out) / "claude"
        home = Path(self.out) / "home"
        # 画面を開くコマンドを出す
        self.assertIn(" HOME=%s " % home, proc.stdout)
        self.assertIn("TSUZUKI_HOME=%s" % ledger, proc.stdout)
        self.assertIn("--claude-dir %s serve" % claude, proc.stdout)
        # 一時領域では資料が索引に入らないことを知らせる
        self.assertIn("一時領域", proc.stderr)
        self.assertEqual(cfg.resume_default, "copy")  # ⌘Enter でアプリを起動しない
        self.assertEqual(cfg.display_strip_prefixes, [self.out])
        self.assertEqual(cfg.claude_dir, str(claude))  # --claude-dir を忘れても本物の ~/.claude を読まない
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731

        kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM sessions GROUP BY kind"))
        self.assertEqual((kinds.get("interactive"), kinds.get("headless")), (10, 5))
        self.assertEqual(len(views.list_sessions(conn, days=30, live={})), 10)  # 一覧の既定の期間にすべて入る
        self.assertGreater(len(views.list_sessions(conn, days=30, live={})),
                           len(views.list_sessions(conn, days=7, live={})))
        recaps = one("SELECT COUNT(*) FROM sessions WHERE kind = 'interactive' AND recap IS NOT NULL")
        self.assertTrue(0 < recaps < 10)  # 要約あり・なし
        self.assertGreaterEqual(one("SELECT COUNT(*) FROM session_prs"), 2)
        self.assertTrue(one("SELECT COUNT(*) FROM messages WHERE role = 'tool'"))

        places = {display: pid for pid, display in conn.execute("SELECT id, display FROM places")}
        # タスクのフォルダーで始めたセッションの場所は「タスク」、shared/ は「共通の資料」
        self.assertEqual(set(places), {"work/recipe-site", "work/sales-report", "work/reading-notes", "タスク",
                                       "共通の資料"})
        relations = {tuple(r) for r in conn.execute("SELECT type, confidence FROM relations")}
        for rel in (("fork", "confirmed"), ("continued", "confirmed"), ("handoff", "strong"), ("child", "strong"),
                    ("reference", "strong")):
            self.assertIn(rel, relations)
        overview = views.place_overview(conn, places["work/recipe-site"], home=self.tmp)
        vias = [item.via_label for item in overview.timeline]
        for label in ("続きの会話へ", "引き継ぎ資料を読んで開始", "分岐", "別の場所へ移った"):
            self.assertIn(label, vias)
        self.assertTrue(overview.folders)
        self.assertEqual(views.place_overview(conn, places["work/sales-report"], home=self.tmp).orphans_total, 1)

        self.assertGreaterEqual(one("SELECT COUNT(*) FROM documents"), 8)
        doc_kinds = {k for (k,) in conn.execute("SELECT DISTINCT kind FROM documents")}
        self.assertLessEqual({"memory", "design", "handoff", "plan", "progress", "readme", "review", "other"},
                             doc_kinds)
        self.assertEqual(one("SELECT place_id FROM documents WHERE kind = 'memory'"), places["work/sales-report"])
        html_path = one("SELECT path FROM documents WHERE ext = '.html'")
        with open(html_path, encoding="utf-8") as f:
            self.assertIn("<svg", f.read())
        rendered = [views.document_detail(conn, doc_id, home=self.tmp).html or ""
                    for (doc_id,) in conn.execute("SELECT id FROM documents WHERE ext = '.md'")]
        self.assertTrue(any("<table>" in h and "<pre><code>" in h for h in rendered))
        self.assertEqual(one("SELECT COUNT(*) FROM history_prompts h LEFT JOIN sessions s ON s.id = h.session_id "
                             "WHERE s.id IS NULL"), 2)

        # 画面で確かめる検索（「引き継ぎ」）：会話・資料・入力履歴に当たり、一致のない場所がある
        result = search(conn, SearchQuery("引き継ぎ"))
        self.assertGreaterEqual(len(result.sessions), 2)
        self.assertGreaterEqual(len(result.sessions[0].turns), 2)  # 状況カードの ▲▼ で移れる
        self.assertTrue(result.documents)
        self.assertEqual(len(result.history), 2)
        hit_places = {pid for pid, n in result.places if n}
        self.assertIn(places["work/recipe-site"], hit_places)
        self.assertNotIn(places["work/reading-notes"], hit_places)

    def test_demo_has_tasks(self):
        # 偽のホームの下のタスクの置き場所（タスク3つと shared/）。設定の tasks_root がそこを指し、
        # 偽の記録のセッションは、タスクの規則で結び付く
        proc, cfg, conn, root = self.index_demo()
        home = Path(self.out) / "home"
        self.assertEqual(root, home / "tasks")
        self.assertEqual(cfg.tasks_root, str(home / "tasks"))  # 本物の ~/tasks を読まない
        self.assertIn(str(home / "tasks"), proc.stdout)
        self.assertTrue((home / "tasks" / "shared").is_dir())

        rows = {r.name: r for r in taskviews.list_tasks(conn, status="all", live={})}
        self.assertEqual(len(rows), 3)
        for name in rows:
            self.assertRegex(name, r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+$")  # YYYY-MM-DD-<短い名前>
        self.assertEqual(sorted(r.status for r in rows.values()), ["active", "done", "waiting"])
        self.assertTrue(any(r.stage_from_pr for r in rows.values()))  # PR の記録から
        self.assertTrue(any(r.ticket is None for r in rows.values()))  # チケットなし
        # 最後の動きは、セッションと資料の時刻（数日前）。フォルダーを作った今の時刻にならない
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        for r in rows.values():
            self.assertLess(r.last_at[:19], day_ago, r.name)
        links = {tuple(r) for r in conn.execute("SELECT rule, confidence FROM task_sessions")}
        for link in (("cwd", "confirmed"), ("wrote", "confirmed"), ("read_task_md", "strong"),
                     ("from_source", "strong"), ("same_branch", "weak"), ("ticket", "weak")):
            self.assertIn(link, links)

        # 進行中のタスクのカード：フォルダーの資料、task.md が名指した共通の資料、ほかの場所の資料、その後の要約
        active = next(r for r in rows.values() if r.status == "active")
        detail = taskviews.task_detail(conn, active.name, live={}, home=str(home),
                                       exclude_dirs=cfg.doc_scan.exclude_dirs, ticket_template=cfg.ticket_url,
                                       default_branches=cfg.default_branches, max_depth=cfg.doc_scan.max_depth)
        self.assertGreaterEqual(len(detail.folder_files), 3)
        self.assertEqual({f.kind for f in detail.folder_files}, {"cover", "research", "design"})
        self.assertTrue(detail.shared_docs)
        self.assertTrue(detail.other_docs)
        self.assertIsNotNone(detail.after)
        self.assertIsNotNone(detail.task_md_doc_id)  # 「task.md を開く」を押せる
        self.assertEqual(detail.branch, "feature/search")
        self.assertTrue(detail.prs)

    def test_runs_again_over_its_own_output(self):
        self.assertEqual(self.make_demo(self.out).returncode, 0)
        projects = os.path.join(self.out, "claude", "projects")
        first = sorted(os.listdir(projects))
        # 前の回にサーバーが作った台帳と、前の回にはなかった記録。作り直すと残らない
        stale = [os.path.join(self.out, "ledger", LEDGER_FILENAME), os.path.join(projects, first[0], "stale.jsonl")]
        for path in stale:
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
        again = self.make_demo(self.out)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(sorted(os.listdir(projects)), first)
        for path in stale:
            self.assertFalse(os.path.exists(path), path)

    def test_refuses_a_directory_it_did_not_make(self):
        os.makedirs(self.out)
        keep = os.path.join(self.out, "keep.txt")
        with open(keep, "w", encoding="utf-8") as f:
            f.write("消さない")
        proc = self.make_demo(self.out)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("空ではありません", proc.stderr)
        self.assertEqual(os.listdir(self.out), ["keep.txt"])


if __name__ == "__main__":
    unittest.main()
