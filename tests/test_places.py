import os
import tempfile
import unittest

from tsuzuki.places import TMP_PLACE, display_name, encode_project, place_id_for, resolve_place, short_labels
from tsuzuki.store import open_ledger
from tests.fixtures import write_file


class PlacesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = os.path.realpath(self._tmp.name)

    def test_tasks_root_rule(self):
        ref = resolve_place("/Users/me/tasks/2026-11-20-x/sub", aliases={}, tasks_root="/Users/me/tasks",
                            exists=lambda p: False, isdir=lambda p: True)
        self.assertEqual(ref.path, "/Users/me/tasks")
        ref = resolve_place("/Users/me/tasks/2026-11-20-x",
                            aliases={"/Users/me/tasks/2026-11-20-x": "/Users/me/dev/web"},
                            tasks_root="/Users/me/tasks", exists=lambda p: False, isdir=lambda p: True)
        self.assertEqual(ref.path, "/Users/me/dev/web")  # 規則1が先

    def test_tasks_display_names(self):
        kw = dict(home="/Users/me", strip_prefixes=[], tasks_root="/Users/me/tasks")
        self.assertEqual(display_name("/Users/me/tasks", **kw), "タスク")
        self.assertEqual(display_name("/Users/me/tasks/shared", **kw), "共通の資料")
        self.assertEqual(display_name("/Users/me/dev/web", **kw), "~/dev/web")
        self.assertEqual(display_name("/Users/me/tasks", home="/Users/me", strip_prefixes=[]), "~/tasks")

    def test_git_root_and_worktrees(self):
        d = self.d
        os.makedirs(f"{d}/repo/.git")
        os.makedirs(f"{d}/repo/sub/x")
        os.makedirs(f"{d}/repo/.claude/worktrees/w2/a")
        os.makedirs(f"{d}/repo/.claude/worktrees/w2/.git")  # 規則2だけが repo を返す
        write_file(f"{d}/wt/.git", f"gitdir: {d}/repo/.git/worktrees/w1\n")
        self.assertEqual(resolve_place(f"{d}/repo/sub/x", aliases={}).path, f"{d}/repo")
        self.assertEqual(resolve_place(f"{d}/wt", aliases={}).path, f"{d}/repo")
        self.assertEqual(resolve_place(f"{d}/repo/.claude/worktrees/w2/a", aliases={}).path, f"{d}/repo")

    def test_alias_beats_worktree_and_tmp_rules(self):
        wt = "/x/repo/.claude/worktrees/w2/a"
        self.assertEqual(resolve_place(wt, aliases={"/x/repo/.claude": "/ALIAS"}, exists=lambda p: False).path, "/ALIAS")
        tmp = "/private/tmp/claude-501/-enc/0d1e2f3a-4b5c-4d6e-8f70-8192a3b4c5d6/scratchpad"
        r = resolve_place(tmp, aliases={"/private/tmp": "/ALIAS"}, exists=lambda p: False)
        self.assertEqual((r.path, r.tmp_session, r.tmp_encoded), ("/ALIAS", None, None))

    def test_tmp_beats_git_walk(self):
        tmp = "/private/tmp/claude-501/-enc/0d1e2f3a-4b5c-4d6e-8f70-8192a3b4c5d6/scratchpad"
        r = resolve_place(tmp, aliases={}, exists=lambda p: p == "/private/tmp/.git", isdir=lambda p: True)
        self.assertEqual((r.path, r.tmp_session), (None, "0d1e2f3a-4b5c-4d6e-8f70-8192a3b4c5d6"))

    def test_relative_gitdir(self):
        d = self.d
        os.makedirs(f"{d}/repo/.git/worktrees/w1")
        write_file(f"{d}/wt/.git", "gitdir: ../repo/.git/worktrees/w1\n")
        os.makedirs(f"{d}/wt/deep")
        self.assertEqual(resolve_place(f"{d}/wt/deep", aliases={}).path, f"{d}/repo")

    def test_submodule_gitfile_is_itself(self):
        d = self.d
        write_file(f"{d}/sup/x/.git", "gitdir: ../.git/modules/x\n")
        os.makedirs(f"{d}/sup/x/src")
        self.assertEqual(resolve_place(f"{d}/sup/x/src", aliases={}).path, f"{d}/sup/x")

    def test_aliases_use_longest_prefix(self):
        self.assertEqual(resolve_place("/a/b/c", aliases={"/a": "/A", "/a/b": "/B"}, exists=lambda p: False).path, "/B")

    def test_alias_trailing_slash_and_boundary(self):
        self.assertEqual(resolve_place("/a/b", aliases={"/a/": "/A"}, exists=lambda p: False).path, "/A")
        self.assertEqual(resolve_place("/ab/c", aliases={"/a": "/A"}, exists=lambda p: False).path, "/ab/c")

    def test_tmp_dir(self):
        r = resolve_place("/private/tmp/claude-501/-Volumes-x-dev-claude/0d1e2f3a-4b5c-4d6e-8f70-8192a3b4c5d6/scratchpad", aliases={})
        self.assertEqual((r.path, r.tmp_session, r.tmp_encoded), (None, "0d1e2f3a-4b5c-4d6e-8f70-8192a3b4c5d6", "-Volumes-x-dev-claude"))

    def test_non_git_and_missing(self):
        d = self.d
        os.makedirs(f"{d}/plain")
        self.assertEqual(resolve_place(f"{d}/plain", aliases={}).path, f"{d}/plain")
        self.assertEqual(resolve_place("/no/such/dir", aliases={}).path, "/no/such/dir")
        self.assertEqual(resolve_place("/no/such/it's 日本語 dir", aliases={}).path, "/no/such/it's 日本語 dir")
        self.assertEqual(resolve_place("", aliases={}).path, "")

    def test_display_and_encode(self):
        self.assertEqual(display_name("/Volumes/external/dev/x", home="/Users/u", strip_prefixes=["/Volumes/external/"]), "dev/x")
        self.assertEqual(display_name("/Users/u/notes", home="/Users/u", strip_prefixes=[]), "~/notes")
        self.assertEqual(display_name("/Users/u", home="/Users/u", strip_prefixes=[]), "~")
        self.assertEqual(display_name(TMP_PLACE, home="/Users/u", strip_prefixes=[]), "一時")
        self.assertEqual(encode_project("/Volumes/external/dev/claude"), "-Volumes-external-dev-claude")

    def test_place_id_for(self):
        conn = open_ledger(os.path.join(self.d, "ledger.db"))
        a = place_id_for(conn, "/Volumes/e/dev/x", home="/Users/u", strip_prefixes=[])
        self.assertEqual(place_id_for(conn, "/Volumes/e/dev/x", home="/Users/u", strip_prefixes=[]), a)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM places").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT display FROM places WHERE id=?", (a,)).fetchone()[0], "/Volumes/e/dev/x")
        b = place_id_for(conn, "/Volumes/e/dev/x", home="/Users/u", strip_prefixes=["/Volumes/e"])
        self.assertEqual(a, b)
        self.assertEqual(conn.execute("SELECT display FROM places WHERE id=?", (a,)).fetchone()[0], "dev/x")
        conn.close()

    # 場所の短い名前（画面の狭い所に出す。表示名の最後の要素から、重ならなくなるまで親を足す）

    def test_short_label_is_the_last_component(self):
        displays = ["/Volumes/external/dev/claude", "~/notes", "dev/garden", "一時"]
        self.assertEqual(short_labels(displays), {"/Volumes/external/dev/claude": "claude", "~/notes": "notes",
                                                  "dev/garden": "garden", "一時": "一時"})
        self.assertEqual(short_labels([]), {})

    def test_short_labels_add_parents_until_unique(self):
        displays = ["/Volumes/external/dev/claude", "/Volumes/external/lab/claude", "/Volumes/external/dev/garden",
                    "~/claude"]
        self.assertEqual(short_labels(displays), {
            "/Volumes/external/dev/claude": "dev/claude", "/Volumes/external/lab/claude": "lab/claude",
            "/Volumes/external/dev/garden": "garden", "~/claude": "~/claude"})

    def test_short_labels_of_the_same_name_in_different_trees(self):
        # 親を1つ足しても重なれば、もう1つ足す。親をたどり切っても重なる場所は、表示名をそのまま出す
        displays = ["/w/a/src/app", "/w/b/src/app", "/old/src/app", "src/app"]
        labels = short_labels(displays)
        self.assertEqual(labels, {"/w/a/src/app": "a/src/app", "/w/b/src/app": "b/src/app",
                                  "/old/src/app": "/old/src/app", "src/app": "src/app"})
        self.assertEqual(len(set(labels.values())), len(displays))
        # 同じ表示名の場所（接頭辞を外した結果が同じ）は見分けられないので、同じ短い名前になる
        self.assertEqual(short_labels(["dev/x", "dev/x", "lab/y"]), {"dev/x": "x", "lab/y": "y"})

    def test_short_labels_of_home_and_root(self):
        # 表示名が ~ や / だけの場所は、それ自身が短い名前
        self.assertEqual(short_labels(["~", "/", "/Volumes/x/proj"]), {"~": "~", "/": "/", "/Volumes/x/proj": "proj"})
        self.assertEqual(short_labels(["~", "~/x", "/x"]), {"~": "~", "~/x": "~/x", "/x": "/x"})


if __name__ == "__main__":
    unittest.main()
