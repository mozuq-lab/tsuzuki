#!/usr/bin/env python3
"""画面を試すための偽のデータを作る（本物の記録は使わない）。

    python3 scripts/make_demo.py <出力先>

<出力先> に次を作り、画面を開くコマンドを出す。
- claude/：偽の ~/.claude（tests/fixtures.py で書く）。対話10件、ヘッドレス5件、記録が消えた入力履歴2件、メモリ1件
- home/：サーバーに HOME として渡す偽のホーム。home/.claude は claude/ を指す
  （メモリの資料は ~/.claude/projects/<場所>/memory/ にあることで見分けるため）
- home/tasks/：タスクの置き場所。タスク3つ（進行中・待ち・完了）と shared/。偽の記録のセッションは、
  フォルダーで開始・資料を書いた・task.md を読んだ・元の会話から・同じブランチ・チケット番号の規則で結び付く
- work/：実在する作業ディレクトリ。場所3つ（Git の recipe-site と、Git でない sales-report・reading-notes）と資料
- ledger/config.json：台帳の設定（⌘Enter はコピーにして、アプリを起動しない。tasks_root は home/tasks を指し、
  本物の ~/tasks は読まない）
時刻は今から数日前に置く（一覧の既定の期間 30日に入るように）。
"""
import json
import os
import shlex
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tsuzuki.documents import TMP_PREFIXES  # noqa: E402
from tsuzuki.places import encode_project  # noqa: E402
from tests.fixtures import (  # noqa: E402
    FakeClaude, assistant, continued_in, make_git_repo, pr_link, recap, title, tool_result, user, write_file,
)

MARKER = ".tsuzuki-demo"  # このスクリプトが作った出力先の印（作り直すときに、ほかの物を消さないため）
PORT = 8799
Tool = Tuple[str, dict]


def demo_id(name: str) -> str:
    """毎回同じになるセッション ID（小文字の UUID）。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "https://example.invalid/tsuzuki-demo/" + name))


def at(days_ago: int, hhmm: str) -> datetime:
    """今日から days_ago 日前の、地方時の hh:mm。"""
    hour, minute = (int(x) for x in hhmm.split(":"))
    day = datetime.now().astimezone() - timedelta(days=days_ago)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)


def iso(when: datetime) -> str:
    when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (when.microsecond // 1000)


def local_day(when: datetime) -> str:
    """地方時の日付（タスクのフォルダー名と task.md の created）。"""
    return when.astimezone().strftime("%Y-%m-%d")


def task_md(title: str, *, ticket: Optional[str], stage: str, waiting: bool, repo: str, created: datetime,
            updated: datetime, sections: dict) -> str:
    """task.md。updated は地方時の YYYY-MM-DDTHH:MM。"""
    lines = ["---", "title: " + title] + (['ticket: "%s"' % ticket] if ticket else [])
    lines += ["stage: " + stage, "waiting: " + ("true" if waiting else "false"), "repo: " + repo,
              "created: " + local_day(created), "updated: " + updated.astimezone().strftime("%Y-%m-%dT%H:%M"),
              "---", "# " + title]
    for name in ("目的", "現在地", "次の一手", "待っていること", "決めたこと", "資料"):
        lines += ["", "## " + name] + ([sections[name]] if sections.get(name) else [])
    return "\n".join(lines) + "\n"


class Session:
    """1つのセッションの記録を、時刻を進めながら組み立てる。"""

    def __init__(self, session_id: str, cwd: str, start: datetime, *, branch: str = "main",
                 entrypoint: str = "cli"):
        self.id, self.cwd, self.now = session_id, cwd, start
        self.kw = dict(branch=branch, entrypoint=entrypoint)
        self.records = []  # type: List[dict]

    def tick(self, minutes: float) -> str:
        self.now += timedelta(minutes=minutes)
        return iso(self.now)

    def jump(self, when: datetime) -> None:
        self.now = when

    def title(self, text: str) -> None:
        self.records.append(title("ai", text, self.id))

    def ask(self, text: str, minutes: float = 2, **kw) -> None:
        self.records.append(user(text, self.tick(minutes), self.id, self.cwd, **self.kw, **kw))

    def task(self, text: str, minutes: float = 0.1) -> None:
        """ヘッドレスの依頼文（本人の入力ではない）。"""
        self.records.append(user(text, self.tick(minutes), self.id, self.cwd, human=False, **self.kw))

    def reply(self, text: Optional[str] = None, tools: Sequence[Tool] = (), minutes: float = 1) -> None:
        self.records.append(assistant(self.tick(minutes), self.id, self.cwd, text=text, tools=tools, **self.kw))

    def result(self, text: str, minutes: float = 0.5) -> None:
        self.records.append(tool_result(text, self.tick(minutes), self.id, self.cwd, **self.kw))

    def recap(self, text: str, minutes: float = 2) -> None:
        self.records.append(recap(text, self.tick(minutes), self.id, self.cwd, **self.kw))

    def pr(self, number: int, minutes: float = 0.5) -> None:
        url = "https://github.com/example/recipe-site/pull/%d" % number
        self.records.append(pr_link(self.id, url, self.tick(minutes)))

    def continued_in(self, next_id: str, minutes: float = 0.5) -> None:
        self.records.append(continued_in(self.id, next_id, self.tick(minutes)))


def write_doc(path: str, text: str, when: datetime) -> str:
    """資料を書き、更新時刻を、それを書いたセッションの時刻にする。"""
    write_file(path, text)
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def settle_folder_times(root: str) -> None:
    """フォルダーの更新時刻を、中の最も新しいファイルの時刻にする（タスクの最後の動きはフォルダーの時刻も見るので、
    作ったばかりの今の時刻のままにしない）。中のフォルダーから先に直す。"""
    for current, dirs, files in os.walk(root, topdown=False):
        times = [os.stat(os.path.join(current, name)).st_mtime for name in files + dirs]
        if times:
            os.utime(current, (max(times), max(times)))


def bash(description: str, command: str) -> Tool:
    return ("Bash", {"description": description, "command": command})


def read(path: str) -> Tool:
    return ("Read", {"file_path": path})


def write(path: str) -> Tool:
    with open(path, encoding="utf-8") as f:
        return ("Write", {"file_path": path, "content": f.read()})


def edit(path: str, old: str, new: str) -> Tool:
    return ("Edit", {"file_path": path, "old_string": old, "new_string": new})


DESIGN = """# レシピサイトの設計

料理のレシピを載せるウェブサイトを、データから HTML まで自動で作る仕組みの設計である。読み手は、この仕組みを直す人と次のセッションの Claude。

## 工程の流れ

| 工程 | 役目 | 出すもの |
|---|---|---|
| 一覧 | レシピを新しい順に並べる | `index.html` |
| 検索 | 材料や料理名で絞り込む | `search.json` |
| 詳細 | 材料と手順を1ページにまとめる | `recipe/*.html` |
| 検査 | リンク切れと画像の大きさを確かめる | `report.md` |

## 動かし方

```bash
python3 -m recipesite build --data docs/recipes.json --out out/
python3 -m recipesite check out/
```

- 工程は、前の工程の出力だけを読む。
- **検査**で落ちたら、その工程からやり直す。
- 材料の単位は2通り（グラム・カップ）。レシピごとに単位を付ける。
"""

HANDOFF = """# 検索の仕事の引き継ぎ

次のセッションで、検索の工程から続けるための資料である。

## 今の状態

- 一覧の工程と検査の工程はできている。テストはすべて通る。
- 検索の工程はまだない。材料の単位は2通り（グラム・カップ）にする。

## 次にすること

1. `docs/specs/2026-11-recipe-design.md` の表を読む。
2. 検索の工程の計画を `docs/plans/` に書く。
3. 試しのページで、絞り込み方ごとの結果を見比べられるようにする。
"""

PLAN = """# 検索の改善の計画

## 課題

1. 材料の名前で絞り込む
2. 料理名の読みがなでも探せるようにする
3. 結果を、新しい順か人気の順に並べ替える
4. 検索の結果と一覧の件数をそろえる
5. 画像の大きさを検査の工程で確かめる

## 進め方

課題1〜3は別のセッションに任せ、結果をこの会話で確かめる。課題4と5は試しのページができてから行う。
"""

PROGRESS = """# 進み具合

| 課題 | 状態 |
|---|---|
| 1. 材料で絞り込む | 済み |
| 2. 読みがなで探す | 済み |
| 3. 並べ替え | 済み |
| 4. 一覧との件数 | まだ |
| 5. 画像の大きさの検査 | まだ |
"""

TRIAL_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>検索の試し</title>
<style>
  body { font-family: -apple-system, "Hiragino Sans", sans-serif; margin: 1.5rem; color: #222; background: #fff; }
  svg text { font-size: 13px; fill: #333; }
  .note { color: #666; font-size: 13px; }
</style>
</head>
<body>
<h1>検索の試し</h1>
<p>絞り込み方ごとの結果を比べるページです。帯の長さは、同じ語で探したときの件数です。</p>
<svg width="360" height="110" viewBox="0 0 360 110" role="img" aria-label="絞り込み方ごとの結果の件数">
  <text x="0" y="30">材料</text>
  <rect x="60" y="13" width="220" height="24" rx="4" fill="#5b8def"></rect>
  <text x="290" y="30">12件</text>
  <text x="0" y="78">料理名</text>
  <rect x="60" y="61" width="147" height="24" rx="4" fill="#f2a33a"></rect>
  <text x="217" y="78">8件</text>
</svg>
<ul>
  <li>材料：食材の名前で絞る（件数は多め）</li>
  <li>料理名：読みがなでも探す（件数は少なめ）</li>
</ul>
<p class="note" id="note"></p>
<script>document.getElementById("note").textContent = "（このページのスクリプトは、隔離した枠の中で動いています）";</script>
</body>
</html>
"""

README = """# recipe-site

料理のレシピを載せるウェブサイト。設計は `docs/specs/` に、計画は `docs/plans/` にある。
"""

LEARNINGS = """# 集計の知見

- 日ごとの欠けは定休日だけだった。
- 月の平均は営業した日で割って出す（休みの日を入れると、低く出る）。
- 売上は桁が大きいので、表では千円単位にする。
"""

MEMORY = """# 集計で覚えておくこと

- 月の平均は営業した日で割って出す（本人の指定）。
- 表の売上は千円単位にする。
- わかったことは learnings.md に書き足す。
"""

SALES = """日付,売上,客数
2026-10-01,152000,81
2026-10-02,153400,84
2026-10-03,151100,79
2026-10-06,149800,77
2026-10-07,152600,83
"""

NOTES = """読書メモ（2026年）
- 『庭の歳時記』：読み終えた（10月3日）。
- 『海辺の町の天気』：読み終えた（10月20日）。
- 『やさしい家計簿』：買ったが、読み終えた日は書いていない。
"""

# タスクの置き場所の資料。名前の先頭の語で種類が決まる（research・design・notes。task.md は表紙）

SEARCH_TASK = {
    "目的": "材料や料理名で、レシピを探せる検索の画面を作る。",
    "現在地": "課題1〜3（材料で絞る・読みがなでも探す・並べ替え）は済み、試しのページで結果を見比べられるようになった。"
              "PR #24・#25 を出した。",
    "次の一手": "- 課題4：検索の結果と一覧の件数をそろえる\n- 課題5：画像の大きさを検査の工程で確かめる",
    "待っていること": "なし",
    "決めたこと": "- 材料の単位は2通り（グラム・カップ）にする\n- 並べ替えは、新しい順と人気の順にする",
    "資料": "- [検索の用語](../shared/notes-search-terms.md)\n- ./research-search.md 検索のしかたの候補\n"
            "- ./design-search-screen.md 画面の作り",
}

AVERAGE_TASK = {
    "目的": "日ごとの売上のデータから、月ごとの平均を毎月自動で出す。",
    "現在地": "10月の平均は手で出せた（151,780 円）。臨時の休みの扱いが決まれば、自動にできる。",
    "次の一手": "店長の回答を受けて、臨時で休んだ日を営業日に数えるかどうかを決める。",
    "待っていること": "店長から、臨時で休んだ日を営業日に数えるかどうかの回答（問い合わせは送った）",
    "決めたこと": "- 月の平均は営業した日で割って出す\n- 売上は千円単位にする",
    "資料": "- ./notes-average.md 集計の決まり",
}

SPEC_TASK = {
    "目的": "読書メモのうち、読み終えた日が書かれた本だけを一覧にする。",
    "現在地": "読み終えた日が書かれた本は2冊だった（『庭の歳時記』『海辺の町の天気』）。一覧を渡して終えた。",
    "次の一手": "なし（完了）",
    "待っていること": "なし",
    "決めたこと": "- 読み終えた日が書かれていない本は「未確認」と書く",
    "資料": "- ./research-finished-books.md 調べ方",
}

RESEARCH_SEARCH = """# 検索のしかたの候補

検索の工程で、材料でも読みがなでも探せる方法を比べた。

| 方法 | 材料で絞る | 読みがなで探す | 手間 |
|---|---|---|---|
| レシピごとに検索用の語を持つ | できる | できる | 小さい |
| 本文を毎回探す | できる | できない | 大きい |

レシピごとに検索用の語を持つ方法にする。
"""

DESIGN_SEARCH = """# 検索の画面の作り

- レシピごとの検索用の語（材料・料理名・読みがな）を読み、入力された語で絞り込む。
- 結果は、新しい順か人気の順に並べる。
- 出すものは `search.json` と、材料ごとの件数の一覧。
"""

AVERAGE_NOTES = """# 月ごとの平均の決まり

- 月の平均は営業した日で割って出す（休みの日を入れると、低く出る）。
- 売上は千円単位にする。
- 臨時で休んだ日は、数えずに飛ばす（店長の回答を待っている）。
"""

RESEARCH_SPEC = """# 読み終えた本の調べ方

1. 読書メモと本棚の記録だけを読む。
2. 本ごとに、読み終えた日が書かれている所を引く。
3. 読み終えた日が書かれていない本は「未確認」とする。
"""

SEARCH_TERMS = """# 検索の用語

- 検索用の語：レシピごとに持つ、材料・料理名・読みがなの語。
- 絞り込み：入力した語に合わないレシピを、結果から外すこと。
- 件数の一覧：材料ごとの結果の件数。一覧の画面と数をそろえるのに使う。
"""

WRITING_RULES = """# 資料の書き方の決まり

- 根拠は、読書メモと本棚の記録だけにする。
- 書かれていないことは「未確認」と書く。
- 調べた日付を、資料の最初に書く。
"""


def build(out: str) -> dict:
    """偽の ~/.claude・作業ディレクトリ・台帳の設定を作り、数をまとめて返す。"""
    work = os.path.join(out, "work")
    repo = make_git_repo(work, "recipe-site")
    sales_dir, reading = os.path.join(work, "sales-report"), os.path.join(work, "reading-notes")
    for d in (sales_dir, reading):
        os.makedirs(d)
    fake = FakeClaude(Path(out))
    home = os.path.join(out, "home")  # サーバーに HOME として渡す。~/.claude（home/.claude）は claude/ を指す
    os.makedirs(home)
    os.symlink(os.path.relpath(str(fake.dir), home), os.path.join(home, ".claude"))
    ids = {name: demo_id(name) for name in ("A", "B", "C", "D", "E", "F", "H1", "H2", "H3", "H4", "H5",
                                            "T1", "T3", "G", "K")}
    design = os.path.join(repo, "docs", "specs", "2026-11-recipe-design.md")
    handoff = os.path.join(repo, "docs", "handoff", "recipe-handoff.md")
    plan = os.path.join(repo, "docs", "plans", "search-plan.md")
    progress = os.path.join(repo, ".superpowers", "recipe-site", "progress.md")
    trial = os.path.join(repo, "out", "search-trial", "index.html")
    learnings, sales, notes = (os.path.join(sales_dir, "learnings.md"), os.path.join(sales_dir, "sales.csv"),
                               os.path.join(reading, "notes.txt"))
    memory = os.path.join(home, ".claude", "projects", encode_project(sales_dir), "memory", "MEMORY.md")
    write_doc(os.path.join(repo, "README.md"), README, at(20, "09:00"))
    write_doc(sales, SALES, at(5, "08:00"))
    write_doc(notes, NOTES, at(4, "12:00"))

    # タスクの置き場所（設定 tasks_root）。フォルダー名は「作った日-短い名前」。task.md の更新時刻は、最後に現在地を
    # 書いた時刻にする（資料はそれを書いたセッションの時刻）
    tasks = os.path.join(home, "tasks")
    search = os.path.join(tasks, local_day(at(9, "17:00")) + "-search-page")  # 進行中。PR の記録から「PR」と出る
    average = os.path.join(tasks, local_day(at(4, "10:00")) + "-monthly-average")  # 待ち。チケットなし
    spec = os.path.join(tasks, local_day(at(3, "10:00")) + "-finished-books")  # 完了
    search_md, average_md, spec_md = (os.path.join(d, "task.md") for d in (search, average, spec))
    research_search, design_search = (os.path.join(search, "research-search.md"),
                                      os.path.join(search, "design-search-screen.md"))
    average_notes, research_spec = (os.path.join(average, "notes-average.md"),
                                    os.path.join(spec, "research-finished-books.md"))
    search_terms, writing_rules = (os.path.join(tasks, "shared", "notes-search-terms.md"),
                                   os.path.join(tasks, "shared", "notes-writing-rules.md"))
    write_doc(search_md, task_md("検索の画面を作る", ticket="#18", stage="実装", waiting=False, repo=repo,
                                 created=at(9, "17:00"), updated=at(7, "11:40"), sections=SEARCH_TASK),
              at(7, "11:40"))
    write_doc(average_md, task_md("月ごとの平均を自動で出す", ticket=None, stage="調整", waiting=True,
                                  repo=sales_dir, created=at(4, "10:00"), updated=at(3, "21:45"),
                                  sections=AVERAGE_TASK),
              at(3, "21:45"))
    write_doc(spec_md, task_md("読み終えた本を一覧にする", ticket="#7", stage="完了", waiting=False, repo=reading,
                               created=at(3, "10:00"), updated=at(2, "17:45"), sections=SPEC_TASK),
              at(2, "17:45"))
    write_doc(search_terms, SEARCH_TERMS, at(10, "09:00"))
    write_doc(writing_rules, WRITING_RULES, at(12, "09:00"))

    # T1：タスクのフォルダーで始め、候補を調べて画面の作りと表紙を書く（フォルダーで開始・確定）
    t1 = Session(ids["T1"], search, at(9, "17:00"), branch="")
    t1.title("検索の画面のタスクを作る")
    t1.ask("チケット #18 の検索の画面を、このフォルダーで調べてから始めます。検索のしかたの候補を調べてください。")
    t1.reply("リポジトリの説明を読み、使える検索の方法を確かめます。", [read(os.path.join(repo, "README.md"))])
    write_doc(research_search, RESEARCH_SEARCH, t1.now + timedelta(minutes=8))
    t1.reply(tools=[write(research_search)], minutes=8)
    t1.reply("候補を research-search.md にまとめました。レシピごとに検索用の語を持てば、材料でも読みがなでも探せます。")
    t1.ask("調べた結果から画面の作りを決めて、task.md も書いてください。", minutes=6)
    write_doc(design_search, DESIGN_SEARCH, t1.now + timedelta(minutes=6))
    t1.reply(tools=[write(design_search), write(search_md)], minutes=6)
    t1.reply("画面の作りを design-search-screen.md に、目的と次の一手を task.md に書きました。")
    t1.recap("検索の画面の作り方を調べて決め、タスクの表紙を作りました。次は、リポジトリで計画を立てて実装します。")

    # A：調べて設計書を書き、会話の上限で続きの会話（B）へ
    a = Session(ids["A"], repo, at(12, "10:00"))
    a.title("レシピサイトの作り方の調査")
    a.ask("このリポジトリでレシピサイトを作る流れを調べて、設計書にまとめてください。")
    a.reply("リポジトリの構成を確かめてから、設計書を書きます。",
            [bash("ファイルの一覧", "ls -la"), read(os.path.join(repo, "README.md")), ("Grep", {"pattern": "template"})])
    a.result("README.md\ndocs\nsrc")
    write_doc(design, DESIGN, a.now + timedelta(minutes=6))
    a.reply(tools=[write(design)], minutes=6)
    a.reply("設計書を docs/specs/2026-11-recipe-design.md に書きました。一覧・検索・詳細・検査の4つの工程です。")
    a.ask("検索の工程は、材料の単位を2通りにできますか？", minutes=30)
    a.reply("できます。レシピごとに単位を付け、検索の工程で換算します。設計書の最後に書き足しました。",
            [edit(design, "- **検査**で落ちたら", "- **検査**で落ちたら")])
    a.jump(at(11, "18:20"))
    a.ask("会話が長くなったので、続きは新しい会話で進めます。")
    a.reply("わかりました。続きの会話では、設計書の表から一覧の工程を作ります。")
    a.recap("レシピサイトの流れ（一覧・検索・詳細・検査）を設計書にまとめ、材料の単位を2通りにする方針を足しました。"
            "次は、続きの会話で一覧の工程から実装します。")
    a.continued_in(ids["B"])

    # B：A の続き。一覧の工程を作り、検査をヘッドレス（H1）に任せ、引き継ぎ資料を書く
    b = Session(ids["B"], repo, at(11, "18:45"))
    b.title("レシピサイトの作り方の調査（続き）")
    b.ask("前の会話の続きです。設計書に沿って、一覧の工程から作ってください。")
    b.reply("設計書を読み、一覧の工程を作ります。", [read(design), bash("テストを走らせる", "python3 -m unittest")])
    b.result("Ran 12 tests\nOK")
    b.jump(at(10, "10:00"))
    b.reply("レシピの題名の長さの検査は、別のセッションに任せます。",
            [bash("題名の検査を別のセッションに任せる", "claude -p 'レシピの題名の長さを検査して'")])
    b.result(json.dumps({"session_id": ids["H1"], "result": "長すぎる題名が3つありました"}, ensure_ascii=False))
    h1 = Session(ids["H1"], repo, at(10, "10:01"), entrypoint="sdk-cli")
    h1.task("レシピの題名の長さを検査して、長すぎる題名を挙げてください。")
    h1.reply("長すぎる題名が3つありました（12件目・31件目・40件目）。どれも30字を超えています。",
             [read(os.path.join(repo, "docs", "recipes.json"))])
    b.jump(at(9, "16:28"))
    b.ask("検索については別のセッションで続けます。引き継ぎ資料を作成してください。")
    b.reply("引き継ぎ資料を作ります。書く前に、資料で案内するファイルがあるかを確かめます。",
            [bash("案内するパスの確認", "ls docs/specs docs/plans")])
    write_doc(handoff, HANDOFF, b.now + timedelta(minutes=3))
    b.reply(tools=[write(handoff)], minutes=3)
    b.reply("引き継ぎ資料を作りました。置き場所は docs/handoff/recipe-handoff.md です。次のセッションでは、まずこの資料を"
            "読んでください。")
    b.recap("一覧の工程と検査を作り、引き継ぎ資料（docs/handoff/recipe-handoff.md）を書きました。"
            "次は、新しいセッションで検索の工程に進みます。", minutes=30)

    # C：引き継ぎ資料を読んで開始。計画を書き、課題をヘッドレス（H2〜H4）に任せ、試しのページと PR を作る
    c = Session(ids["C"], repo, at(9, "18:00"), branch="feature/search")
    c.title("検索の引き継ぎの次の作業")
    c.ask("docs/handoff/recipe-handoff.md を読んで、引き継ぎの続きをお願いします。")
    c.reply("引き継ぎ資料を読みました。残っているのは検索の工程と、試しのページの2つです。",
            [read(handoff), read(design), bash("最近の変更を見る", "git log --oneline -5")])
    c.ask("検索の工程を、計画を立ててから進めてください。", minutes=5)
    write_doc(plan, PLAN, c.now + timedelta(minutes=4))
    c.reply(tools=[write(plan)], minutes=4)
    c.reply("計画を docs/plans/search-plan.md に書きました。課題は5つです。1〜3は別のセッションに任せます。")
    children = (("H2", "14:14", "検索の課題1：材料の名前で絞り込む"),
                ("H3", "14:20", "検索の課題2：料理名の読みがなでも探せるようにする"),
                ("H4", "14:25", "検索の課題3：結果を新しい順か人気の順に並べ替える"))
    c.jump(at(8, "14:12"))
    for name, hhmm, task in children:
        c.reply(tools=[bash("課題を別のセッションに任せる", "claude -p '%s'" % task)], minutes=1)
        c.result(json.dumps({"session_id": ids[name], "result": "済みました"}, ensure_ascii=False))
        child = Session(ids[name], repo, at(8, hhmm), branch="feature/search", entrypoint="sdk-cli")
        child.task(task + "。終わったら、変えたファイルを挙げてください。")
        child.reply(tools=[read(plan), edit(os.path.join(repo, "src", "search.py"), "limit = 10", "limit = 20")])
        child.result("ファイルを更新しました")
        child.reply("済みました。src/search.py を直し、テストを足しました。")
        fake.write(repo, child.id, child.records)
    c.jump(at(7, "11:00"))
    c.ask("試しのページを作って、絞り込み方ごとの結果を見比べられるようにしてください。")
    write_doc(trial, TRIAL_PAGE, c.now + timedelta(minutes=7))
    c.reply(tools=[write(trial), bash("ページを開いて確かめる", "open out/search-trial/index.html")], minutes=7)
    c.reply("out/search-trial/index.html に試しのページを作りました。帯の長さで、結果の件数を比べられます。")
    c.pr(24)
    c.ask("PR を作ってください。引き継ぎ資料にも、今日の進み具合を書き足してください。", minutes=20)
    write_doc(progress, PROGRESS, c.now + timedelta(minutes=4))
    with open(handoff, encoding="utf-8") as f:
        write_doc(handoff, f.read() + "\n## 進み具合（追記）\n\n課題1〜3は済んだ。詳しくは `.superpowers/recipe-site/progress.md`。\n",
                  c.now + timedelta(minutes=4))
    # タスクの task.md の現在地も書き直す（資料を書いた・確定。分岐の D と子の H2〜H4 は、元の会話から引き継ぐ）
    c.reply(tools=[write(progress), edit(handoff, "## 次にすること", "## 次にすること"),
                   edit(search_md, "## 現在地", "## 現在地\n" + SEARCH_TASK["現在地"]),
                   bash("PR を作る", "gh pr create --fill")], minutes=4)
    c.reply("PR #25 を作り、引き継ぎ資料と進み具合、タスクの現在地を更新しました。")
    c.pr(25)
    c.jump(at(5, "15:00"))
    c.recap("検索の課題5つのうち3つが済み、試しのページで絞り込み方ごとの結果を見比べられるようになりました"
            "（PR #24・#25）。次は、残りの課題4と5を進めます。")
    c.jump(at(4, "15:10"))
    c.ask("ありがとうございます。残りは明日進めます。")
    c.reply("わかりました。残りの課題は、引き継ぎ資料の「次にすること」に書いてあります。")

    # D：C から分岐して、レシピを投稿したときの知らせの文面だけを直す（要約なし）
    d = Session(ids["D"], repo, at(6, "10:00"), branch="feature/search")
    d.title("知らせの文面を直す")
    d.ask("分岐して、レシピを投稿したときの知らせの文面だけを直します。今の文面は硬すぎます。", forked_from=ids["C"])
    d.reply("知らせの文面を探して、やわらかい言い方にします。",
            [("Grep", {"pattern": "登録が完了しました"}), edit(os.path.join(repo, "src", "notify.py"),
                                                              "登録が完了しました", "レシピをのせました")])
    d.ask("もう少し短くしてください。", minutes=10)
    d.reply("短くしました。「レシピをのせました」の1行だけにしました。")

    # E：Git でない場所。資料を読んで知見を書く（要約なし）
    e = Session(ids["E"], sales_dir, at(3, "21:00"), branch="")
    e.title("売上データの集計")
    e.ask("sales.csv の日ごとの売上から、月ごとの平均を出してください。")
    e.reply("sales.csv を読み、月ごとに平均を出します。", [read(sales), bash("月ごとの平均", "python3 average.py")])
    e.result("2026-10 151780")
    e.reply("2026年10月の売上の平均は 151,780 円でした。")
    e.ask("わかったことを learnings.md に残してください。", minutes=5)
    write_doc(learnings, LEARNINGS, e.now + timedelta(minutes=3))
    write_doc(memory, MEMORY, e.now + timedelta(minutes=3))
    e.reply(tools=[write(learnings), write(memory)], minutes=3)
    e.reply("learnings.md に3点書きました。集計の決まりは、次の会話でも使えるようにメモリにも残しました。")
    # タスクのフォルダーにメモを書く（資料を書いた・確定）
    e.ask("集計の決まりを、タスクのメモにも残してください。", minutes=4)
    write_doc(average_notes, AVERAGE_NOTES, e.now + timedelta(minutes=2))
    e.reply(tools=[write(average_notes)], minutes=2)
    e.reply("タスクのフォルダーの notes-average.md に、集計の決まりを書きました。")
    e.ask("次は、どの商品から見ればよいでしょうか？", minutes=10)
    e.reply("売上の多い順に3つ見るのがよいと思います。")

    # H5：親が分からないヘッドレス（どの記録にも ID がなく、前後5分に動いた対話もない）
    h5 = Session(ids["H5"], sales_dir, at(1, "03:10"), branch="", entrypoint="sdk-cli")
    h5.task("毎朝の売上データの取り込みを確かめて、欠けた日があれば挙げてください。")
    h5.reply("欠けた日はありませんでした。", [read(sales)])

    # F：別の場所。D を ID で参照して始める（D の場所から見ると「別の場所へ移った」）
    f_ = Session(ids["F"], reading, at(2, "17:00"), branch="")
    f_.title("読み終えた本の調査")
    f_.ask("前のセッション %s では知らせの文面を直しました。今回は、notes.txt を読んで、読書メモのうち"
           "読み終えた日が書かれた本だけを一覧にしてください。" % ids["D"])
    f_.reply("notes.txt とタスクの task.md を読み、読み終えた日が書かれているかを1冊ずつ確かめます。",
             [read(notes), read(spec_md), ("Grep", {"pattern": "読み終えた"})])  # task.md を読んだ（推定（強））
    f_.reply("読み終えた日が書かれた本は2冊でした（『庭の歳時記』『海辺の町の天気』）。")
    f_.ask("この結果をもとに、元の本棚の一覧を直すかどうか決めたいです。", minutes=6)
    f_.reply("直すべき点は2つです。『やさしい家計簿』は読み終えた日がないので、「未確認」と書くのがよいと思います。")
    f_.recap("読み終えた日が書かれた本だけの一覧を作り終えました。次は、この結果をもとに元の本棚の一覧を直すかどうかを"
             "決めてください。", minutes=4)

    # T3：完了したタスクのフォルダーで始め、共通の資料を読んで調べ方を書く（フォルダーで開始・確定）
    t3 = Session(ids["T3"], spec, at(2, "16:00"), branch="")
    t3.title("読み終えた本の調べ方を決める")
    t3.ask("チケット #7 の調査を始めます。読書メモと本棚の記録だけを根拠にする調べ方を決めてください。")
    t3.reply("資料の書き方の決まりを読んでから、調べ方を決めます。", [read(writing_rules)])
    write_doc(research_spec, RESEARCH_SPEC, t3.now + timedelta(minutes=5))
    t3.reply(tools=[write(research_spec)], minutes=5)
    t3.reply("調べ方を research-finished-books.md にまとめました。読書メモと本棚の記録だけを根拠にします。")

    # G：C と同じブランチ（feature/search）で、検索の結果と一覧の件数を調べる（同じブランチ・推定（弱））
    g = Session(ids["G"], repo, at(3, "10:00"), branch="feature/search")
    g.title("検索の結果と一覧の件数を調べる")
    g.ask("検索の結果と一覧の件数がずれていないか、試しのデータで調べてください。")
    g.reply("検索の工程と一覧の工程の件数を比べます。", [bash("件数のずれを測る", "python3 -m recipesite check out/ --count")])
    g.result("ずれの最大 2件（材料で絞ったとき）")
    g.reply("ずれは最大2件でした。材料で絞ったとき、検索の結果が一覧より2件少なくなります。")

    # K：チケット番号の入ったブランチ（fix/18-image-size）で、画像の大きさの検査を足す（チケット番号・推定（弱））
    k = Session(ids["K"], repo, at(1, "11:00"), branch="fix/18-image-size")
    k.title("画像の大きさの検査を足す")
    k.ask("検査の工程に、画像の大きさの検査を足してください。")
    k.reply("検査の工程に、画像の幅と高さの上限を確かめる処理を足します。",
            [edit(os.path.join(repo, "src", "check.py"), "def check(", "def check_image_size(")])
    k.reply("画像の大きさの検査を足しました。上限を超えた画像と、小さすぎる画像を挙げます。")

    for s in (a, b, c, d, e, f_, h1, h5, t1, t3, g, k):
        fake.write(s.cwd, s.id, s.records)
    settle_folder_times(tasks)
    # 記録が消えたセッションの入力（入力履歴にだけ残る）
    fake.history([
        {"display": "前のレシピ検索の引き継ぎメモを見せてください", "pastedContents": {},
         "timestamp": int(at(60, "20:04").timestamp() * 1000), "project": repo, "sessionId": demo_id("gone-1")},
        {"display": "集計の引き継ぎをお願いします。別のセッションで続けます。", "pastedContents": {},
         "timestamp": int(at(75, "09:30").timestamp() * 1000), "project": sales_dir, "sessionId": demo_id("gone-2")},
    ])

    ledger = os.path.join(out, "ledger")
    os.makedirs(ledger, mode=0o700)
    config = {
        "claude_dir": str(fake.dir),  # --claude-dir を付け忘れても、本物の ~/.claude を読まない
        "resume_default": "copy",  # ⌘Enter でアプリを起動しない
        "display_strip_prefixes": [out],
        # 出力先が Git の中にあっても、Git でない作業ディレクトリをそれぞれ1つの場所にする
        "place_aliases": {sales_dir: sales_dir, reading: reading},
        "tasks_root": tasks,  # 偽のホームのタスクの置き場所（本物の ~/tasks は読まない・書かない）
    }
    with open(os.path.join(ledger, "config.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return {"claude": str(fake.dir), "home": home, "ledger": ledger, "tasks": tasks, "places": 5, "interactive": 10,
            "headless": 5, "documents": 19, "history": 2, "task_count": 3}


def prepare(out: str) -> Optional[str]:
    """出力先を空にする。このスクリプトが作った出力先だけを作り直す。断るときは理由を返す。"""
    if os.path.exists(out) and not os.path.isdir(out):
        return "出力先がディレクトリではありません: %s" % out
    if os.path.isdir(out) and os.listdir(out):
        if not os.path.isfile(os.path.join(out, MARKER)):
            return "出力先が空ではありません: %s（make_demo.py が作った所でなければ、作り直しません）" % out
        for name in ("claude", "work", "ledger", "home"):
            shutil.rmtree(os.path.join(out, name), ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, MARKER), "w", encoding="utf-8") as f:
        f.write("scripts/make_demo.py が作った偽のデータです。\n")
    return None


def main(argv: List[str]) -> int:
    if len(argv) != 1 or argv[0].startswith("-"):
        print("使い方: python3 scripts/make_demo.py <出力先>", file=sys.stderr)
        return 2
    out = os.path.realpath(os.path.abspath(argv[0]))
    problem = prepare(out)
    if problem:
        print("make_demo: " + problem, file=sys.stderr)
        return 2
    made = build(out)
    if (out + "/").startswith(TMP_PREFIXES):
        print("注意: 出力先が一時領域（/tmp など）にあるので、サーバーは資料を索引に入れません。"
              "資料も見るときは、一時領域の外に作ってください。", file=sys.stderr)
    print("偽のデータを作りました: %s" % out)
    print("  場所 %(places)d、対話 %(interactive)d件、ヘッドレス %(headless)d件、資料 %(documents)d件、"
          "記録が消えた入力履歴 %(history)d件、タスク %(task_count)d件と shared/" % made)
    print("  タスクの置き場所: %s（ledger/config.json の tasks_root）" % made["tasks"])
    print("画面を開くには（表示された URL をブラウザで開く）:")
    print("  PYTHONPATH=%s HOME=%s TSUZUKI_HOME=%s python3 -m tsuzuki --claude-dir %s serve --port %d --no-open" % (
        shlex.quote(str(ROOT / "src")), shlex.quote(made["home"]), shlex.quote(made["ledger"]),
        shlex.quote(made["claude"]), PORT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
