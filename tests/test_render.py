"""画面に出す文字の変換（Markdown・強調の印・時刻）のテスト。"""
import re
import time
import unittest
from datetime import datetime

from tsuzuki.render import fmt_local, highlight, render_markdown, render_plain, render_pre

# render_markdown が出してよいタグ。入力の HTML はすべて文字として出る（div は Mermaid の図の囲みだけ）
ALLOWED_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "code", "blockquote", "hr",
                "table", "thead", "tbody", "tr", "th", "td", "strong", "em", "a", "div"}
MERMAID_OPEN = '<div class="mermaid-block"><pre class="mermaid-src"><code>'


def local_str(iso: str, fmt: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime(fmt)


class RenderTest(unittest.TestCase):
    def assertOnlyKnownMarkup(self, html):
        for name in re.findall(r"</?([A-Za-z][A-Za-z0-9]*)", html):
            self.assertIn(name.lower(), ALLOWED_TAGS, html)
        for href in re.findall(r"<a ([^>]*)>", html):
            self.assertRegex(href, r'^href="https?://[^"<>]*" rel="noopener noreferrer" target="_blank"$')


    def test_markdown_subset(self):
        html = render_markdown("# H\n\n- a\n- b\n\n```\ncode <x>\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        for part in ("<h1>H</h1>", "<li>a</li>", "<pre><code>code &lt;x&gt;</code></pre>", "<table>", "<td>1</td>"):
            self.assertIn(part, html)

    def test_markdown_is_safe(self):
        html = render_markdown("<script>alert(1)</script>\n\n[x](javascript:alert(1)) [y](https://example.com)")
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript', html)
        self.assertIn('href="https://example.com"', html)

    def test_highlight_and_time(self):
        self.assertEqual(highlight("a<b>引き継ぎ", [(4, 8)]), "a&lt;b&gt;<mark>引き継ぎ</mark>")
        now = datetime(2026, 11, 21, 18, 0).astimezone()
        self.assertEqual(fmt_local("2026-11-21T08:18:00.000Z", now=now),
                         local_str("2026-11-21T08:18:00+00:00", "%m/%d %H:%M"))

    # Markdown

    def test_markdown_blocks(self):
        html = render_markdown(
            "## 二\n###### 六\n####### 七\n\n> 引用の\n> 続き\n\n1. 一つ目\n2. 二つ目\n   - 入れ子\n\n"
            "---\n\n3. 三\n4. 四\n\n* * *\n\n- 上\n  - 下\n- 次")
        self.assertIn("<h2>二</h2>", html)
        self.assertIn("<h6>六</h6>", html)
        self.assertIn("<p>####### 七</p>", html)
        self.assertIn("<blockquote>\n<p>引用の\n続き</p>\n</blockquote>", html)
        self.assertIn("<ol><li>一つ目</li><li>二つ目<ul><li>入れ子</li></ul></li></ol>", html)
        self.assertIn('<ol start="3"><li>三</li><li>四</li></ol>', html)
        self.assertEqual(html.count("<hr>"), 2)
        self.assertIn("<ul><li>上<ul><li>下</li></ul></li><li>次</li></ul>", html)
        self.assertOnlyKnownMarkup(html)

    def test_breaks_keep_single_newlines(self):
        self.assertEqual(render_markdown("一行目\n二行目", breaks=True), "<p>一行目<br>\n二行目</p>")
        self.assertIn("<li>a<br>\nb</li>", render_markdown("- a\n  b\n- c", breaks=True))
        self.assertIn("<blockquote>\n<p>a<br>\nb</p>\n</blockquote>", render_markdown("> a\n> b", breaks=True))
        code = render_markdown("```\nx\ny\n```", breaks=True)
        self.assertNotIn("<br>", code)
        self.assertIn("<pre><code>x\ny</code></pre>", code)

    def test_breaks_off_is_unchanged(self):
        text = "a\nb\n\n- c\n  d\n\n> e\n> f"
        self.assertEqual(render_markdown(text), render_markdown(text, breaks=False))
        self.assertNotIn("<br>", render_markdown(text))

    def test_markdown_paragraphs_keep_newlines(self):
        self.assertEqual(render_markdown("一行目\n二行目\n\n次の段落"), "<p>一行目\n二行目</p>\n<p>次の段落</p>")
        # 見出しの閉じの # は外し、語の一部の # は残す
        self.assertEqual(render_markdown("## 題 ##\n# C#"), "<h2>題</h2>\n<h1>C#</h1>")

    def test_markdown_inline(self):
        html = render_markdown("`a <b>` と **太字** と *斜体* と _斜体_ と ![図の説明](https://example.com/a.png)"
                               " と snake_case_name と ***両方***")
        self.assertIn("<code>a &lt;b&gt;</code>", html)
        self.assertIn("<strong>太字</strong>", html)
        self.assertIn("<em>斜体</em> と <em>斜体</em>", html)
        self.assertIn("図の説明", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("a.png", html)
        self.assertIn("snake_case_name", html)
        self.assertIn("<strong><em>両方</em></strong>", html)
        # コードの中は変換せず、リンクの中のコードと強調は変換する
        self.assertEqual(render_markdown("`**x** [y](https://e.com)`"), "<p><code>**x** [y](https://e.com)</code></p>")
        self.assertEqual(render_markdown("**[`f.py`](https://e.com/a_b_c)** と *a **b** c*"),
                         '<p><strong><a href="https://e.com/a_b_c" rel="noopener noreferrer" target="_blank">'
                         "<code>f.py</code></a></strong> と <em>a <strong>b</strong> c</em></p>")

    def test_markdown_fences_and_tables(self):
        # 言語名の付いた囲み、閉じていない囲みは最後まで
        self.assertEqual(render_markdown("```python\nif a < b:\n    pass\n```"),
                         "<pre><code>if a &lt; b:\n    pass</code></pre>")
        self.assertEqual(render_markdown("```\nx\n\n# 見出しではない"), "<pre><code>x\n\n# 見出しではない</code></pre>")
        # 箇条書きの中の囲み
        self.assertEqual(render_markdown("1. 実行する：\n   ```\n   make\n   ```\n2. 次"),
                         "<ol><li>実行する：<pre><code>make</code></pre></li><li>次</li></ol>")
        html = render_markdown("| 左 | 右 | 中 |\n|:--|--:|:-:|\n| `a\\|b` | 2 |\n| 3 | 4 | 5 | 6 |")
        self.assertIn('<th align="left">左</th><th align="right">右</th><th align="center">中</th>', html)
        self.assertIn('<td align="left"><code>a|b</code></td><td align="right">2</td><td align="center"></td>', html)
        self.assertIn('<td align="center">5</td></tr>', html)
        self.assertNotIn("6", html)
        # 区切りの行がなければ表にしない
        self.assertEqual(render_markdown("a | b\nc | d"), "<p>a | b\nc | d</p>")

    def test_markdown_never_emits_input_html(self):
        samples = [
            "<img src=x onerror=alert(1)>",
            "[x](https://a.example/\"onmouseover=\"alert(1)) と [y](https://b.example/'x')",
            "![x](javascript:alert(1)) [z](data:text/html,<b>) [w]( javascript:alert(1))",
            "```\n</code></pre><script>alert(1)</script>\n```",
            "> <iframe src=\"https://e.com\"></iframe>\n- <b>a</b>\n\n| <i>x</i> |\n|---|\n| <u>y</u> |",
            "`</code><script>` **<em>x</em>** _<s>y</s>_",
            "\x00<p>\ue000",
            "```mermaid\n</code></pre></div><script>alert(1)</script>\n```",
        ]
        for text in samples:
            html = render_markdown(text)
            self.assertOnlyKnownMarkup(html)
            for raw in ("<img", "<script", "<iframe", "<b>", "<i>", "<u>", "<s>"):
                self.assertNotIn(raw, html)
            self.assertNotIn("href=\"javascript", html)
            self.assertNotIn("href=\"data", html)
        self.assertNotIn('"onmouseover', render_markdown(samples[1]))

    def test_mermaid_block(self):
        html = render_markdown("```mermaid\ngraph TD; A-->B<script>\n```\n")
        self.assertIn('<div class="mermaid-block"><pre class="mermaid-src"><code>', html)
        self.assertIn("A--&gt;B&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("mermaid-block", render_markdown("```python\nprint(1)\n```\n"))

    def test_mermaid_block_details(self):
        # 囲み全体を1つの div にする。中身はほかの囲みと同じく、エスケープしたまま
        self.assertEqual(render_markdown("```mermaid\nsequenceDiagram\n  A->>B: \"やあ\" & <b>\n```"),
                         MERMAID_OPEN + "sequenceDiagram\n  A-&gt;&gt;B: &quot;やあ&quot; &amp; &lt;b&gt;"
                         "</code></pre></div>")
        # 言語名は情報文字列の最初の語（大文字・小文字は問わない）。~~~ の囲みも、箇条書きの中の囲みも
        for text in ("```mermaid {init: 1}\nx\n```", "``` Mermaid\nx\n```", "~~~mermaid\nx\n~~~"):
            with self.subTest(text=text):
                self.assertEqual(render_markdown(text), MERMAID_OPEN + "x</code></pre></div>")
        self.assertEqual(render_markdown("- 図：\n  ```mermaid\n  graph LR\n  ```"),
                         "<ul><li>図：" + MERMAID_OPEN + "graph LR</code></pre></div></li></ul>")
        # 名前が mermaid で始まるだけの言語や、言語のない囲みは今までどおり
        for text in ("```mermaidx\nx\n```", "```\nmermaid\n```", "```js mermaid\nx\n```"):
            with self.subTest(text=text):
                self.assertTrue(render_markdown(text).startswith("<pre><code>"))

    def test_markdown_handles_many_markers_quickly(self):
        # 閉じない印が並んでも、行の長さに比例する時間で終わる（全部で 0.1 秒ほど。2乗の時間なら数分かかる）
        elapsed = 0.0
        for text in ("*a " * 50000, "_a " * 50000, "`" + "a``" * 30000, "[a](" * 30000, "> " * 3000 + "x",
                     "#" * 100000, "- " + " " * 100000 + "x", "| " * 50000 + "\n" + "|-" * 50000):
            started = time.perf_counter()
            html = render_markdown(text)
            elapsed += time.perf_counter() - started
            self.assertTrue(html)
            self.assertOnlyKnownMarkup(html)
        self.assertLess(elapsed, 1.0)

    # 文字・強調の印・時刻

    def test_render_plain_and_pre(self):
        self.assertEqual(render_plain("a<b>\n2行目"), "a&lt;b&gt;<br>2行目")
        self.assertEqual(render_pre("x & <y>\n"), "<pre>x &amp; &lt;y&gt;\n</pre>")

    def test_highlight_merges_and_clamps(self):
        self.assertEqual(highlight("abcdef", [(3, 5), (1, 4)]), "a<mark>bcde</mark>f")
        self.assertEqual(highlight("a&b", [(2, 99), (-5, 1)]), "<mark>a</mark>&amp;<mark>b</mark>")
        self.assertEqual(highlight("<x>", []), "&lt;x&gt;")
        self.assertEqual(highlight("abc", [(2, 1), (5, 9)]), "abc")

    def test_fmt_local_other_year_and_invalid(self):
        now = datetime(2027, 1, 5, 12, 0).astimezone()
        self.assertEqual(fmt_local("2026-07-13T11:04:00.000Z", now=now),
                         local_str("2026-07-13T11:04:00+00:00", "%Y/%m/%d %H:%M"))
        self.assertEqual(fmt_local("2026-07-13T20:04:00+09:00", now=now),
                         local_str("2026-07-13T20:04:00+09:00", "%Y/%m/%d %H:%M"))
        # 秒の端数が3桁でなくても読む
        self.assertEqual(fmt_local("2027-01-02T03:04:05.1Z", now=now),
                         local_str("2027-01-02T03:04:05+00:00", "%m/%d %H:%M"))
        for bad in (None, "", "昨日", "2026-13-40T00:00:00Z", "9999-12-31T23:59:59-14:00"):
            self.assertEqual(fmt_local(bad, now=now), "", bad)
        self.assertRegex(fmt_local("2026-11-21T08:18:00.000Z"), r"^(\d{4}/)?\d{2}/\d{2} \d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
