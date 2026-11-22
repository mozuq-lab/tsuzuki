"""画面の HTML の断片のテスト。断片は innerHTML でページに入るので、台帳の文字のエスケープを特に確かめる。"""
import html as htmllib
import re
import tempfile
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from tsuzuki import views
from tsuzuki.config import Config
from tsuzuki.documents import KIND_LABELS
from tsuzuki.indexer import update
from tsuzuki.render import fmt_local, render_markdown
from tsuzuki.search import DocHit, HistoryHit, SearchQuery, SearchResult, SessionHit, Snippet, search
from tsuzuki.sources.live import LiveSession
from tsuzuki.ui_render import (
    frag_card, frag_children, frag_conversation, frag_conversation_part, frag_doc, frag_docs_filters, frag_docs_list,
    frag_orphans, frag_place, frag_places, frag_search, frag_session_list,
)
from tsuzuki.views import (
    ChildRow, ConvItem, ConversationWindow, DocItem, DocLinkView, DocumentDetail, PlaceItem, PlaceOverview,
    RelationView, SessionDetail, SessionRow, TimelineItem,
)
from tests.fixtures import FakeClaude, assistant, make_ledger, recap, sid, t, tool_result, user

S1, S2, S3, S7, S9 = sid(1), sid(2), sid(3), sid(7), sid(9)
HOME = "/Users/u"
SEP = " ・ "
DAYS = 60 * 24  # t() の1日分
# 台帳から来うる悪意のある文字。タグにも属性にもならず、文字のまま出なければならない
EVIL = '<script>alert(1)</script>"\'><img src=x onerror=alert(1)>&amp;'
EVIL_ID = 'x" onclick="alert(1)'
EVIL_FILE = '<img src=x onerror=alert(1)>"\'&amp;.md'  # パスの最後の要素にできる形（/ を含まない）

# 断片が出してよいタグと属性（render_markdown が出すタグを含む）。style 属性とイベント属性は出さない
ALLOWED_TAGS = {"div", "span", "em", "h4", "button", "mark", "br", "details", "summary", "ol", "li", "iframe",
                "p", "h1", "h2", "h3", "h5", "h6", "ul", "pre", "code", "blockquote", "hr", "table", "thead",
                "tbody", "tr", "th", "td", "strong", "a"}
ALLOWED_ATTRS = {"class", "id", "disabled", "open", "sandbox", "src", "title", "href", "rel", "target", "align",
                 "start", "aria-label"}


class Markup(HTMLParser):
    """断片を読み、開始タグ（名前と属性）と文字を集める。"""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags, self.chunks = [], []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def values(self, name: str):
        return [attrs[name] for _, attrs in self.tags if name in attrs]


def texts(fragment: str, cls: str):
    """class がちょうど cls の div（入れ子の div を持たないもの）の文字。"""
    return [htmllib.unescape(re.sub(r"<[^>]+>", "", body))
            for body in re.findall(r'<div class="%s"[^>]*>(.*?)</div>' % re.escape(cls), fragment)]


def at(minutes: float) -> float:
    """t(minutes) と同じ時刻の UNIX 時刻（資料の mtime の形）。"""
    return datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() + minutes * 60


def day(ts: str) -> str:
    return fmt_local(ts).split(" ")[0]


def row(**kw) -> SessionRow:
    values = dict(id=S1, title="作業の続き", kind="interactive", place_id=4, place_display="dev/w", ended_at=t(10),
                  n_prompts=3, n_children=0, prs=[], summary="要約です。", summary_is_recap=True, live=False,
                  transcript_exists=True, place_label="w")
    values.update(kw)
    return SessionRow(**values)


def detail(**kw) -> SessionDetail:
    values = dict(id=S1, title="作業の続き", kind="interactive", place_id=4, place_display="dev/w", ended_at=t(60),
                  n_prompts=3, n_children=0, prs=[], summary="要約です。", summary_is_recap=True, live=False,
                  transcript_exists=True, place_label="w", branch="main", started_at=t(0), n_agents=0, recap=None,
                  recap_at=t(50),
                  prompts_after_recap=0, first_prompt="最初の依頼の文", last_prompt="最後の依頼の文",
                  last_reply="最後の応答の文", cwd="/w", docs=[], docs_total=0, relations=[], children_total=0,
                  resume_command="cd /w && claude --resume " + S1)
    values.update(kw)
    if not values["transcript_exists"] and "resume_command" not in kw:
        values["resume_command"] = None  # views は記録の消えたセッションに再開のコマンドを出さない
    return SessionDetail(**values)


def doc_item(**kw) -> DocItem:
    values = dict(id=7, path="/r/w/docs/a.md", name="a.md", kind="design", place_display="dev/w", folder="docs",
                  mtime=at(10), size=9 * 1024, ext=".md", place_label="w")
    values.update(kw)
    return DocItem(**values)


def doc_detail(ext=".md", *, title=None, html=None, links=()) -> DocumentDetail:
    name = "a" + ext
    return DocumentDetail(item=doc_item(path="/r/w/docs/" + name, name=name, ext=ext), title=title, html=html,
                          links=list(links))


class Ledger(unittest.TestCase):
    """検索の一致に題名や場所を添えるための、一時ディレクトリの台帳。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = make_ledger(Path(tmp.name))
        self.addCleanup(self.conn.close)
        self.seq = 0
        for pid, path, display in ((1, "/w/garden", "work/garden"), (2, "/w/plan", "work/plan"),
                                   (3, "/w/old", "work/old")):
            self.conn.execute("INSERT INTO places(id, path, display) VALUES (?, ?, ?)", (pid, path, display))
        self.session(S1, "interactive", 1, first_prompt="料理の引き継ぎ資料を作って", ended=101)
        self.messages(S1, [(1, "user", "料理の引き継ぎ資料を作って", 100), (1, "assistant", "作りました", 101)])
        self.session(S2, "interactive", 2, first_prompt="引き継ぎをお願い", ended=11)
        self.messages(S2, [(1, "user", "引き継ぎをお願い", 10), (1, "assistant", "はい", 11)])
        self.session(S9, "headless", 2, ended=50)
        self.conn.execute("INSERT INTO digests(session_id, first_prompt, text) VALUES (?, ?, ?)",
                          (S9, "計画の課題をやる", "計画の課題をやる"))
        self.conn.execute("INSERT INTO documents(id, path, place_id, kind, ext, size, mtime, title, body) "
                          "VALUES (1, '/w/garden/handoff.md', 1, 'handoff', '.md', 100, ?, ?, ?)",
                          (at(0), "料理の仕事の引き継ぎ", "# 料理の仕事の引き継ぎ"))
        # 記録のないセッション S7 の入力履歴
        self.conn.execute("INSERT INTO history_prompts(id, session_id, project, place_id, ts, text) "
                          "VALUES (1, ?, '/w/old', 3, ?, '引き継ぎをお願いします')", (S7, t(5)))
        self.conn.commit()

    def session(self, session_id, kind, place, *, first_prompt=None, ended=None, title=None, exists=1):
        self.conn.execute(
            "INSERT INTO sessions(id, transcript_path, transcript_exists, kind, place_id, ended_at, first_prompt, "
            "custom_title) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, "/x/%s.jsonl" % session_id, exists, kind, place, t(ended) if ended is not None else None,
             first_prompt, title))

    def messages(self, session_id, rows):
        for turn, role, text, minute in rows:
            self.seq += 1
            self.conn.execute("INSERT INTO messages(session_id, seq, turn, ts, role, text) VALUES (?, ?, ?, ?, ?, ?)",
                              (session_id, self.seq, turn, t(minute), role, text))


class SafeMarkup:
    def assertSafe(self, fragment: str) -> Markup:
        """知っているタグと属性だけで、インラインのスクリプトも style 属性もイベント属性もないこと。"""
        lowered = fragment.lower()
        self.assertNotIn("<script", lowered)
        self.assertNotIn("<img", lowered)
        m = Markup(fragment)
        for tag, attrs in m.tags:
            self.assertIn(tag, ALLOWED_TAGS, fragment)
            for name in attrs:
                self.assertTrue(name in ALLOWED_ATTRS or name.startswith("data-"), (tag, name, fragment))
        return m


class UiRenderTest(SafeMarkup, Ledger):

    def test_list_escapes_titles(self):
        html = frag_session_list([row(title='<img src=x onerror=1>')], selected=None)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_card_parts(self):
        html = frag_card(detail(recap="作業中です。", prompts_after_recap=2), resume_default="vscode")
        for part in ('data-action="resume" data-how="vscode"', 'data-how="terminal"', 'data-action="copy"',
                     "直近の要約", "この要約の後の入力 2 回", "最初の依頼", "最後の依頼", "資料", "つながり", "会話で開く"):
            self.assertIn(part, html)
        self.assertRegex(html, r'class="[^"]*pri[^"]*"[^>]*data-how="vscode"|data-how="vscode"[^>]*class="[^"]*pri')

    def test_card_task_row_and_add(self):
        html = frag_card(detail(tasks=[("a", "A の題", "フォルダーで開始", "確定")]), resume_default="vscode")
        self.assertIn('data-action="task-open"', html)
        self.assertIn('data-task="a"', html)
        self.assertIn("A の題", html)
        self.assertNotIn('data-action="task-link"', html)
        # 結び付いたタスクがあっても、まだ結び付いていないタスクに足せる
        html = frag_card(detail(tasks=[("a", "A の題", "フォルダーで開始", "確定")]), resume_default="vscode",
                         task_choices=[("b", "B の題")])
        self.assertEqual(html.count('<div class="sec">タスク</div>'), 1)
        self.assertLess(html.index('data-action="task-open"'), html.index('data-action="task-link"'))
        self.assertIn('<option value="b">B の題</option>', html)
        html = frag_card(detail(), resume_default="vscode", task_choices=[("a", "A の題"), ("b", "<b>")])
        self.assertIn('data-action="task-link"', html)
        self.assertIn("タスクに足す", html)
        self.assertIn('<option value="a">A の題</option>', html)
        self.assertIn("&lt;b&gt;", html)
        self.assertNotIn("タスクに足す", frag_card(detail(), resume_default="vscode"))

    def test_docs_kinds_include_task_kinds(self):
        html = frag_docs_filters({"cover": 1, "research": 2, "response": 1, "notes": 1}, [], kind=None, place_id=None)
        for label in ("表紙", "調査", "レビュー対応", "メモ"):
            self.assertIn(label, html)

    def test_doc_row_shows_task(self):
        html = frag_docs_list([doc_item(id=3, task=("a", "A の題")), doc_item(id=4, name="task.md", kind="cover",
                                                                       task=("b", "B の題"), task_cover=True),
                               doc_item(id=5, name="task.md", task=("c", "C の題"))], selected=None)
        self.assertIn("A の題", html)
        self.assertNotIn('data-task="a"', html)  # task.md 以外の行は資料を開く
        self.assertIn('data-doc="3"', html)
        self.assertIn('data-doc="4" data-task="b"', html)  # 選べる行のまま、タスクの名前を持つ
        # タスクのフォルダーの直下にない task.md（入れ子）は、資料として開く
        self.assertIn("C の題", html)
        self.assertNotIn('data-task="c"', html)

    def test_card_without_transcript(self):
        html = frag_card(detail(transcript_exists=False), resume_default="vscode")
        self.assertIn("記録は削除済みのため再開できません", html)
        self.assertEqual(html.count("disabled"), 3)

    def test_badges(self):
        html = frag_session_list([row(live=True), row(transcript_exists=False)], selected=None)
        self.assertIn("実行中", html)
        self.assertIn("記録なし", html)

    def test_search_fragment(self):
        conn = self.conn
        result = search(conn, SearchQuery("引き継ぎ"))
        html = frag_search(conn, result, terms=["引き継ぎ"], selected=None)
        self.assertIn("会話 2セッション ・ 資料 1件 ・ 入力履歴 1件", html)
        self.assertIn("<mark>引き継ぎ</mark>", html)
        self.assertIn("記録は削除済み・再開不可", html)

    def test_frag_doc_marks_terms_outside_tags(self):
        d = doc_detail(html='<h2>Layout の話</h2><p>a &amp; b の layout を <a href="/layout">見る</a></p>'
                            '<div class="mermaid-block"><pre class="mermaid-src"><code>graph TD; layout--&gt;x</code></pre></div>')
        html = frag_doc(d, terms=["layout"])
        self.assertIn("<h2><mark>Layout</mark> の話</h2>", html)
        self.assertIn("a &amp; b の <mark>layout</mark> を", html)
        self.assertIn('href="/layout"', html)  # 属性の中には付けない
        self.assertIn("<code>graph TD; layout--&gt;x</code>", html)  # Mermaid の元の文には付けない
        self.assertNotIn("<mark>", frag_doc(d))  # 語がなければ今までどおり

    def test_frag_doc_marks_terms_with_entity_characters(self):
        d = doc_detail(html="<p>a &amp; b と x &lt; y と &quot;q&quot;</p>")
        self.assertIn("<p><mark>a &amp; b</mark> と x &lt; y と &quot;q&quot;</p>", frag_doc(d, terms=["a & b"]))
        self.assertIn("と <mark>x &lt; y</mark> と", frag_doc(d, terms=["x < y"]))
        self.assertNotIn("&amp;amp;", frag_doc(d, terms=["amp"]))  # 文字参照の名前そのものには当てない
        self.assertNotIn("<mark>amp</mark>", frag_doc(d, terms=["amp"]))

    def test_html_document_uses_sandboxed_frame(self):
        self.assertIn('sandbox="allow-scripts allow-popups"', frag_doc(doc_detail(ext=".html")))

    def test_html_document_note_says_external_resources_are_not_loaded(self):
        note = "外部の資料は読み込みません"
        html = frag_doc(doc_detail(ext=".html"))
        self.assertIn(note, html)
        self.assertIn("ブラウザで開く", html)
        for ext, body in ((".md", "<p>x</p>"), (".txt", "<p>x</p>"), (".pdf", None)):
            with self.subTest(ext=ext):
                self.assertNotIn(note, frag_doc(doc_detail(ext=ext, html=body)))

    # エスケープ

    def test_every_fragment_escapes_hostile_text(self):
        evil_row = row(id=EVIL_ID, title=EVIL, place_display=EVIL, place_label=EVIL, summary=EVIL, n_children=2,
                       prs=["https://github.com/o/r/pull/3" + EVIL])
        evil_detail = detail(
            id=EVIL_ID, title=EVIL, place_display=EVIL, place_label=EVIL, branch=EVIL, recap=EVIL, recap_at=t(5),
            first_prompt=EVIL, last_prompt=EVIL, last_reply=EVIL, cwd=EVIL, resume_command=EVIL, live=True,
            docs=[DocLinkView(5, EVIL, EVIL, EVIL, "created", 0.0, 10)], docs_total=1,
            relations=[RelationView("reference", "weak", EVIL, EVIL_ID, EVIL, EVIL, t(0), t(1), True,
                                    evidence=EVIL, peer_place_label=EVIL)])
        place = PlaceItem(4, EVIL, 1, 2, 3, t(5), label=EVIL)
        doc = doc_item(id=9, path="/r/" + EVIL_FILE, name=EVIL_FILE, kind=EVIL, place_display=EVIL, folder=EVIL,
                       ext=EVIL, place_label=EVIL)
        page = doc_item(id=10, path="/r/" + EVIL_FILE + ".html", name=EVIL_FILE + ".html", ext=".html")
        conversation = ConversationWindow(EVIL_ID, [ConvItem("user", t(0), EVIL, 1, []),
                                                    ConvItem("assistant", t(1), EVIL, 1, []),
                                                    ConvItem("tools", t(1), "", 1, [EVIL, EVIL])], 0, 1, 1, "ledger")
        fragments = {
            "places": frag_places([place], selected=4),
            "places-search": frag_places([place], selected=None, counts={4: 2}),
            "list": frag_session_list([evil_row], selected=EVIL_ID),
            "card": frag_card(evil_detail, resume_default="copy", hit_turns=[2, 1], hit_index=0,
                              excerpt=[ConvItem("user", t(1), EVIL, 2, []), ConvItem("tools", t(1), "", 2, [EVIL])],
                              terms=[EVIL, "alert"]),
            "children": frag_children([ChildRow(EVIL_ID, t(1), EVIL)], session_id=EVIL_ID, offset=0, has_more=True),
            "orphans": frag_orphans([ChildRow(EVIL_ID, t(1), EVIL)], place_id=4, offset=0, has_more=True),
            "place": frag_place(PlaceOverview(place, [TimelineItem(EVIL_ID, EVIL, t(0), t(1), 1, 1, EVIL, EVIL,
                                                                   via_evidence=EVIL, other_place_label=EVIL)],
                                              [(EVIL, [doc])], 3)),
            "docs-filters": frag_docs_filters({EVIL: 1, "design": 2}, [place], kind=EVIL, place_id=4),
            "docs": frag_docs_list([doc], selected=9),
            "doc-md": frag_doc(DocumentDetail(doc, EVIL, render_markdown(EVIL), [("created", evil_row)])),
            "doc-html": frag_doc(DocumentDetail(page, None, None, [(EVIL, evil_row)])),
            "conversation": frag_conversation(conversation, title=EVIL, terms=["alert", EVIL], hit_turns=[1]),
        }
        for name, fragment in fragments.items():
            with self.subTest(fragment=name):
                m = self.assertSafe(fragment)
                self.assertTrue(EVIL in m.text or EVIL_FILE in m.text, fragment)
        for name in ("list", "card", "children", "place", "conversation"):
            self.assertIn(EVIL_ID, Markup(fragments[name]).values("data-session"), name)
        self.assertIn("children-" + EVIL_ID, Markup(fragments["list"]).values("id"))
        self.assertEqual(Markup(fragments["card"]).values("data-copy"), [EVIL])
        self.assertIn(EVIL, Markup(fragments["docs-filters"]).values("data-kind"))
        self.assertEqual(Markup(fragments["doc-html"]).values("title"), [EVIL_FILE + ".html"])
        # 場所の表示名は title の値として、そのまま戻る（属性を抜け出さない）
        for name in ("places", "places-search", "list", "card", "place", "docs-filters", "docs"):
            self.assertIn(EVIL, Markup(fragments[name]).values("title"), name)
        # つながりの根拠は、エスケープした文字として出る
        for name in ("card", "place"):
            spans = re.findall(r'<span class="ev">(.*?)</span>', fragments[name])
            self.assertEqual([htmllib.unescape(s) for s in spans], [EVIL], name)

    def test_search_escapes_hostile_text(self):
        self.conn.execute("UPDATE places SET display = ? WHERE id = 1", (EVIL,))
        self.conn.execute("UPDATE sessions SET custom_title = ? WHERE id = ?", (EVIL, S1))
        self.conn.execute("UPDATE documents SET path = ?, kind = ? WHERE id = 1", ("/w/garden/" + EVIL_FILE, EVIL))
        result = SearchResult(
            sessions=[SessionHit(S1, [1], 1, t(100), Snippet(EVIL, [(0, 8)], "user", t(100)))],
            headless=[SessionHit(S9, [], 1, t(50), Snippet(EVIL, [], "digest", t(50)))],
            documents=[DocHit(1, Snippet(EVIL, [(3, 9)], "document", t(0)))],
            history=[HistoryHit(1, S7, EVIL, t(5), Snippet(EVIL, [], "user", t(5)), None)],
            places=[], elapsed_ms=1.0)
        m = self.assertSafe(frag_search(self.conn, result, terms=["alert"], selected=S1, home=HOME))
        self.assertIn(EVIL, m.text)
        self.assertIn(EVIL_FILE, m.text)

    def test_search_shows_path_for_path_only_document(self):
        # パスにだけ語がある資料は、抜粋にパス（ホームを ~ にしたもの）を出し、語に印を付ける
        self.conn.execute("UPDATE documents SET path = ? WHERE id = 1", (HOME + "/dev/proj/layout.html",))
        result = SearchResult(sessions=[], headless=[], history=[], places=[], elapsed_ms=1.0,
                              documents=[DocHit(1, Snippet(HOME + "/dev/proj/layout.html", [], "document", t(0)),
                                                path_only=True)])
        html = frag_search(self.conn, result, terms=["layout.html"], selected=None, home=HOME)
        self.assertIn('<div class="r">~/dev/proj/<mark>layout.html</mark></div>', html)
        self.assertNotIn(HOME, html)

    def test_docs_list_shows_snippets_when_searching(self):
        item = doc_item(id=5, name="layout.html", path=HOME + "/proj/content/layout.html")
        hit = DocHit(5, Snippet(HOME + "/proj/content/layout.html", [], "document", None), path_only=True)
        html = frag_docs_list([item], selected=None, hits={5: hit}, terms=["layout"], home=HOME)
        self.assertIn('<div class="r">~/proj/content/<mark>layout</mark>.html</div>', html)
        self.assertNotIn('class="r"', frag_docs_list([item], selected=None))  # 検索語がなければ抜粋なし

    # 状況カード

    def test_copy_button_carries_exact_command(self):
        command = "cd '/Users/u/my dir/it'\"'\"'s' && claude --resume " + S1
        html = frag_card(detail(resume_command=command), resume_default="copy")
        self.assertEqual(Markup(html).values("data-copy"), [command])
        self.assertIn('<button class="btn pri" data-action="copy"', html)
        self.assertNotIn('class="btn pri" data-action="resume"', html)
        self.assertNotIn("disabled", html)

    def test_resume_buttons(self):
        html = frag_card(detail(), resume_default="terminal")
        self.assertIn('<button class="btn" data-action="resume" data-how="vscode">VS Code で再開</button>', html)
        self.assertIn('<button class="btn pri" data-action="resume" data-how="terminal">Terminal で再開</button>',
                      html)
        self.assertIn('<button class="btn" data-action="copy" data-copy="cd /w &amp;&amp; claude --resume %s">'
                      'コピー</button>' % S1, html)
        # 記録はあるが再開のコマンドを作れない（ID の形でない・作業ディレクトリがない）ときは、理由の文なしで止める
        html = frag_card(detail(resume_command=None), resume_default="vscode")
        self.assertEqual(html.count("disabled"), 3)
        self.assertNotIn("記録は削除済みのため再開できません", html)
        self.assertIn('data-action="resume" data-how="terminal" disabled>', html)
        # 記録が消えていれば、コマンドが残っていても止める
        html = frag_card(detail(transcript_exists=False, resume_command="cd /w && claude --resume " + S1),
                         resume_default="vscode")
        self.assertEqual(html.count("disabled"), 3)
        self.assertIn("記録は削除済みのため再開できません", html)
        self.assertIn("記録なし", html)

    def test_card_sections(self):
        d = detail(
            branch="HEAD", started_at=t(0), ended_at=t(3 * DAYS), n_prompts=27, n_agents=6, n_children=31,
            children_total=31, recap="要約の\n本文", recap_at=t(50), prompts_after_recap=0, live=True,
            docs=[DocLinkView(5, "/w/docs/h.md", "docs/h.md", "handoff", "created", at(10), 9 * 1024),
                  DocLinkView(None, "/gone/b.md", "~/b.md", "other", "read", None, None)],
            docs_total=7,
            relations=[RelationView("continued", "confirmed", "前の会話", S2, "前の仕事", "dev/w", t(-4 * DAYS),
                                    t(-2 * DAYS), False, evidence="記録の continued-in", peer_place_label="w"),
                       RelationView("reference", "weak", "参照した会話（推定）", S3, "別の仕事", "dev/other",
                                    t(-DAYS), t(-DAYS), True, evidence="", peer_place_label="other")])
        html = frag_card(d, resume_default="vscode")
        self.assertEqual(texts(html, "meta"), [SEP.join(["w", fmt_local(t(0)) + " 〜 " + fmt_local(t(3 * DAYS)),
                                                         "入力27回", "サブエージェント6", "子31"])])
        self.assertIn('<div class="meta"><span title="dev/w">w</span> ・ ', html)
        self.assertNotIn("HEAD", html)
        self.assertIn("実行中", html)
        self.assertIn("直近の要約（%s に記録）" % fmt_local(t(50)), html)
        self.assertIn("要約の<br>本文", html)
        self.assertNotIn("この要約の後の入力", html)
        self.assertNotIn("最後の応答", html)
        self.assertIn("資料 7件", html)
        self.assertIn('<button class="more" data-action="docs" data-place="4">すべて</button>', html)
        self.assertEqual(Markup(html).values("data-doc"), ["5"])
        self.assertEqual(texts(html, "doc"), ["引き継ぎdocs/h.md9KB ・ " + fmt_local(t(10)), "その他~/b.md"])
        links = texts(html, "link")
        self.assertEqual(links, ["前の会話記録の continued-in前の仕事（w ・ %s〜%s）" % (day(t(-4 * DAYS)),
                                                                              day(t(-2 * DAYS))),
                                 "参照した会話（推定）別の仕事（other ・ %s）別の場所へ移った" % day(t(-DAYS))])
        # 根拠は種類のすぐ後に小さな字で（根拠がなければ出さない）。相手の場所は短い名前で、表示名は title に
        self.assertIn('<span class="rel">前の会話</span><span class="ev">記録の continued-in</span><span class="pt">'
                      '前の仕事</span><span class="tiny">（<span title="dev/w">w</span> ・ ', html)
        self.assertIn('<span class="rel">参照した会話（推定）</span><span class="pt">別の仕事</span>', html)
        self.assertEqual(html.count('class="ev"'), 1)
        self.assertIn('<div class="link" data-session="%s">' % S2, html)
        self.assertIn('<div class="link" data-session="%s">' % S3, html)
        self.assertIn('<div class="card" data-session="%s" data-turns="" data-index="0">' % S1, html)
        self.assertIn('<button class="btn" data-action="conversation" data-session="%s" data-turn="">会話で開く'
                      '</button>' % S1, html)
        # 題名・場所など・再開・要約・最初と最後の依頼・資料・つながり・会話で開く の順
        parts = ("<h4>", '<div class="meta">', 'data-action="resume"', "直近の要約", "最初の依頼", "最後の依頼",
                 "資料 7件", "つながり", "会話で開く")
        positions = [html.index(p) for p in parts]
        self.assertEqual(positions, sorted(positions))
        # 要約がなければ最後の応答。つながりも資料もなければそう書く
        html = frag_card(detail(branch="feature/x", recap=None, last_reply="応答です。", first_prompt=None),
                         resume_default="vscode")
        self.assertIn("最後の応答", html)
        self.assertIn("応答です。", html)
        self.assertNotIn("直近の要約", html)
        self.assertIn("feature/x", html)
        self.assertIn('<div class="kv"><span class="k">最初の依頼</span><span class="v none">なし</span></div>', html)
        self.assertIn("資料 0件", html)
        self.assertIn('<div class="empty">なし</div>', html)

    def test_card_search_state(self):
        long_text = "あ" * 400 + "引き継ぎ" + "い" * 400
        excerpt = [ConvItem("user", t(0), "引き継ぎ資料を\n作って", 3, []),
                   ConvItem("assistant", t(1), long_text, 3, []),
                   ConvItem("tools", t(1), "", 3, ["Write a.md", "Bash"])]
        html = frag_card(detail(), resume_default="vscode", hit_turns=[5, 3, 1], hit_index=1, excerpt=excerpt,
                         terms=["引き継ぎ"])
        self.assertIn('<div class="card" data-session="%s" data-turns="5,3,1" data-index="1">' % S1, html)
        self.assertIn("一致した箇所（2/3）", html)
        self.assertNotIn("disabled", html)
        # ▲▼ には読み上げ用の名前を付ける
        self.assertIn('<button class="btn nav" data-action="hit" data-dir="prev" aria-label="前の一致">▲</button>', html)
        self.assertIn('<button class="btn nav" data-action="hit" data-dir="next" aria-label="次の一致">▼</button>', html)
        self.assertEqual(texts(html, "who"), ["あなた ・ " + fmt_local(t(0)), "Claude ・ " + fmt_local(t(1))])
        self.assertIn("<mark>引き継ぎ</mark>資料を 作って", html)
        self.assertIn("…" + "あ" * 148 + "<mark>引き継ぎ</mark>" + "い" * 148 + "…", html)
        self.assertNotIn("ツール", html)  # 語を含まないツールは出さない
        # 会話で開くは、いま見ている一致のターンで開く
        opens = re.findall(r'data-action="conversation" data-session="[^"]*" data-turn="([^"]*)"', html)
        self.assertEqual(opens, ["3", "3"])
        # 一致の箇所は再開のボタンの後、要約の前
        self.assertLess(html.index('data-action="copy"'), html.index("一致した箇所"))
        self.assertLess(html.index("一致した箇所"), html.index("最後の応答"))
        # 端では ▲ か ▼ を止める。範囲の外の位置は端にする
        first = frag_card(detail(), resume_default="vscode", hit_turns=[5, 3, 1], excerpt=excerpt, terms=["引き継ぎ"])
        self.assertIn("一致した箇所（1/3）", first)
        self.assertIn('data-dir="prev" aria-label="前の一致" disabled>▲', first)
        self.assertEqual(first.count("disabled"), 1)
        self.assertEqual(re.findall(r'data-turn="([^"]*)"', first), ["5", "5"])
        last = frag_card(detail(), resume_default="vscode", hit_turns=[5, 3, 1], hit_index=9, terms=["引き継ぎ"])
        self.assertIn("一致した箇所（3/3）", last)
        self.assertIn('data-dir="next" aria-label="次の一致" disabled>▼', last)
        self.assertIn('data-index="2"', last)
        self.assertNotIn('class="excerpt"', last)
        one = frag_card(detail(), resume_default="vscode", hit_turns=[4], excerpt=[], terms=[])
        self.assertIn("一致した箇所（1/1）", one)
        self.assertEqual(one.count("disabled"), 2)

    def excerpt_of(self, items, terms=("引き継ぎ",)):
        """一致した箇所の抜粋の、見出し（who）・発言（x）・ツールの行（x tool）の文字。"""
        html = frag_card(detail(), resume_default="vscode", hit_turns=[3], excerpt=items, terms=list(terms))
        box = re.search(r'<div class="excerpt">(.*?)</div><div class="go">', html).group(1)
        return texts(box, "who"), texts(box, "x"), texts(box, "x tool"), box

    def test_excerpt_of_a_long_turn(self):
        # 長いターン：本人の入力1つと応答30。語を含む応答（7番目と25番目）だけを出す
        items = [ConvItem("user", t(0), "い" * 500, 3, [])]
        for n in range(1, 31):
            items.append(ConvItem("assistant", t(n), "応答%d" % n + ("：引き継ぎ資料を更新" if n in (7, 25) else ""), 3, []))
        items.insert(5, ConvItem("tools", t(4.5), "", 3, ["Read a.md", "Bash"]))
        who, said, tools, box = self.excerpt_of(items)
        self.assertEqual(who, ["あなた ・ " + fmt_local(t(0)), "Claude ・ " + fmt_local(t(7)),
                               "Claude ・ " + fmt_local(t(25))])
        # 本人の入力は、語がなければ先頭から切る
        self.assertEqual(said, ["い" * 300 + "…", "応答7：引き継ぎ資料を更新", "応答25：引き継ぎ資料を更新"])
        self.assertEqual(tools, [])
        self.assertIn("応答7：<mark>引き継ぎ</mark>資料を更新", box)
        # 語を含む応答は3つまで（ターンの中の順）
        items = [ConvItem("user", t(0), "引き継ぎの話", 3, [])]
        items += [ConvItem("assistant", t(n), "引き継ぎ%d" % n, 3, []) for n in range(1, 6)]
        who, said, tools, _ = self.excerpt_of(items)
        self.assertEqual(said, ["引き継ぎの話", "引き継ぎ1", "引き継ぎ2", "引き継ぎ3"])
        self.assertEqual(len(who), 4)

    def test_excerpt_of_a_tool_only_match(self):
        # 語がツールの要約にだけある：一致する要約を1行ずつ（5つまで）、「ツール」の見出しの下に出す
        items = [ConvItem("user", t(0), "資料を直して", 3, []), ConvItem("assistant", t(1), "直します", 3, []),
                 ConvItem("tools", t(2), "", 3, ["Read a.md", "Write 引き継ぎ.md", "Bash（検査）"]),
                 ConvItem("assistant", t(3), "直しました", 3, []),
                 ConvItem("tools", t(4), "", 3, ["Edit 引き継ぎ.md"] + ["Write 引き継ぎ%d.md" % n for n in range(6)])]
        who, said, tools, box = self.excerpt_of(items)
        self.assertEqual(who, ["あなた ・ " + fmt_local(t(0)), "ツール"])
        self.assertEqual(said, ["資料を直して"])
        self.assertEqual(tools, ["Write 引き継ぎ.md", "Edit 引き継ぎ.md", "Write 引き継ぎ0.md", "Write 引き継ぎ1.md",
                                 "Write 引き継ぎ2.md"])
        self.assertIn('<div class="x tool">Write <mark>引き継ぎ</mark>.md</div>', box)
        self.assertNotIn("直します", box)
        self.assertNotIn("Read a.md", box)
        # 応答にもツールにも語があれば、応答の後にツール
        items[3] = ConvItem("assistant", t(3), "引き継ぎを直しました", 3, [])
        who, said, tools, _ = self.excerpt_of(items)
        self.assertEqual(who, ["あなた ・ " + fmt_local(t(0)), "Claude ・ " + fmt_local(t(3)), "ツール"])
        self.assertEqual(len(tools), 5)

    def test_excerpt_falls_back_to_the_first_reply(self):
        # 語が本人の入力にだけある：最初の応答を（先頭から切って）出す
        items = [ConvItem("user", t(0), "引き継ぎ資料を作って", 3, []), ConvItem("tools", t(1), "", 3, ["Bash"]),
                 ConvItem("assistant", t(1), "あ" * 400, 3, []), ConvItem("assistant", t(2), "二つ目の応答", 3, [])]
        who, said, tools, box = self.excerpt_of(items)
        self.assertEqual(who, ["あなた ・ " + fmt_local(t(0)), "Claude ・ " + fmt_local(t(1))])
        self.assertEqual(said, ["引き継ぎ資料を作って", "あ" * 300 + "…"])
        self.assertEqual(tools, [])
        self.assertNotIn("二つ目の応答", box)
        # 応答のないターンは本人の入力だけ。英字の語は大文字小文字を区別せず、空白をまとめて当てる
        who, said, tools, _ = self.excerpt_of([ConvItem("user", t(0), "fix the\nHandoff doc", 3, []),
                                               ConvItem("tools", t(1), "", 3, ["Write handoff.md"])],
                                              terms=["the handoff"])
        self.assertEqual((said, tools), (["fix the Handoff doc"], []))
        _, _, _, box = self.excerpt_of([ConvItem("user", t(0), "fix the\nHandoff doc", 3, [])], terms=["the handoff"])
        self.assertIn("fix <mark>the Handoff</mark> doc", box)
        # 応答の選び方も、空白をまとめてから当てる
        who, said, _, _ = self.excerpt_of([ConvItem("user", t(0), "x", 3, []), ConvItem("assistant", t(1), "最初", 3, []),
                                           ConvItem("assistant", t(2), "wrote the\n  HANDOFF", 3, [])],
                                          terms=["the handoff"])
        self.assertEqual(said, ["x", "wrote the HANDOFF"])

    # 一覧

    def test_session_list_rows(self):
        rows = [row(id=S1, title="作業の続き", place_display="dev/w", ended_at=t(10), n_prompts=22, n_children=16,
                    prs=["https://github.com/o/r/pull/8", "https://github.com/o/r/pull/100",
                         "https://github.com/o/r/pull/7", "https://github.com/o/other/pull/8", "https://x/other"]),
                row(id=S2, title="質問", place_display="", place_label="", ended_at=None, n_prompts=1234,
                    summary="この3つは公開情報？", summary_is_recap=False),
                row(id=S3, kind="headless", title="課題", n_prompts=0, summary="課題をやって", summary_is_recap=False),
                row(id=S9, summary="")]
        html = frag_session_list(rows, selected=S2)
        # PR は URL の /pull/<n> の番号を、数の順に重ねずに。場所は短い名前で、表示名は title に
        self.assertEqual(texts(html, "m"), [SEP.join(["w", fmt_local(t(10)), "入力22回", "子16", "PR #7 #8 #100"]),
                                            "入力1,234回",
                                            SEP.join(["w", fmt_local(t(10)), "入力0回"]),
                                            SEP.join(["w", fmt_local(t(10)), "入力3回"])])
        self.assertEqual(html.count('<div class="m"><span title="dev/w">w</span> ・ '), 3)
        self.assertIn('<button class="kids" data-action="children" data-session="%s">子16</button>' % S1, html)
        self.assertIn('<div class="children" id="children-%s"></div>' % S1, html)
        self.assertEqual(html.count('class="children"'), 1)
        self.assertEqual(texts(html, "r"), ["要約です。", "課題をやって"])
        self.assertEqual(texts(html, "r none"), ["要約なし ― 最後の依頼：この3つは公開情報？"])
        self.assertIn('<div class="row on" data-session="%s">' % S2, html)
        self.assertEqual(html.count('class="row on"'), 1)
        self.assertIn('<span class="badge headless">ヘッドレス</span>課題', html)
        self.assertEqual(frag_session_list([], selected=None), '<div class="empty">セッションがありません</div>')

    def test_list_notice_when_rows_were_cut(self):
        # 一覧を上限で切ったときは、最後に1行で、条件に合う数と絞り方を出す（まだ絞れるものだけを挙げる）
        rows = [row(id=sid(100 + n)) for n in range(3)]
        shown = "新しい順に3件まで表示しています"
        html = frag_session_list(rows, selected=None, total=392, days=30, headless=True)
        self.assertEqual(texts(html, "listnote"), [shown + "（全392件）。期間を短くするか、場所を選ぶか、"
                                                   "「ヘッドレスも並べる」を外すと絞れます"])
        self.assertTrue(html.endswith('</div><div class="listnote">%s（全392件）。期間を短くするか、場所を選ぶか、'
                                      '「ヘッドレスも並べる」を外すと絞れます</div>' % shown), html)
        self.assertEqual(html.count('class="row'), 3)
        cases = ((dict(total=1286, place_id=4, days=7, headless=True), "（全1,286件）。「ヘッドレスも並べる」を外すと絞れます"),
                 (dict(total=250, days=None), "（全250件）。期間を短くするか、場所を選ぶと絞れます"),
                 (dict(total=250, place_id=4, days=90), "（全250件）。期間を短くすると絞れます"),
                 (dict(total=250, place_id=4, days=7), "（全250件）"))
        for kw, text in cases:
            with self.subTest(**kw):
                self.assertEqual(texts(frag_session_list(rows, selected=None, **kw), "listnote"), [shown + text])
        # 切っていなければ出さない
        for kw in (dict(), dict(total=3, headless=True), dict(total=2)):
            with self.subTest(**kw):
                self.assertNotIn("listnote", frag_session_list(rows, selected=None, **kw))

    def test_places(self):
        places = [PlaceItem(3, "/Volumes/x/dev/aws", 10, 1286, 5, t(9), "aws"),
                  PlaceItem(5, "/Volumes/x/dev/weather", 6, 0, 0, t(8), "weather"),
                  PlaceItem(7, "/Volumes/x/dev/memo", 47, 0, 2, t(7), "memo")]
        # 場所は短い名前で、表示名（パスの全体）は title に。「すべて」には title がない
        pattern = (r'<div class="(place[^"]*)" data-place="([^"]*)"><span(?: title="([^"]*)")?>(.*?)</span>'
                   r'<em>(.*?)</em></div>')
        html = frag_places(places, selected=None)
        self.assertIn('<div class="ph">場所</div>', html)
        self.assertEqual(re.findall(pattern, html), [("place on", "", "", "すべて", "63"),
                                                     ("place", "3", "/Volumes/x/dev/aws", "aws", "10 ＋子1,286"),
                                                     ("place", "5", "/Volumes/x/dev/weather", "weather", "6"),
                                                     ("place", "7", "/Volumes/x/dev/memo", "memo", "47")])
        self.assertEqual(re.findall(pattern, frag_places(places, selected=7))[3][0], "place on")
        # 検索の状態：一致のある場所だけを、一致の数とともに出す（選んでいる場所は一致がなくても出す）
        html = frag_places(places, selected=None, counts={7: 3, 3: 1, 99: 2})
        self.assertIn('<div class="ph">場所（一致がある所）</div>', html)
        self.assertEqual(re.findall(pattern, html), [("place on", "", "", "すべて", "6"),
                                                     ("place", "3", "/Volumes/x/dev/aws", "aws", "1"),
                                                     ("place", "7", "/Volumes/x/dev/memo", "memo", "3")])
        html = frag_places(places, selected=5, counts={7: 3})
        self.assertEqual(re.findall(pattern, html), [("place", "", "", "すべて", "3"),
                                                     ("place on", "5", "/Volumes/x/dev/weather", "weather", "0"),
                                                     ("place", "7", "/Volumes/x/dev/memo", "memo", "3")])

    def test_children_and_orphans(self):
        kids = [ChildRow(S2, t(5), "レビューの残りを直す"), ChildRow(S3, t(4), "知見をまとめる")]
        html = frag_children(kids, session_id=S1, offset=0, has_more=True)
        self.assertEqual(texts(html, "row child"), [fmt_local(t(5)) + " レビューの残りを直す",
                                                    fmt_local(t(4)) + " 知見をまとめる"])
        self.assertEqual(Markup(html).values("data-session"), [S2, S3, S1])
        self.assertIn('<button class="more" data-action="children" data-session="%s" data-offset="2">さらに表示'
                      '</button>' % S1, html)
        self.assertIn('data-offset="52"', frag_children(kids, session_id=S1, offset=50, has_more=True))
        self.assertNotIn("さらに表示", frag_children(kids, session_id=S1, offset=0, has_more=False))
        html = frag_orphans(kids, place_id=4, offset=50, has_more=True)
        self.assertIn('<button class="more" data-action="orphans" data-place="4" data-offset="52">さらに表示'
                      '</button>', html)
        self.assertEqual(frag_children([], session_id=S1, offset=0, has_more=False),
                         '<div class="empty">子セッションはありません</div>')
        self.assertEqual(frag_orphans([], place_id=4, offset=0, has_more=False),
                         '<div class="empty">親が分からないヘッドレスはありません</div>')
        self.assertEqual(frag_orphans([], place_id=4, offset=50, has_more=False), "")

    # 検索

    def test_search_says_when_a_section_was_cut(self):
        # 一致は種類ごとに上限（limit）までしか返らない。上限まで返った種類には、その旨を見出しに添える（数は出さない）
        hit = lambda session: SessionHit(session, [1], 1, t(1), Snippet("引き継ぎ", [(0, 4)], "user", t(1)))  # noqa
        history = HistoryHit(1, S7, "/w/old", t(5), Snippet("引き継ぎ", [(0, 4)], "user", t(5)), 3)
        result = SearchResult(sessions=[hit(S1), hit(S2)], headless=[hit(S9)],
                              documents=[DocHit(1, Snippet("引き継ぎ", [(0, 4)], "document", t(0)))],
                              history=[history, history], places=[], elapsed_ms=1.0)
        heads = lambda **kw: texts(frag_search(self.conn, result, terms=["引き継ぎ"], selected=None, **kw),  # noqa
                                   "listhead")
        self.assertEqual(heads(limit=2), ["会話 2セッション（新しい順に2件まで） ・ 資料 1件 ・ "
                                          "入力履歴 2件（新しい順に2件まで） ・ ヘッドレス 1件"])
        self.assertEqual(heads(limit=1), ["会話 2セッション（新しい順に1件まで） ・ 資料 1件（新しい順に1件まで） ・ "
                                          "入力履歴 2件（新しい順に1件まで） ・ ヘッドレス 1件（新しい順に1件まで）"])
        # 会話を一致の多い順に並べたときは、その順に上限まで
        self.assertEqual(heads(limit=2, order="count"), ["会話 2セッション（一致の多い順に2件まで） ・ 資料 1件 ・ "
                                                         "入力履歴 2件（新しい順に2件まで） ・ ヘッドレス 1件"])
        # 上限まで返っていなければ、何も添えない
        self.assertEqual(heads(limit=100), ["会話 2セッション ・ 資料 1件 ・ 入力履歴 2件 ・ ヘッドレス 1件"])
        self.assertEqual(heads(), ["会話 2セッション ・ 資料 1件 ・ 入力履歴 2件 ・ ヘッドレス 1件"])

    def test_docs_search_says_when_hits_were_cut(self):
        # 資料の画面の検索も、一致が上限まで返ったら最後に1行で知らせる（数は出さない）
        items = [doc_item(id=n, name="d%d.md" % n) for n in range(1, 4)]
        html = frag_docs_list(items, selected=None, capped_at=3)
        self.assertEqual(texts(html, "listnote"), ["一致は新しい順に3件まで表示しています"])
        self.assertTrue(html.endswith('<div class="listnote">一致は新しい順に3件まで表示しています</div>'), html)
        self.assertNotIn("listnote", frag_docs_list(items, selected=None))
        # 種類は、上限までの一致を絞って出す（上限より古い一致は種類が合っても出ない）ので、そう書く
        kinded = "新しい順に100件までの一致を、種類で絞って表示しています"
        self.assertEqual(texts(frag_docs_list(items[:1], selected=None, capped_at=100, kind="plan"), "listnote"),
                         [kinded])
        empty = frag_docs_list([], selected=None, capped_at=100, kind="plan")
        self.assertEqual(empty, '<div class="empty">資料がありません</div><div class="listnote">%s</div>' % kinded)

    def test_search_rows(self):
        self.session(S3, "interactive", None, first_prompt="記録の消えた仕事", ended=200, exists=0)
        result = SearchResult(
            sessions=[SessionHit(S1, [5, 3, 1], 3, t(100), Snippet("料理の引き継ぎ資料を作って", [(3, 7)], "user", t(100))),
                      SessionHit(S2, [1], 1, t(10), Snippet("引き継ぎをお願い", [(0, 4)], "user", t(10))),
                      SessionHit(S3, [2], 1, t(200), Snippet("引き継ぎの話", [], "user", t(200))),
                      SessionHit(sid(77), [1], 1, t(1), Snippet("台帳にない", [], "user", t(1)))],
            headless=[SessionHit(S9, [], 1, t(50), Snippet("計画の課題をやる", [(0, 2)], "digest", t(50)))],
            documents=[DocHit(1, Snippet("# 料理の仕事の引き継ぎ", [(8, 12)], "document", t(0))),
                       DocHit(99, Snippet("台帳にない資料", [], "document", t(0)))],
            history=[HistoryHit(1, S7, "/w/old", t(5), Snippet("引き継ぎをお願いします", [(0, 4)], "user", t(5)), 3),
                     HistoryHit(2, None, HOME + "/x/proj", t(6), Snippet("引き継ぎのメモ", [(0, 4)], "user", t(6))),
                     HistoryHit(3, None, "", t(7), Snippet("引き継ぎ", [(0, 4)], "user", t(7)))],
            places=[(1, 2)], elapsed_ms=1.0)
        live = {S1: LiveSession(1, S1, "/w/garden", "busy")}
        html = frag_search(self.conn, result, terms=["引き継ぎ"], selected=S2, live=live, home=HOME)
        self.assertTrue(html.startswith('<div class="listhead">会話 4セッション ・ 資料 2件 ・ 入力履歴 3件 ・ '
                                        'ヘッドレス 1件</div>'), html)
        self.assertIn('<div class="row" data-session="%s" data-turn="5" data-turns="5,3,1">' % S1, html)
        self.assertIn('<div class="row on" data-session="%s" data-turn="1" data-turns="1">' % S2, html)
        self.assertIn('<div class="row" data-session="%s">' % S9, html)
        self.assertNotIn(sid(77), html)
        self.assertEqual(texts(html, "m"), [
            SEP.join(["garden", fmt_local(t(100)), "ほか2か所"]), SEP.join(["plan", fmt_local(t(10))]),
            fmt_local(t(200)),
            SEP.join(["引き継ぎ", "garden", fmt_local(t(0))]),
            SEP.join(["old", fmt_local(t(5))]), SEP.join(["~/x/proj", fmt_local(t(6))]), fmt_local(t(7)),
            SEP.join(["plan", fmt_local(t(50))])])
        # 会話・資料・入力履歴の行の場所は、短い名前に表示名の title を付ける
        self.assertIn('<div class="m"><span title="work/garden">garden</span> ・ ', html)
        self.assertIn('<div class="m">引き継ぎ ・ <span title="work/garden">garden</span> ・ ', html)
        self.assertIn('<div class="m"><span title="work/old">old</span> ・ ', html)
        self.assertEqual(html.count("<span title="), 5)  # 入力履歴の ~/x/proj は場所ではないので付けない
        self.assertIn('<span class="badge live">実行中</span>料理の引き継ぎ資料を作って', html)
        self.assertIn('<span class="badge gone">記録なし</span>記録の消えた仕事', html)
        self.assertIn('<span class="badge headless">ヘッドレス</span>計画の課題をやる', html)
        self.assertIn("料理の<mark>引き継ぎ</mark>資料を作って", html)
        self.assertIn("<mark>引き継ぎ</mark>の話", html)  # 印のない抜粋は検索の語で印を付ける
        self.assertIn('<div class="row" data-doc="1"><div class="t"><span class="ty">MD</span> handoff.md</div>', html)
        self.assertIn("# 料理の仕事の<mark>引き継ぎ</mark>", html)
        self.assertEqual(texts(html, "t gone"), ["入力履歴（記録は削除済み・再開不可）"] * 3)
        for history_row in re.findall(r'<div class="row hist"[^>]*>', html):
            self.assertEqual(history_row, '<div class="row hist">')  # クリックの操作はない
        # 会話・資料・入力履歴・ヘッドレスの順
        positions = [html.index(p) for p in ('data-session="%s"' % S1, 'data-doc="1"', "row hist",
                                             'data-session="%s"' % S9)]
        self.assertEqual(positions, sorted(positions))
        # 何も一致しなければ
        empty = SearchResult([], [], [], [], [], 0.5)
        self.assertEqual(frag_search(self.conn, empty, terms=["x"], selected=None),
                         '<div class="empty">一致するものはありませんでした</div>')

    # 場所の概要

    def test_place_overview(self):
        place = PlaceItem(4, "dev/aws", 10, 1286, 64, t(100), "aws")
        timeline = [TimelineItem(S1, "最初の仕事", t(0), t(2 * DAYS), 46, 16, None, None, None, None),
                    TimelineItem(S2, "続きの仕事", t(3 * DAYS), t(3 * DAYS + 5), 27, 0, "続きの会話へ", None,
                                 "記録の continued-in", None),
                    TimelineItem(S3, "別の場所の仕事", t(5 * DAYS), t(6 * DAYS), 22, 0, "別の場所へ移った",
                                 "/Volumes/x/dev/garden", "記録に ID", "garden")]
        folders = [("docs/handoff", [doc_item(id=11, name="h.md", kind="handoff"),
                                     doc_item(id=12, name="g.md", kind="design")]),
                   (".", [doc_item(id=13, name="README.md", kind="readme")]),
                   ("~/plans", [doc_item(id=14, name="p.md", kind="plan")])]
        html = frag_place(PlaceOverview(place, timeline, folders, 3))
        # 題は場所の短い名前で、表示名は title に
        self.assertIn('<h4><span title="dev/aws">aws</span></h4>', html)
        self.assertEqual(texts(html, "meta"), ["対話10 ・ ヘッドレス1,286 ・ 資料64 ・ 最終 " + fmt_local(t(100))])
        self.assertIn('<button class="btn" data-action="open-place" data-place="4" data-how="vscode">VS Code で開く'
                      '</button>', html)
        self.assertIn('<button class="btn" data-action="open-place" data-place="4" data-how="finder">Finder'
                      '</button>', html)
        # 矢印の言葉の後に、つながりの根拠を小さな字で
        self.assertEqual(texts(html, "via"), ["↓ 続きの会話へ記録の continued-in", "↓ 別の場所へ移った記録に ID"])
        self.assertIn('<div class="via">↓ 続きの会話へ<span class="ev">記録の continued-in</span></div>', html)
        self.assertEqual(texts(html, "tiny"), [
            SEP.join(["%s〜%s" % (day(t(0)), day(t(2 * DAYS))), "入力46回", "子16"]),
            SEP.join([day(t(3 * DAYS)), "入力27回"]),
            SEP.join(["garden", "%s〜%s" % (day(t(5 * DAYS)), day(t(6 * DAYS))), "入力22回"])])
        self.assertIn('<div class="tiny"><span title="/Volumes/x/dev/garden">garden</span> ・ ', html)
        self.assertIn('<div class="tli" data-session="%s">' % S1, html)
        self.assertIn('<div class="tli other" data-session="%s">' % S3, html)
        self.assertLess(html.index("↓ 続きの会話へ"), html.index("続きの仕事"))
        self.assertEqual(texts(html, "folder"), ["docs/handoff/", "./", "~/plans/"])
        self.assertEqual(Markup(html).values("data-doc"), ["11", "12", "13", "14"])
        self.assertIn('<div class="doc" data-doc="11"><span class="kind">引き継ぎ</span><span class="p">h.md</span>'
                      '</div>', html)
        self.assertIn('<button class="more" data-action="docs" data-place="4">すべて</button>', html)
        self.assertIn('<button class="more" data-action="orphans" data-place="4">親が分からないヘッドレス 3件'
                      '</button><div class="children" id="orphans-4"></div>', html)
        # 親が分からないヘッドレスがなければボタンを出さない。最後の時刻がなければ「最終」も出さない
        html = frag_place(PlaceOverview(PlaceItem(5, "dev/docs", 0, 0, 1, None, "docs"), [], [], 0))
        self.assertNotIn("親が分からないヘッドレス", html)
        self.assertNotIn("orphans", html)
        self.assertEqual(texts(html, "meta"), ["対話0 ・ ヘッドレス0 ・ 資料1"])
        self.assertIn('<div class="sec">つながり</div><div class="empty">対話はありません</div>', html)
        self.assertIn('<div class="empty">資料はありません</div>', html)

    # 資料

    def test_docs_filters(self):
        counts = {"design": 3, "plan": 1, "handoff": 2, "other": 5}
        places = [PlaceItem(4, "dev/aws", 1, 0, 6, t(1), "aws"), PlaceItem(5, "dev/none", 2, 0, 0, t(0), "none")]
        kind_rows = r'<div class="(place[^"]*)" data-kind="([^"]*)"><span>(.*?)</span><em>(.*?)</em></div>'
        place_rows = (r'<div class="(place[^"]*)" data-place="([^"]*)"><span(?: title="([^"]*)")?>(.*?)</span>'
                      r'(?:<em>(.*?)</em>)?</div>')
        html = frag_docs_filters(counts, places, kind="plan", place_id=None)
        self.assertEqual(re.findall(kind_rows, html), [("place", "", "すべて", "11")] + [
            ("place on" if k == "plan" else "place", k, KIND_LABELS[k], str(counts.get(k, 0))) for k in KIND_LABELS])
        # 場所は短い名前で、表示名は title に（種類と「すべて」には付けない）
        self.assertEqual(re.findall(place_rows, html), [("place on", "", "", "すべて", ""),
                                                        ("place", "4", "dev/aws", "aws", "6")])
        self.assertLess(html.index('<div class="ph">種類</div>'), html.index('<div class="ph">場所</div>'))
        html = frag_docs_filters({"design": 1, "zzz": 2}, places, kind=None, place_id=5)
        self.assertEqual(re.findall(kind_rows, html)[0], ("place on", "", "すべて", "3"))
        self.assertEqual(re.findall(kind_rows, html)[-1], ("place", "zzz", "zzz", "2"))  # 知らない種類は最後に
        self.assertEqual(re.findall(place_rows, html), [("place", "", "", "すべて", ""),
                                                        ("place", "4", "dev/aws", "aws", "6"),
                                                        ("place on", "5", "dev/none", "none", "0")])

    def test_docs_list(self):
        items = [doc_item(id=1, name="progress.md", kind="progress", folder=".superpowers", mtime=at(10)),
                 doc_item(id=2, name="p.md", kind="plan", place_display="", place_label="", folder="~/plans"),
                 doc_item(id=3, name="index.html", kind="other", folder=".", ext=".html"),
                 doc_item(id=4, name="memo", kind="other", folder="/tmp/x", ext="")]
        html = frag_docs_list(items, selected=2)
        # 場所の中の資料は「場所の短い名前/フォルダー」（表示名は title に）。場所の外はフォルダーのパスのまま
        self.assertEqual(texts(html, "m"), [SEP.join(["台帳・進捗", "w/.superpowers", fmt_local(t(10))]),
                                            SEP.join(["計画", "~/plans", fmt_local(t(10))]),
                                            SEP.join(["その他", "w", fmt_local(t(10))]),
                                            SEP.join(["その他", "/tmp/x", fmt_local(t(10))])])
        self.assertIn('<div class="m">台帳・進捗 ・ <span title="dev/w">w</span>/.superpowers ・ ', html)
        self.assertEqual(html.count("<span title="), 2)
        self.assertIn('<div class="row on" data-doc="2">', html)
        self.assertEqual(html.count('class="row on"'), 1)
        self.assertIn('<div class="t"><span class="ty">HTML</span> index.html</div>', html)
        self.assertIn('<div class="t">memo</div>', html)
        self.assertEqual(frag_docs_list([], selected=None), '<div class="empty">資料がありません</div>')

    def test_docs_list_notice_when_rows_were_cut(self):
        # 資料の一覧も、上限で切ったときは最後に1行で、条件に合う数と絞り方（種類・場所・検索）を出す
        items = [doc_item(id=n, name="d%d.md" % n) for n in range(1, 4)]
        shown = "新しい順に3件まで表示しています"
        html = frag_docs_list(items, selected=2, total=10888)
        note = shown + "（全10,888件）。種類を選ぶか、場所を選ぶか、検索すると絞れます"
        self.assertEqual(texts(html, "listnote"), [note])
        self.assertTrue(html.endswith('</div><div class="listnote">%s</div>' % note), html)
        self.assertEqual(html.count('class="row'), 3)
        self.assertIn('<div class="row on" data-doc="2">', html)
        cases = ((dict(total=500, kind="design"), "（全500件）。場所を選ぶか、検索すると絞れます"),
                 (dict(total=500, place_id=4), "（全500件）。種類を選ぶか、検索すると絞れます"),
                 (dict(total=500, kind="design", place_id=4), "（全500件）。検索すると絞れます"))
        for kw, text in cases:
            with self.subTest(**kw):
                self.assertEqual(texts(frag_docs_list(items, selected=None, **kw), "listnote"), [shown + text])
        # 切っていなければ出さない
        for kw in (dict(), dict(total=3), dict(total=1, kind="design")):
            with self.subTest(**kw):
                self.assertNotIn("listnote", frag_docs_list(items, selected=None, **kw))

    def test_doc_fragment(self):
        links = [("created", row(id=S1, title="作った会話")), ("edited", row(id=S2, title="直した会話")),
                 ("read", row(id=S3, title="読んだ会話", live=True)), ("named", row(id=S7, title="名指しした会話"))]
        md = frag_doc(doc_detail(".md", title="設計", html="<h1>設計</h1>", links=links))
        self.assertIn('<div class="md"><h1>設計</h1></div>', md)
        self.assertIn("<h4>設計</h4>", md)
        self.assertEqual(texts(md, "path"), ["dev/w/docs/a.md"])  # 資料の画面のパスは、場所の表示名のまま
        self.assertNotIn("<span title=", md)
        self.assertEqual(texts(md, "meta"), ["設計書 ・ 9KB ・ %s 更新" % fmt_local(t(10))])
        self.assertIn('<button class="btn pri" data-action="open-doc" data-doc="7" data-how="vscode">VS Code で開く'
                      '</button>', md)
        self.assertIn('<button class="btn" data-action="open-doc" data-doc="7" data-how="finder">Finder</button>', md)
        self.assertNotIn("browser", md)
        self.assertNotIn("<iframe", md)
        self.assertEqual(texts(md, "link"), ["作った：作った会話", "直した：直した会話", "読んだ：読んだ会話実行中",
                                             "名指しした：名指しした会話"])
        self.assertEqual(Markup(md).values("data-session"), [S1, S2, S3, S7])
        page = frag_doc(doc_detail(".html"))
        self.assertIn('<iframe class="docframe" sandbox="allow-scripts allow-popups" src="/doc-raw/7" title="a.html">'
                      '</iframe>', page)
        self.assertIn('<button class="btn" data-action="open-doc" data-doc="7" data-how="browser">ブラウザで開く'
                      '</button>', page)
        self.assertIn("<h4>a.html</h4>", page)  # 題名がなければファイル名
        self.assertIn("data-how=\"browser\"", frag_doc(doc_detail(".HTM")))
        pdf = frag_doc(doc_detail(".pdf"))
        self.assertIn('<div class="empty">この形式は外のアプリで開いてください</div>', pdf)
        self.assertNotIn("<iframe", pdf)
        self.assertNotIn("browser", pdf)
        txt = frag_doc(doc_detail(".txt", html="<pre>a &lt; b</pre>"))
        self.assertIn('<div class="md"><pre>a &lt; b</pre></div>', txt)
        empty = frag_doc(doc_detail(".md", html=""))  # 中身が空の Markdown も、外のアプリへは回さない
        self.assertIn('<div class="md"></div>', empty)
        self.assertNotIn("外のアプリ", empty)

    def test_sizes(self):
        for size, label in ((0, "0B"), (500, "500B"), (9 * 1024, "9KB"), (298 * 1024 + 100, "298KB"),
                            (int(1.2 * 1024 * 1024), "1.2MB"), (1024 * 1024 - 1, "1.0MB"),
                            (3 * 1024 ** 3, "3.0GB")):
            html = frag_doc(DocumentDetail(doc_item(size=size), None, None, []))
            self.assertEqual(texts(html, "meta"), [SEP.join(["設計書", label, fmt_local(t(10)) + " 更新"])], size)

    # 会話

    def test_conversation(self):
        items = [ConvItem("user", t(0), "引き継ぎ資料を\n作って **太字** <b>", 1, []),
                 ConvItem("assistant", t(1), "作ります", 1, []),
                 ConvItem("tools", t(1), "", 1, ["Bash（確認）", "Write docs/h.md", "Bash（検査）", "Write 引き継ぎ.md",
                                                 "Bash"]),
                 ConvItem("tools", t(1), "", 1, ["Read a.md"]),
                 ConvItem("assistant", t(2), "引き継ぎ資料を作りました", 1, []),
                 ConvItem("user", t(3), "次へ", 2, [])]
        w = ConversationWindow(S1, items, 0, 2, 2, "ledger")
        html = frag_conversation(w, title="料理の仕事", terms=["引き継ぎ"], hit_turns=[2, 1])
        self.assertTrue(html.startswith('<div class="conv" data-turns="2,1">'), html)
        self.assertIn('<button class="back" data-action="card" data-session="%s">← 状況カードに戻る</button>' % S1, html)
        self.assertIn("<h4>料理の仕事 ・ 会話</h4>", html)
        self.assertIn('<span class="hits">一致 2か所</span>', html)
        self.assertIn('<button class="btn nav" data-action="hit" data-dir="prev" aria-label="前の一致">▲</button>', html)
        self.assertIn('<button class="btn nav" data-action="hit" data-dir="next" aria-label="次の一致">▼</button>', html)
        self.assertIn('<button class="btn" data-action="fold">中央を畳む</button>', html)
        self.assertEqual(texts(html, "who"), ["あなた ・ " + fmt_local(t(0)), "Claude ・ " + fmt_local(t(1)),
                                              "Claude ・ " + fmt_local(t(2)), "あなた ・ " + fmt_local(t(3))])
        self.assertIn('<div class="turn u" id="turn-1">', html)
        self.assertIn('<div class="turn u" id="turn-2">', html)
        self.assertEqual(html.count('id="turn-'), 2)
        # Markdown にせず、文字のまま（改行だけ <br>）
        self.assertIn("<mark>引き継ぎ</mark>資料を<br>作って **太字** &lt;b&gt;", html)
        self.assertIn('<details class="tools" open><summary>ツール5回：Bash（確認） ／ Write docs/h.md ／ Bash（検査）'
                      '</summary>', html)
        self.assertIn("<li>Write <mark>引き継ぎ</mark>.md</li>", html)
        self.assertIn('<details class="tools"><summary>ツール1回：Read a.md</summary><ol><li>Read a.md</li></ol>'
                      '</details>', html)
        # 一致がなければ移動のボタンを出さない
        html = frag_conversation(w, title="料理の仕事", terms=[], hit_turns=[])
        self.assertIn('<div class="conv" data-turns="">', html)
        self.assertNotIn('data-action="hit"', html)
        self.assertNotIn("<mark>", html)
        self.assertNotIn(" open>", html)
        # 本文がなければ
        html = frag_conversation(ConversationWindow(S1, [], 0, 0, 3, "none"), title="x", terms=[], hit_turns=[])
        self.assertIn('<div class="empty">会話の本文は残っていません</div>', html)
        self.assertIn('data-action="card"', html)
        # ヘッドレスの記録（ターンはすべて 0）でも id は重ならない
        w = ConversationWindow(S9, [ConvItem("user", t(0), "課題", 0, []), ConvItem("user", t(1), "続き", 0, [])],
                               0, 0, 0, "transcript")
        self.assertEqual(frag_conversation(w, title="課題", terms=[], hit_turns=[]).count('id="turn-0"'), 1)

    def test_conversation_offers_add_to_task(self):
        # 会話の画面の見出しにも「タスクに足す」を置く。足せるタスクがなければ出さない
        w = ConversationWindow(S1, [ConvItem("user", t(0), "依頼", 1, [])], 0, 1, 1, "ledger")
        html = frag_conversation(w, title="x", terms=[], hit_turns=[], task_choices=[("a", "A の題"), ("b", "<b>")])
        head = html[:html.index('<div class="turn')]
        self.assertIn('<div class="taskadd"><select class="pick-task" aria-label="タスク"><option value="a">A の題</option>'
                      '<option value="b">&lt;b&gt;</option></select><button class="btn" data-action="task-link" '
                      'data-session="%s">タスクに足す</button></div>' % S1, head)
        self.assertNotIn("タスクに足す", frag_conversation(w, title="x", terms=[], hit_turns=[]))


    def test_conversation_reads_more_turns(self):
        # 長い会話は一致の周りから読み、前後に残りがあれば読み足すボタンを出す（その向きに残りが
        # あるときだけ）。ボタンは読み足す分と残りの数を書き、読み足す所の端のターンを data-turn に持つ
        def turn(n):
            return [ConvItem("user", t(n), "質問%d" % n, n, []), ConvItem("assistant", t(n), "答え%d" % n, n, [])]
        items = [item for n in range(40, 61) for item in turn(n)]
        w = ConversationWindow(S1, items, 40, 60, 60, "ledger", before=39, after=0)
        html = frag_conversation(w, title="長い仕事", terms=[], hit_turns=[])
        before = ('<button class="conv-more" data-action="read-more" data-session="%s" data-dir="before" '
                  'data-turn="40">前の 20 ターンを読む（残り 39）</button>' % S1)
        self.assertIn(before, html)
        self.assertLess(html.index("</div></div>"), html.index(before))  # 見出しの後、最初の発言の前
        self.assertLess(html.index(before), html.index('id="turn-40"'))
        self.assertNotIn('data-dir="after"', html)
        w = ConversationWindow(S1, [item for n in range(1, 26) for item in turn(n)], 0, 25, 60, "ledger",
                               before=0, after=35)
        html = frag_conversation(w, title="長い仕事", terms=["質問"], hit_turns=[5])
        after = ('<button class="conv-more" data-action="read-more" data-session="%s" data-dir="after" '
                 'data-turn="25">後の 20 ターンを読む（残り 35）</button>' % S1)
        self.assertTrue(html.endswith(after + "</div>"), html[-300:])
        self.assertNotIn('data-dir="before"', html)
        self.assertSafe(html)
        # 残りが単位より少なければ、残りの数だけ読む
        w = ConversationWindow(S1, turn(20), 20, 20, 21, "ledger", before=7, after=1)
        html = frag_conversation(w, title="x", terms=[], hit_turns=[])
        self.assertIn(">前の 7 ターンを読む（残り 7）</button>", html)
        self.assertIn(">後の 1 ターンを読む（残り 1）</button>", html)
        # ヘッドレスの記録は項目の番号で読み足す（前は先頭から読んでいるので出ない）
        w = ConversationWindow(S9, [ConvItem("user", t(0), "課題", 0, [])], 0, 0, 0, "transcript", before=0,
                               after=251, offset=0)
        html = frag_conversation(w, title="課題", terms=[], hit_turns=[])
        self.assertIn('<button class="conv-more" data-action="read-more" data-session="%s" data-dir="after" '
                      'data-offset="1">後の 200 件を読む（残り 251）</button>' % S9, html)
        # 本文がなければ、ボタンは出さない
        html = frag_conversation(ConversationWindow(S1, [], 0, 0, 3, "none"), title="x", terms=[], hit_turns=[])
        self.assertNotIn("read-more", html)

    def test_conversation_part(self):
        # 読み足した分の断片：前に足す分はボタン（残りがあれば）が先、後ろに足す分は後。見出しは付けない
        items = [ConvItem("user", t(n), "質問%d の引き継ぎ" % n, n, []) for n in range(20, 40)]
        w = ConversationWindow(S1, items, 20, 39, 60, "ledger", before=19, after=21)
        html = frag_conversation_part(w, terms=["引き継ぎ"], side="before")
        self.assertTrue(html.startswith('<button class="conv-more" data-action="read-more" data-session="%s" '
                                        'data-dir="before" data-turn="20">前の 19 ターンを読む（残り 19）</button>'
                                        '<div class="turn u" id="turn-20">' % S1), html[:300])
        self.assertNotIn("conv", html.replace("conv-more", ""))  # 会話の枠と見出しは付けない
        self.assertNotIn('data-dir="after"', html)
        self.assertEqual(html.count("<mark>引き継ぎ</mark>"), 20)
        self.assertSafe(html)
        html = frag_conversation_part(w, terms=[], side="after")
        self.assertTrue(html.startswith('<div class="turn u" id="turn-20">'), html[:100])
        self.assertTrue(html.endswith('data-dir="after" data-turn="39">後の 20 ターンを読む（残り 21）</button>'))
        self.assertNotIn('data-dir="before"', html)
        done = ConversationWindow(S1, items[:1], 0, 20, 20, "ledger", before=0, after=0)
        self.assertNotIn("read-more", frag_conversation_part(done, terms=[], side="before"))
        self.assertEqual(frag_conversation_part(ConversationWindow(S1, [], 0, -1, 20, "ledger"), terms=[],
                                                side="before"), "")
        # ヘッドレスの記録の続き：ターンはすべて 0 なので、続きの断片には id を付けない（ページの中で重なるため）
        w = ConversationWindow(S9, [ConvItem("user", t(0), "続き", 0, []), ConvItem("assistant", t(1), "はい", 0, [])],
                               0, 0, 0, "transcript", before=200, after=5, offset=200)
        html = frag_conversation_part(w, terms=[], side="after")
        self.assertNotIn("id=", html)
        self.assertTrue(html.endswith('data-dir="after" data-offset="202">後の 5 件を読む（残り 5）</button>'), html)

    def test_conversation_part_escapes_hostile_text(self):
        w = ConversationWindow(EVIL_ID, [ConvItem("user", t(0), EVIL, 3, []), ConvItem("tools", t(0), "", 3, [EVIL])],
                               3, 3, 9, "ledger", before=2, after=6)
        for side in ("before", "after"):
            m = self.assertSafe(frag_conversation_part(w, terms=[], side=side))
            self.assertIn(EVIL, m.text)
            self.assertEqual(m.values("data-session"), [EVIL_ID])


class FromViewsTest(SafeMarkup, unittest.TestCase):
    """記録から更新で作った台帳の views の結果を、そのまま断片にする（views と断片のつなぎ目）。
    S0 から分岐した S1（要約の後に入力1回、Write の資料1件、子のヘッドレス S9）。"""

    S0, S1, S9 = sid(40), sid(41), sid(49)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.conn = make_ledger(root / "tsuzuki")
        self.addCleanup(self.conn.close)
        fake = FakeClaude(root)
        s0, s1, s9, cwd = self.S0, self.S1, self.S9, "/w"
        fake.write(cwd, s0, [user("元の会話", t(-30), s0, cwd), assistant(t(-29), s0, cwd, text="はい")])
        fake.write(cwd, s1, [
            user("最初の依頼 <b>", t(0), s1, cwd, forked_from=s0),
            assistant(t(1), s1, cwd, text="了解しました", tools=[("Write", {"file_path": cwd + "/docs/a.md"})]),
            tool_result('{"session_id": "%s"}' % s9, t(1.5), s1, cwd),
            recap("資料を作りました。", t(2), s1, cwd),
            user("2回目の依頼", t(3), s1, cwd),
        ])
        fake.write(cwd, s9, [user("子の課題", t(1.2), s9, cwd, human=False, entrypoint="sdk-cli")])
        update(self.conn, Config(), claude_dir=fake.dir, ledger_dir=root / "tsuzuki", home=HOME)

    def test_fragments_from_views(self):
        conn, s1 = self.conn, self.S1
        d = views.session_detail(conn, s1, live={}, home=HOME)
        card = frag_card(d, resume_default="vscode", hit_turns=[1], excerpt=views.turn_excerpt(conn, s1, 1),
                         terms=["依頼"])
        # 一致は本人の入力にだけあるので、抜粋は入力と最初の応答（語を含まないツールは出さない）
        for part in ("この要約の後の入力 1 回", "分岐元", "元の会話", "最初の<mark>依頼</mark> &lt;b&gt;", "了解しました",
                     "資料 1件", "docs/a.md", "一致した箇所（1/1）", "子1",
                     'data-copy="cd /w &amp;&amp; claude --resume %s"' % s1,
                     # 場所の短い名前と表示名の title、つながりの根拠（更新が残したもの）
                     '<div class="meta"><span title="/w">/w</span> ・ ', '<span class="ev">記録の forkedFrom</span>'):
            self.assertIn(part, card)
        self.assertNotIn("ツール", card)
        self.assertEqual(card.count("disabled"), 2)  # 一致が1つなので ▲ と ▼ だけ
        place_id = d.place_id
        fragments = [
            card,
            frag_session_list(views.list_sessions(conn, include_headless=True, live={}), selected=s1),
            frag_children(views.children(conn, s1), session_id=s1, offset=0, has_more=False),
            frag_conversation(views.conversation_window(conn, s1, around_turn=1), title=d.title, terms=["依頼"],
                              hit_turns=[2, 1]),
            frag_conversation(views.conversation_window(conn, self.S9), title="子", terms=[], hit_turns=[]),
            frag_places(views.list_places(conn), selected=place_id),
            frag_place(views.place_overview(conn, place_id, home=HOME)),
            frag_orphans(views.orphan_headless(conn, place_id), place_id=place_id, offset=0, has_more=False),
            frag_search(conn, search(conn, SearchQuery("依頼")), terms=["依頼"], selected=None, home=HOME),
        ]
        for fragment in fragments:
            self.assertSafe(fragment)
        self.assertIn('<div class="children" id="children-%s"></div>' % s1, fragments[1])
        self.assertIn("子の課題", fragments[2])
        self.assertIn('<div class="turn u" id="turn-1">', fragments[3])
        self.assertIn("<summary>ツール1回：Write docs/a.md</summary>", fragments[3])
        self.assertIn("子の課題", fragments[4])
        self.assertIn('data-place="%s"' % place_id, fragments[5])
        self.assertIn('data-place="%s"><span title="/w">/w</span>' % place_id, fragments[5])
        self.assertIn("↓ 分岐<span class=\"ev\">記録の forkedFrom</span>", fragments[6])
        self.assertIn('<h4><span title="/w">/w</span></h4>', fragments[6])
        self.assertIn('<div class="empty">親が分からないヘッドレスはありません</div>', fragments[7])
        self.assertIn("会話 1セッション ・ 資料 0件 ・ 入力履歴 0件", fragments[8])


if __name__ == "__main__":
    unittest.main()
