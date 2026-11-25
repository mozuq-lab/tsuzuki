"""タスクの HTML の断片のテスト。断片は innerHTML でページに入るので、台帳と task.md から
来た文字のエスケープを特に確かめる。"""
import html as htmllib
import re
import time
import unittest
from html.parser import HTMLParser

from tsuzuki.render import fmt_local
from tsuzuki.retention import iso_utc
from tsuzuki.tasks import STAGES, stage_label
from tsuzuki.taskviews import TaskDetail, TaskFileItem, TaskRow, TaskSessionItem
from tsuzuki.ui_tasks import at_reference, frag_new_task, frag_task_card, frag_task_filters, frag_task_list
from tsuzuki.views import SessionRow
from tests.fixtures import sid, t

S1, S2, S3 = sid(1), sid(2), sid(3)
HOME = "/Users/me"
ROOT = "/Users/me/tasks"
TASK = ROOT + "/2026-11-20-a"
# 台帳や task.md から来うる悪意のある文字。タグにも属性にもならず、文字のまま出なければならない
EVIL = '<script>alert(1)</script>"\'><img src=x onerror=alert(1)>&amp;'
EVIL_NAME = '2026-11-20-x" onclick="alert(1)'
EVIL_FILE = '<img src=x onerror=alert(1)>"\'&amp;.md'

ALLOWED_TAGS = {"div", "span", "em", "h4", "button", "br", "p", "ul", "ol", "li", "pre", "code", "blockquote", "hr",
                "table", "thead", "tbody", "tr", "th", "td", "strong", "a", "h1", "h2", "h3", "h5", "h6", "input",
                "form", "label", "select", "option"}
ALLOWED_ATTRS = {"class", "id", "disabled", "title", "href", "rel", "target", "align", "start", "aria-label", "type",
                 "name", "value", "required", "maxlength", "placeholder", "autocomplete", "spellcheck"}


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

    def tags_with(self, name: str, value: str):
        return [(tag, attrs) for tag, attrs in self.tags if attrs.get(name) == value]


def day(iso: str) -> str:
    return fmt_local(iso).split(" ")[0]


def local_day(date: str) -> str:
    """task.md の日付（地方時）の、画面での日付。"""
    return day(iso_utc(time.mktime(time.strptime(date, "%Y-%m-%d"))))


def make_row(**kw) -> TaskRow:
    values = dict(name="2026-11-20-a", title="ログインのセッション切れを延ばす", ticket="#123", stage="実装",
                  stage_label="実装・テスト", stage_from_pr=False, status="active", place_id=4, place_label="web-app",
                  last_at=t(60 * 24 * 21), n_sessions=2, live=False, pr_numbers=[], line2_label="次の一手",
                  line2="結合テストを通す")
    values.update(kw)
    if "stage" in kw and "stage_label" not in kw:
        values["stage_label"] = stage_label(values["stage"])
    return TaskRow(**values)


def session_row(**kw) -> SessionRow:
    values = dict(id=S1, title="結合テストの失敗を直す", kind="interactive", place_id=4, place_display="dev/web-app",
                  ended_at=t(60 * 24 * 21), n_prompts=3, n_children=0, prs=[], summary="", summary_is_recap=False,
                  live=False, transcript_exists=True, place_label="web-app")
    values.update(kw)
    return SessionRow(**values)


def session_item(rule="フォルダーで開始", confidence="confirmed", label="確定", n_children=0, **kw) -> TaskSessionItem:
    return TaskSessionItem(row=session_row(**kw), rule_label=rule, confidence=confidence, confidence_label=label,
                           n_children=n_children)


def file_item(path, *, rel_path=None, kind="other", kind_label="その他", title=None, excerpt="", relation=None,
              doc_id=None, missing=False) -> TaskFileItem:
    return TaskFileItem(path=path, rel_path=rel_path or path, kind=kind, kind_label=kind_label,
                        title=title or path.rsplit("/", 1)[-1], excerpt=excerpt, relation=relation, doc_id=doc_id,
                        missing=missing)


def make_detail(*, stage="実装", stage_from_pr=False, warnings=(), file_path=None, row=None, **kw) -> TaskDetail:
    """2026-11-20-a のカードのデータ。セッションは確定の S1 と、推定（弱）の S2 の2つ。"""
    files = [file_item(TASK + "/task.md", rel_path="task.md", kind="cover", kind_label="表紙",
                       title="ログインのセッション切れを延ばす", doc_id=11),
             file_item(TASK + "/research-expiry.md", rel_path="research-expiry.md", kind="research",
                       kind_label="調査", title="期限はどこで決まるか", excerpt="期限は2か所で決まる", relation="作った",
                       doc_id=12)]
    if file_path is not None:
        files.append(file_item(file_path, rel_path=file_path[len(TASK) + 1:], kind="research", kind_label="調査"))
    values = dict(
        row=row or make_row(stage=stage, stage_from_pr=stage_from_pr),
        path=TASK, task_md=TASK + "/task.md", md_mtime=1795243515.25, md_size=321, task_md_doc_id=11,
        created="2026-11-20", updated_at="2026-11-21T09:40:00Z",
        sections={"目的": "期限を延ばす", "現在地": "単体テストを足した", "次の一手": "- 結合テストを通す",
                  "待っていること": "なし", "決めたこと": "延長は 8 時間"},
        after=None, warnings=list(warnings), ticket_url="https://github.com/o/web-app/issues/123",
        branch="fix/session-timeout", prs=[], folder_files=files, folder_cut=False,
        shared_docs=[file_item(ROOT + "/shared/auth-overview.md", kind="notes", kind_label="メモ",
                               title="認証の全体像", relation="読んだ", doc_id=13)],
        other_docs=[file_item("/Users/me/dev/web-app/docs/api.md", kind="readme", kind_label="説明・規約",
                              title="API 一覧", relation="読んだ", doc_id=14)],
        sessions=[session_item(), session_item(id=S2, title="チケット 123 の古い会話", ended_at=t(10),
                                               rule="チケット番号", confidence="weak", label="推定（弱）")],
        resume_session=S1, repo="/Users/me/dev/web-app", shared_total=1, other_total=1)
    values.update(kw)
    return TaskDetail(**values)


class SafeMarkup:
    def assertSafe(self, fragment: str) -> Markup:
        """知っているタグと属性だけで、インラインのスクリプトも style 属性もイベント属性もないこと。"""
        lowered = fragment.lower()
        self.assertNotIn("<script", lowered)
        self.assertNotIn("<img", lowered)
        self.assertNotIn(" style=", lowered)
        m = Markup(fragment)
        for tag, attrs in m.tags:
            self.assertIn(tag, ALLOWED_TAGS, fragment)
            for name in attrs:
                self.assertTrue(name in ALLOWED_ATTRS or name.startswith("data-"), (tag, name, fragment))
        return m


class UiTasksTest(SafeMarkup, unittest.TestCase):

    def test_escapes_and_attributes(self):
        row = make_row(name="2026-11-20-a<b>", title='題 "x" <y>')
        html = frag_task_list([row], selected="2026-11-20-a<b>", status="open")
        self.assertIn('data-task="2026-11-20-a&lt;b&gt;"', html)
        self.assertIn("題 &quot;x&quot; &lt;y&gt;", html)
        self.assertNotIn(" style=", html)  # CSP で style 属性は止まる

    def test_card_stage_and_notes(self):
        html = frag_task_card(make_detail(stage="PR", stage_from_pr=True, warnings=["task.md に現在地がありません"]),
                              resume_default="vscode")
        self.assertIn('data-stage="PR"', html)
        self.assertRegex(html, r'class="stp cur"[^>]*data-stage="PR"')
        self.assertIn("PR の記録から", html)
        self.assertIn("task.md に現在地がありません", html)
        self.assertIn('data-copy="@/Users/me/tasks/2026-11-20-a/task.md"', html)
        self.assertIn("推定（弱）", html)  # make_detail に推定（弱）のセッションを1つ入れておく
        self.assertNotIn(" style=", html)

    def test_at_reference(self):
        self.assertEqual(at_reference("/Users/me/tasks/a/task.md"), "@/Users/me/tasks/a/task.md")
        self.assertEqual(at_reference("/Users/me/tasks/a/research a b.md"), '@"/Users/me/tasks/a/research a b.md"')
        self.assertEqual(at_reference('/Users/me/tasks/a/research "a b".md'), '@/Users/me/tasks/a/research "a b".md')

    def test_paths_with_spaces_and_quotes_stay_whole(self):
        html = frag_task_card(make_detail(file_path="/Users/me/tasks/2026-11-20-a/research a b.md"),
                              resume_default="vscode")
        self.assertIn('data-copy="@&quot;/Users/me/tasks/2026-11-20-a/research a b.md&quot;"', html)
        html = frag_task_card(make_detail(file_path='/Users/me/tasks/2026-11-20-a/research "a b".md'),
                              resume_default="vscode")
        self.assertIn('data-copy="@/Users/me/tasks/2026-11-20-a/research &quot;a b&quot;.md"', html)

    # @ の形

    def test_at_reference_other_spaces(self):
        self.assertEqual(at_reference("/r/a\tb.md"), '@"/r/a\tb.md"')
        self.assertEqual(at_reference("/r/全角　空白.md"), '@"/r/全角　空白.md"')
        self.assertEqual(at_reference("/r/日本語.md"), "@/r/日本語.md")

    def test_at_reference_lives_in_tasks(self):
        # @ の形は tasks に1つだけ置き、画面・新しい会話・tsuzuki task new が同じものを使う
        from tsuzuki import tasks
        self.assertIs(at_reference, tasks.at_reference)

    # 左：状況・場所・共通の資料

    def test_filters(self):
        counts = {"open": 4, "done": 12, "all": 16}
        places = [(3, "web-app", 2), (5, "billing-api", 1), (None, "場所なし", 1)]
        html = frag_task_filters(counts, places, status="open", place=None, shared_place=7)
        self.assertSafe(html)
        self.assertEqual(re.findall(r'<div class="ph">([^<]*)</div>', html), ["状況", "場所", "共通の資料"])
        self.assertIn('<div class="place on" data-status=""><span>進行中と待ち</span><em>4</em></div>', html)
        self.assertIn('<div class="place" data-status="done"><span>完了</span><em>12</em></div>', html)
        self.assertIn('<div class="place" data-status="all"><span>すべて</span><em>16</em></div>', html)
        self.assertIn('<div class="place on" data-place=""><span>すべて</span><em>4</em></div>', html)
        self.assertIn('<div class="place" data-place="3"><span>web-app</span><em>2</em></div>', html)
        self.assertIn('<div class="place" data-place="0"><span>場所なし</span><em>1</em></div>', html)  # 場所なしは 0
        # 共通の資料は、資料の画面を共通の資料の場所で絞って開く（既存の docs の操作）
        shared = Markup(html).tags_with("data-action", "docs")
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0][1]["data-place"], "7")
        chosen = frag_task_filters(counts, places, status="done", place=3, shared_place=7)
        self.assertIn('<div class="place on" data-status="done">', chosen)
        self.assertIn('<div class="place on" data-place="3">', chosen)
        self.assertIn('<div class="place" data-place="">', chosen)
        self.assertIn('<div class="place on" data-place="0">', frag_task_filters(counts, places, status="all",
                                                                               place=0, shared_place=None))
        none = frag_task_filters(counts, [], status="open", place=None, shared_place=None)
        self.assertEqual(Markup(none).tags_with("data-action", "docs"), [])
        self.assertIn("shared/ はまだありません", none)

    # 中央：一覧

    def test_list_rows(self):
        rows = [make_row(live=True, n_sessions=4, pr_numbers=["134", "135"]),
                make_row(name="2026-11-21-b", title="お知らせ文", ticket=None, stage="調査", n_sessions=0,
                         line2_label="要約", line2="変更点を集めた"),
                make_row(name="2026-11-19-c", title="締め日の扱い", status="waiting", stage="調整",
                         line2_label="待っていること", line2="経理の回答"),
                make_row(name="2026-11-18-d", title="PR の記録から", stage="PR", stage_from_pr=True,
                         ticket="https://github.com/o/r/issues/77, #78")]
        html = frag_task_list(rows, selected="2026-11-21-b", status="open")
        m = self.assertSafe(html)
        head = re.search(r'<div class="listhead"><span>([^<]*)</span>(.*?)</div>', html)
        self.assertEqual(head.group(1), "進行中と待ち 4件 ・ 最近動いた順")
        self.assertEqual(head.group(2), '<button class="btn" data-action="new-task">＋ 新しいタスク</button>')
        self.assertEqual(re.findall(r'<div class="grp">([^<]*)</div>', html), ["進行中 3", "待ち 1"])
        self.assertEqual(m.values("data-task"), ["2026-11-20-a", "2026-11-21-b", "2026-11-18-d", "2026-11-19-c"])
        self.assertIn('<div class="row on" data-task="2026-11-21-b">', html)
        self.assertIn('<div class="row" data-task="2026-11-20-a">', html)
        first = html[html.index('data-task="2026-11-20-a"'):html.index('data-task="2026-11-21-b"')]
        self.assertIn('ログインのセッション切れを延ばす<span class="chip tk">#123</span>'
                      '<span class="chip stage">実装・テスト</span>', first)
        self.assertIn("web-app ・ %s ・ 会話 4 ・ PR #134 #135" % fmt_local(t(60 * 24 * 21)), first)
        self.assertIn('<span class="badge live">実行中</span>', first)
        self.assertIn('<div class="r">次の一手：結合テストを通す</div>', first)
        second = html[html.index('data-task="2026-11-21-b"'):]
        self.assertIn('<span class="chip tk none">チケットなし</span>', second)
        self.assertIn('<div class="r">要約：変更点を集めた</div>', second)
        self.assertNotIn("会話 0", second)
        waiting = html[html.index('data-task="2026-11-19-c"'):]
        self.assertIn('<div class="r"><span class="chip wait">待ち</span>経理の回答</div>', waiting)
        from_pr = html[html.index('data-task="2026-11-18-d"'):html.index('<div class="grp">待ち')]
        self.assertIn('<span class="chip tk">#77 #78</span>', from_pr)
        self.assertIn('<span class="chip stage" title="PR の記録から">PR</span>', from_pr)

    def test_list_groups_by_status(self):
        rows = [make_row(name="a", status="active"), make_row(name="w", status="waiting"),
                make_row(name="d", status="done", stage="完了")]
        html = frag_task_list(rows, selected=None, status="all")
        self.assertIn("<span>すべて 3件 ・ 最近動いた順</span>", html)
        self.assertEqual(re.findall(r'<div class="grp">([^<]*)</div>', html), ["進行中 1", "待ち 1", "完了 1"])
        done = frag_task_list([rows[2]], selected=None, status="done")
        self.assertIn("<span>完了 1件 ・ 最近動いた順</span>", done)
        self.assertNotIn('class="row on"', html)

    def test_empty_list_still_offers_a_new_task(self):
        html = frag_task_list([], selected=None, status="open")
        self.assertIn('data-action="new-task"', html)
        self.assertIn('<div class="empty">タスクがありません</div>', html)
        self.assertNotIn('class="grp"', html)

    # 右：タスクのカード

    def test_card_frame_and_header(self):
        d = make_detail()
        html = frag_task_card(d, resume_default="vscode")
        m = self.assertSafe(html)
        self.assertTrue(html.startswith('<div class="task-card" data-task="2026-11-20-a" data-mtime="1795243515.25" '
                                        'data-size="321">'), html[:200])
        self.assertIn("<h4>ログインのセッション切れを延ばす</h4>", html)
        meta = re.search(r'<div class="meta">(.*?)</div>', html).group(1)
        self.assertEqual(htmllib.unescape(meta), "#123 ・ %s ・ %s〜%s ・ 会話 2" % (
            TASK, local_day("2026-11-20"), day(t(60 * 24 * 21))))
        # 渡した home の下は ~ で出す
        tilde = frag_task_card(d, resume_default="vscode", home=HOME)
        self.assertIn(" ・ ~/tasks/2026-11-20-a ・ ", tilde)
        self.assertEqual(m.tags_with("class", "task-card")[0][1]["data-task"], "2026-11-20-a")

    def test_card_foot_has_the_trash_button(self):
        html = frag_task_card(make_detail(), resume_default="vscode", home=HOME)
        self.assertSafe(html)
        foot = html.rsplit('<div class="foot">', 1)[1]
        button = re.search(r"<button[^>]*>ゴミ箱に入れる</button>", foot).group(0)
        self.assertIn('data-action="task-trash"', button)
        confirm = htmllib.unescape(re.search(r'data-confirm="([^"]*)"', button).group(1))
        self.assertEqual(confirm, "「ログインのセッション切れを延ばす」のフォルダー（~/tasks/2026-11-20-a）を、"
                                  "中のファイルごとゴミ箱に入れます。")
        live = frag_task_card(make_detail(row=make_row(live=True)), resume_default="vscode", home=HOME)
        live_button = re.search(r"<button[^>]*>ゴミ箱に入れる</button>", live.rsplit('<div class="foot">', 1)[1])
        self.assertIn(htmllib.escape(confirm + "このタスクの会話が実行中です。"), live_button.group(0))
        evil = frag_task_card(make_detail(row=make_row(title=EVIL)), resume_default="vscode", home=HOME)
        self.assertSafe(evil)

    def test_mtime_keeps_full_precision(self):
        html = frag_task_card(make_detail(md_mtime=1795243515.1234567), resume_default="vscode")
        mtime = Markup(html).values("data-mtime")[0]
        self.assertEqual(mtime, repr(1795243515.1234567))
        self.assertEqual(float(mtime), 1795243515.1234567)

    def test_card_buttons(self):
        html = frag_task_card(make_detail(), resume_default="vscode")
        self.assertIn('<button class="btn pri" data-action="resume" data-session="%s" data-how="vscode">'
                      '続きから（VS Code）</button>' % S1, html)
        self.assertIn('<button class="btn" data-action="task-conversation" data-how="vscode">'
                      '新しい会話（VS Code）</button>', html)
        self.assertIn('<button class="btn" data-action="copy" data-copy="@%s/task.md">@task.md をコピー</button>'
                      % TASK, html)
        self.assertIn('<button class="btn" data-action="open-doc" data-doc="11" data-how="vscode">task.md を開く'
                      '</button>', html)
        in_vscode = frag_task_card(make_detail(), resume_default="vscode-terminal")
        self.assertIn('data-how="vscode-terminal">続きから（VS Code のターミナル）', in_vscode)
        self.assertIn('data-how="vscode-terminal">新しい会話（VS Code のターミナル）', in_vscode)
        terminal = frag_task_card(make_detail(), resume_default="terminal")
        self.assertIn('data-action="resume" data-session="%s" data-how="terminal">続きから（Terminal）' % S1, terminal)
        self.assertIn('data-action="task-conversation" data-how="terminal">新しい会話（Terminal）', terminal)
        # 既定がコピーなら、続きからは再開のコマンドをコピーする（コマンドはサーバーが渡す）
        command = "cd '/w/my dir' && claude --resume " + S1
        copy = frag_task_card(make_detail(), resume_default="copy", resume_command=command)
        self.assertIn('<button class="btn pri" data-action="copy" data-copy="%s">続きから（コピー）</button>'
                      % htmllib.escape(command), copy)
        self.assertIn('data-action="task-conversation" data-how="copy">新しい会話（コピー）', copy)
        self.assertIn('data-action="copy" data-copy="" disabled>続きから（コピー）',
                      frag_task_card(make_detail(), resume_default="copy"))

    def test_card_buttons_without_a_session_to_resume(self):
        # 続きから開ける会話がなければ止め、@task.md のコピーを目立たせる（新しいタスクのカード）
        html = frag_task_card(make_detail(resume_session=None, sessions=[], task_md_doc_id=None),
                              resume_default="vscode")
        self.assertIn('data-action="resume" data-session="" data-how="vscode" disabled>続きから（VS Code）', html)
        self.assertIn('<button class="btn pri" data-action="copy" data-copy="@%s/task.md">@task.md をコピー'
                      % TASK, html)
        self.assertIn('data-action="open-doc" data-doc="" data-how="vscode" disabled>task.md を開く', html)
        self.assertIn("まだ会話はありません", html)
        # 記録の消えたセッションは再開できない
        gone = make_detail(sessions=[session_item(transcript_exists=False)])
        html = frag_task_card(gone, resume_default="vscode")
        self.assertIn('data-how="vscode" disabled>続きから（VS Code）', html)
        self.assertIn('title="記録は削除済みのため再開できません"', html)

    def test_card_stages_and_waiting(self):
        html = frag_task_card(make_detail(stage="実装"), resume_default="vscode")
        stages = re.findall(r'<button class="(stp[^"]*)" data-action="task-stage" data-stage="([^"]*)">'
                            r'([^<]*)</button>', html)
        self.assertEqual([s[1] for s in stages], list(STAGES))
        self.assertEqual([s[2] for s in stages], [stage_label(s) for s in STAGES])
        self.assertEqual([s[0] for s in stages], ["stp done"] * 3 + ["stp cur"] + ["stp"] * 3)
        self.assertIn('<button class="btn" data-action="task-waiting" data-waiting="true">待ちにする</button>', html)
        self.assertNotIn("PR の記録から", html)
        waiting = frag_task_card(make_detail(row=make_row(status="waiting")), resume_default="vscode")
        self.assertIn('<button class="btn" data-action="task-waiting" data-waiting="false">待ちを解く</button>', waiting)
        done = frag_task_card(make_detail(row=make_row(status="done", stage="完了")), resume_default="vscode")
        self.assertNotIn('data-action="task-waiting"', done)
        self.assertIn('class="stp cur" data-action="task-stage" data-stage="完了"', done)

    def test_card_current_and_next(self):
        html = frag_task_card(make_detail(), resume_default="vscode")
        box = re.search(r'<div class="box">(.*?)</div><div class="kv">', html).group(1)
        self.assertIn('<div class="lab">現在地（task.md ・ %s に更新）</div>' % fmt_local("2026-11-21T09:40:00Z"), box)
        self.assertIn('<div class="md"><p>単体テストを足した</p></div>', box)
        self.assertIn('<div class="lab">次の一手</div><div class="md"><ul><li>結合テストを通す</li></ul></div>', box)
        self.assertNotIn("その後", box)
        after = frag_task_card(make_detail(after=("結合テストが通った", t(60 * 24 * 21))), resume_default="vscode")
        self.assertIn('<div class="after"><div class="lab">その後の会話の要約（%s）</div><div class="body">結合テストが'
                      '通った</div></div>' % fmt_local(t(60 * 24 * 21)), after)
        # 現在地がなければ、最新の要約を出し、そのことを添える
        bare = make_detail(sections={}, after=("最新の要約の文", t(5)), warnings=["task.md に現在地がありません"])
        html = frag_task_card(bare, resume_default="vscode")
        box = re.search(r'<div class="box">(.*?)</div><div class="kv">', html).group(1)
        self.assertIn('<div class="lab">最新の会話の要約（%s）</div><div class="body">最新の要約の文</div>' % fmt_local(t(5)),
                      box)
        self.assertIn('<div class="note">task.md に現在地がありません</div>', box)
        self.assertNotIn("現在地（", box)
        self.assertNotIn('class="notice"', html)  # 枠の中に添えたので、最後の注意には出さない

    def test_card_fields(self):
        html = frag_task_card(make_detail(prs=["https://github.com/o/web-app/pull/134"]), resume_default="vscode")
        self.assertEqual(re.findall(r'<div class="kv"><div class="k">([^<]*)</div>', html),
                         ["待っていること", "決めたこと", "チケット", "ブランチ・PR"])
        for part in ('<div class="k">待っていること</div><div class="v none">なし</div>',
                     '<div class="k">決めたこと</div><div class="v"><div class="md"><p>延長は 8 時間</p></div></div>',
                     '<div class="k">チケット</div><div class="v">#123 <a href="https://github.com/o/web-app/issues/123" '
                     'rel="noopener noreferrer" target="_blank">チケットを開く</a></div>',
                     '<div class="k">ブランチ・PR</div><div class="v">fix/session-timeout ・ <a href="https://github.com/'
                     'o/web-app/pull/134" rel="noopener noreferrer" target="_blank">PR #134</a></div>'):
            self.assertIn(part, html)
        # 目的は出さない
        self.assertNotIn("期限を延ばす", html)
        none = frag_task_card(make_detail(row=make_row(ticket=None), ticket_url=None, branch=None,
                                          sections={"待っていること": "経理の回答\n- 締め日"}), resume_default="vscode")
        self.assertIn('<div class="k">チケット</div><div class="v none">チケットなし</div>', none)
        self.assertIn('<div class="k">ブランチ・PR</div><div class="v none">ブランチなし ・ PR なし</div>', none)
        self.assertIn('<div class="k">待っていること</div><div class="v"><div class="md"><p>経理の回答</p>\n<ul><li>締め日'
                      '</li></ul></div></div>', none)
        self.assertIn('<div class="k">決めたこと</div><div class="v none">なし</div>', none)

    def test_ticket_links_are_http_only(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "vscode://x/y", "data:text/html,x"):
            with self.subTest(url=url):
                html = frag_task_card(make_detail(row=make_row(ticket=url), ticket_url=url, prs=[url]),
                                      resume_default="vscode")
                self.assertSafe(html)
                self.assertNotIn("href", html)
                self.assertIn(htmllib.escape(url), html)  # リンクにはせず、文字として出す
        http = frag_task_card(make_detail(row=make_row(ticket="#9"), ticket_url="http://jira.example/9"),
                              resume_default="vscode")
        self.assertIn('href="http://jira.example/9"', http)

    def test_card_with_only_folder_files_is_not_empty(self):
        # 資料がフォルダーのファイルだけのカードに、「資料はありません」を出さない
        d = make_detail(shared_docs=[], other_docs=[], shared_total=0, other_total=0)
        html = frag_task_card(d, resume_default="vscode")
        self.assertNotIn("資料はありません", html)
        empty = frag_task_card(make_detail(folder_files=[], shared_docs=[], other_docs=[], shared_total=0,
                                           other_total=0), resume_default="vscode")
        self.assertIn("資料はありません", empty)

    def test_card_documents(self):
        missing = file_item("/Users/me/old/gone.md", missing=True, relation="名指し")
        d = make_detail(folder_cut=True, other_docs=make_detail().other_docs + [missing], other_total=2)
        html = frag_task_card(d, resume_default="vscode")
        self.assertIn('<div class="sec">資料 5<button class="more" data-action="copy-picked">選んだ資料の @ をまとめてコピー'
                      '</button></div>', html)
        self.assertEqual(re.findall(r'<div class="fold">([^<]*)</div>', html),
                         ["このタスクのフォルダー", "共通の資料", "ほかの場所の資料"])
        rows = re.findall(r'<div class="doc">(.*?)</div>', html)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[1], (
            '<input type="checkbox" class="pick" data-copy="@{t}/research-expiry.md" '
            'aria-label="選ぶ：research-expiry.md">'
            '<span class="ty">調査</span><span class="p" data-doc="12">期限はどこで決まるか<span class="fn">research-expiry.md'
            '</span><span class="ex">期限は2か所で決まる</span></span><span class="w">作った</span>'
            '<button class="cp" data-action="copy" data-copy="@{t}/research-expiry.md">@ コピー</button>').format(t=TASK))
        self.assertIn('<span class="fn">task.md</span>', rows[0])
        self.assertIn('<span class="fn">shared/auth-overview.md</span>', rows[2])  # 共通の資料は置き場所からのパス
        self.assertIn('<span class="fn">/Users/me/dev/web-app/docs/api.md</span>', rows[3])
        tilde = frag_task_card(d, resume_default="vscode", home=HOME)
        self.assertIn('<span class="fn">~/dev/web-app/docs/api.md</span>', tilde)
        # 見つからない資料は、そう書き、コピーと選ぶ欄を止める
        self.assertIn('<span class="ex missing">見つかりません</span>', rows[4])
        self.assertIn(' disabled>@ コピー</button>', rows[4])
        self.assertRegex(rows[4], r'<input type="checkbox" class="pick"[^>]* disabled>')
        self.assertNotIn("data-doc", rows[4])
        self.assertIn("フォルダーのファイルが多いため、300件まで並べています", html)
        self.assertNotIn("（ほか", html)
        # 共通の資料とほかの場所の資料は上限まで。並べなかった数を出す
        capped = frag_task_card(make_detail(shared_total=1, other_total=60), resume_default="vscode")
        self.assertEqual(re.findall(r'<div class="listnote">([^<]*)</div>', capped),
                         ["新しい順に50件まで並べています（ほか 59件）"])
        self.assertLess(capped.index("ほかの場所の資料"), capped.index("（ほか 59件）"))
        # 名前と題が同じなら、名前を2回出さない
        plain = frag_task_card(make_detail(folder_files=[file_item(TASK + "/shot.png", rel_path="shot.png")]),
                               resume_default="vscode")
        self.assertIn('<span class="p">shot.png</span>', plain)

    def test_documents_heading_counts_all_not_capped(self):
        # 資料の見出しの数は、並べた数（50件まで）でなく全体の数
        many = [file_item(ROOT + "/shared/n%d.md" % i, kind="notes", kind_label="メモ", doc_id=100 + i)
                for i in range(50)]
        d = make_detail(folder_files=[], shared_docs=many, shared_total=70, other_docs=[], other_total=0)
        html = frag_task_card(d, resume_default="vscode")
        self.assertIn('<div class="sec">資料 70', html)
        self.assertIn("（ほか 20件）", html)

    def test_card_sessions(self):
        items = [session_item(live=True, n_children=2),
                 session_item(id=S2, title="古い会話", rule="チケット番号", confidence="weak", label="推定（弱）"),
                 session_item(id=S3, title="親が別のヘッドレス", kind="headless", rule="子の会話から",
                              confidence="strong", label="推定（強）")]
        html = frag_task_card(make_detail(sessions=items), resume_default="vscode")
        self.assertIn('<div class="sec">会話 3</div>', html)
        rows = re.findall(r'<div class="link" data-action="card" data-session="([^"]*)">(.*?)</div>', html)
        self.assertEqual([r[0] for r in rows], [S1, S2, S3])
        self.assertEqual(rows[0][1], (
            '<span class="badge live">実行中</span><span class="pt">結合テストの失敗を直す</span><span class="tiny">%s</span>'
            '<span class="ev">フォルダーで開始 ・ 確定</span><span class="tiny">＋ヘッドレス 2</span>'
            '<button class="more" data-action="task-unlink" data-session="%s">外す</button>') % (
            fmt_local(t(60 * 24 * 21)), S1))
        self.assertIn('<span class="ev">チケット番号 ・ 推定（弱）</span>', rows[1][1])
        self.assertIn('<span class="badge headless">ヘッドレス</span>', rows[2][1])

    def test_card_notes_come_last(self):
        warnings = ["task.md の見出し情報を読めません", "段階の値を読めません: 実装中"]
        html = frag_task_card(make_detail(warnings=warnings), resume_default="vscode")
        notes = re.findall(r'<div class="notice">([^<]*)</div>', html)
        self.assertEqual(notes, warnings)
        self.assertTrue(html.endswith('<div class="notice">段階の値を読めません: 実装中</div></div>'), html[-200:])

    # 新しいタスク

    def test_new_task_form(self):
        html = frag_new_task([("/Users/me/dev/web-app", "dev/web-app"), ("/w/x", "x")])
        m = self.assertSafe(html)
        self.assertEqual(m.tags[0], ("form", {"id": "new-task", "class": "new-task", "autocomplete": "off"}))
        self.assertEqual([a["name"] for tag, a in m.tags if tag in ("input", "select")],
                         ["title", "short_name", "ticket", "repo"])
        self.assertIn("required", m.tags_with("name", "title")[0][1])
        self.assertIn("required", m.tags_with("name", "short_name")[0][1])
        self.assertNotIn("required", m.tags_with("name", "ticket")[0][1])
        self.assertEqual([a.get("value") for tag, a in m.tags if tag == "option"],
                         ["", "/Users/me/dev/web-app", "/w/x"])
        self.assertIn('<option value="/Users/me/dev/web-app" title="/Users/me/dev/web-app">dev/web-app</option>', html)
        self.assertIn('<button type="submit" class="btn pri">作る</button>', html)
        self.assertIn('<option value="">なし</option></select>', frag_new_task([]))

    # エスケープ

    def test_every_fragment_escapes_hostile_text(self):
        row = make_row(name=EVIL_NAME, title=EVIL, ticket=EVIL, place_label=EVIL, line2=EVIL, line2_label=EVIL,
                       pr_numbers=[EVIL], status="waiting")
        evil_file = file_item(TASK + "/" + EVIL_FILE, rel_path=EVIL_FILE, kind=EVIL, kind_label=EVIL, title=EVIL,
                              excerpt=EVIL, relation=EVIL, doc_id=5)
        detail = make_detail(
            row=row, path=ROOT + "/" + EVIL_NAME, task_md=ROOT + "/" + EVIL_NAME + "/task.md", created=EVIL,
            updated_at=EVIL, sections={k: EVIL for k in ("現在地", "次の一手", "待っていること", "決めたこと")},
            after=(EVIL, EVIL), warnings=[EVIL], ticket_url=EVIL, branch=EVIL, prs=[EVIL, "https://x.example/" + EVIL],
            folder_files=[evil_file], shared_docs=[evil_file], other_docs=[evil_file],
            sessions=[session_item(id='x" onclick="alert(1)', title=EVIL, rule=EVIL, label=EVIL, live=True)],
            resume_session='x" onclick="alert(1)')
        fragments = {
            "filters": frag_task_filters({"open": 1, "done": 0, "all": 1}, [(4, EVIL, 1)], status="open", place=4,
                                         shared_place=7),
            "list": frag_task_list([row], selected=EVIL_NAME, status="open"),
            "card": frag_task_card(detail, resume_default="vscode", home=HOME),
            "card-copy": frag_task_card(detail, resume_default="copy", resume_command=EVIL),
            "new": frag_new_task([(EVIL, EVIL)]),
        }
        for name, fragment in fragments.items():
            with self.subTest(fragment=name):
                m = self.assertSafe(fragment)
                self.assertIn(EVIL, m.text)
        self.assertEqual(Markup(fragments["list"]).values("data-task"), [EVIL_NAME])
        self.assertEqual(Markup(fragments["card"]).values("data-task"), [EVIL_NAME])
        self.assertIn(EVIL, Markup(fragments["card-copy"]).values("data-copy"))
        self.assertIn(EVIL, Markup(fragments["new"]).values("value"))
        self.assertNotIn("href", fragments["card"].replace('href="https://x.example/', ""))
        # 悪意のある名前のファイルも、@ の形のまま属性に入り、属性を抜け出さない
        self.assertIn(at_reference(TASK + "/" + EVIL_FILE), Markup(fragments["card"]).values("data-copy"))


if __name__ == "__main__":
    unittest.main()
