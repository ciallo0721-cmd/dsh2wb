#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh2wb 回归测试。

    python -m unittest discover -s tests -v

每条测试都对应一个踩过的坑。改解析逻辑时先跑这个。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dsh2wb as m  # noqa: E402


# ---------------------------------------------------------------- 测试工具


def make_plugin(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def read_all_texts(dst: Path) -> str:
    """把产物里所有文本文件拼起来，用来断言「不该出现的东西确实没出现」。"""
    buf = []
    for p in dst.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".md", ".sh", ".cmd"):
            buf.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(buf)


class PluginCase(unittest.TestCase):
    """每个用例一个临时目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, files: dict[str, str], vendor: str = "none") -> tuple[m.Analysis, Path]:
        src = make_plugin(self.tmp / "src", files)
        a = m.analyze(src, None)
        dst = m.build(a, self.tmp / "out", vendor, force=True)
        return a, dst


# ---------------------------------------------------------------- 转义还原


class TestJsUnescape(unittest.TestCase):
    def test_unicode_escape_keeps_chinese(self):
        """v0.1 用 .encode().decode('unicode_escape') 会把整条中文变乱码。"""
        self.assertEqual(m._js_unescape(r"\u4fe1\u606f"), "信息")

    def test_plain_chinese_untouched(self):
        self.assertEqual(m._js_unescape("获取用户信息，只读"), "获取用户信息，只读")

    def test_mixed_escape_and_chinese(self):
        self.assertEqual(m._js_unescape(r"\u67e5\u8be2 \u72b6\u6001"), "查询 状态")

    def test_quote_and_newline(self):
        self.assertEqual(m._js_unescape(r"it\'s\nok"), "it's\nok")

    def test_unknown_escape_passthrough(self):
        self.assertEqual(m._js_unescape(r"a\qb"), r"a\qb")


# ---------------------------------------------------------------- 分区识别


class TestSectionKind(unittest.TestCase):
    def test_em_dash_chinese_read(self):
        self.assertEqual(
            m._section_kind("// —— 读（不需要写操作，安全） ——"), "read")

    def test_em_dash_does_not_see_write_inside_parens(self):
        """括号里出现「写」不能把整张只读表变成写表。"""
        self.assertEqual(
            m._section_kind("// —— 读（会跳过所有写操作） ——"), "read")

    def test_em_dash_chinese_write(self):
        self.assertEqual(
            m._section_kind("// —— 写（会真的发出去） ——"), "write")

    def test_english_dashes(self):
        self.assertEqual(m._section_kind("// --- read ---"), "read")
        self.assertEqual(m._section_kind("# --- write ---"), "write")

    def test_bracket_form(self):
        self.assertEqual(m._section_kind("// [只读]"), "read")
        self.assertEqual(m._section_kind("// [write]"), "write")

    def test_plain_comment_exact(self):
        self.assertEqual(m._section_kind("// 读"), "read")
        self.assertEqual(m._section_kind("## 写操作"), "write")
        self.assertEqual(m._section_kind("// 只读命令"), "read")

    def test_plain_prose_comment_is_not_a_section(self):
        """回归：`// 列出全部命令` 以「列」开头，绝不能当成只读分区。"""
        self.assertIsNone(m._section_kind("// 列出全部命令"))
        self.assertIsNone(m._section_kind("// 发送前记得检查配置"))
        self.assertIsNone(m._section_kind("# 这个文件负责编排所有子命令"))


# ---------------------------------------------------------------- 读写判定


class TestClassify(unittest.TestCase):
    def _c(self, name, desc, kind="unknown"):
        return m.Command(name=name, target="x.mjs", desc=desc, kind=kind)

    def test_write_wins_over_dry_flag(self):
        """回归：写命令说明里带 --dry，v0.1 会被判成只读 —— 代价是用户的账号。"""
        self.assertEqual(
            m.classify(self._c("comment-add", "发一条评论，先用 --dry 预览再 --go 发出")),
            "write")

    def test_write_hint_with_read_word(self):
        self.assertEqual(
            m.classify(self._c("post", "查看后发布动态")), "write")

    def test_chinese_create_is_write(self):
        self.assertEqual(m.classify(self._c("create", "新建一条笔记")), "write")

    def test_chinese_search_is_read(self):
        self.assertEqual(m.classify(self._c("search", "按关键词搜索笔记")), "read")

    def test_pure_read(self):
        self.assertEqual(m.classify(self._c("list", "列出全部笔记")), "read")

    def test_unknown_stays_meta(self):
        self.assertEqual(m.classify(self._c("zzz", "奇妙的操作")), "meta")

    def test_section_kind_is_respected(self):
        """分区已经给了答案，就不许关键词覆盖它。"""
        self.assertEqual(
            m.classify(self._c("delete-all", "删除全部", kind="read")), "read")


# ---------------------------------------------------------------- 命令表解析


class TestParseCommandTable(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def parse(self, body: str) -> list[m.Command]:
        p = self.dir / "cli.mjs"
        p.write_text(body, encoding="utf-8")
        return m.parse_command_table(p)

    def test_two_element_array(self):
        cmds = self.parse(
            "const map = {\n"
            "  list: ['commands/list.mjs', '列出全部'],\n"
            "  show: ['commands/show.mjs', '查看单条'],\n"
            "};\n")
        self.assertEqual([c.name for c in cmds], ["list", "show"])
        self.assertEqual(cmds[0].desc, "列出全部")

    def test_single_element_array(self):
        """省掉说明的写法很常见，必须能解析。"""
        cmds = self.parse(
            "const map = {\n"
            "  list: ['commands/list.mjs'],\n"
            "  show: ['commands/show.mjs'],\n"
            "};\n")
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0].desc, "")

    def test_string_value(self):
        cmds = self.parse(
            "const map = {\n"
            "  list: 'commands/list.mjs',\n"
            "  show: 'commands/show.mjs',\n"
            "};\n")
        self.assertEqual([c.name for c in cmds], ["list", "show"])

    def test_object_value(self):
        cmds = self.parse(
            "const map = {\n"
            "  list: { file: 'commands/list.mjs', desc: '列出全部' },\n"
            "};\n")
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].desc, "列出全部")

    def test_quoted_keys(self):
        cmds = self.parse(
            "const map = {\n"
            "  'comment-add': ['commands/ca.mjs', '发评论'],\n"
            "  \"comment-del\": ['commands/cd.mjs', '删评论'],\n"
            "};\n")
        self.assertEqual([c.name for c in cmds], ["comment-add", "comment-del"])

    def test_variable_name_irrelevant(self):
        """回归：v0.1 只认 `const map = {` / `const COMMANDS`，换个名字就 0 条。"""
        for var in ("map", "commands", "COMMANDS", "cmdMap", "SUBCOMMANDS", "table"):
            cmds = self.parse(
                f"const {var} = {{\n"
                f"  list: ['commands/list.mjs', '列出'],\n"
                f"  show: ['commands/show.mjs', '查看'],\n"
                f"}};\n")
            self.assertEqual(len(cmds), 2, f"变量名 {var} 没解析出来")

    def test_non_path_values_ignored(self):
        """普通配置对象不能被当成命令表。"""
        cmds = self.parse(
            "const cfg = {\n"
            "  timeout: [1000, '毫秒'],\n"
            "  retries: [3, '次'],\n"
            "};\n")
        self.assertEqual(cmds, [])


# ---------------------------------------------------------------- 入口选择


class TestFindEntry(PluginCase):
    def test_missing_targets_marked(self):
        src = make_plugin(self.tmp / "src", {
            "package.json": json.dumps({"name": "dsh-t", "version": "1.0.0"}),
            "tools/cli.mjs": (
                "const map = {\n"
                "  list: ['commands/list.mjs', '列出'],\n"
                "  show: ['commands/show.mjs', '查看'],\n"
                "};\n"),
            "commands/list.mjs": "export default 1;\n",
        })
        a = m.analyze(src, None)
        got = {c.name: c.exists for c in a.commands}
        self.assertTrue(got["list"])
        self.assertFalse(got["show"])
        self.assertEqual([c.name for c in a.missing], ["show"])
        self.assertTrue(a.runnable)

    def test_prefers_complete_table(self):
        """两个候选表，选实现文件齐全的那个，哪怕条数少。"""
        src = make_plugin(self.tmp / "src", {
            "package.json": json.dumps({"name": "dsh-t"}),
            "a/broken.mjs": (
                "const map = {\n"
                "  a1: ['x/a1.mjs', '一'],\n"
                "  a2: ['x/a2.mjs', '二'],\n"
                "  a3: ['x/a3.mjs', '三'],\n"
                "  a4: ['x/a4.mjs', '四'],\n"
                "};\n"),
            "b/real.mjs": (
                "const map = {\n"
                "  b1: ['b/r1.mjs', '一'],\n"
                "  b2: ['b/r2.mjs', '二'],\n"
                "};\n"),
            "b/r1.mjs": "",
            "b/r2.mjs": "",
        })
        a = m.analyze(src, None)
        self.assertEqual([c.name for c in a.commands], ["b1", "b2"])


# ---------------------------------------------------------------- 端到端


class TestEndToEnd(PluginCase):
    def note_plugin(self) -> dict[str, str]:
        return {
            "package.json": json.dumps({
                "name": "dsh-note-taker",
                "version": "1.2.0",
                "description": "本地笔记管理插件，支持 Markdown 归档。",
                "dependencies": {"better-sqlite3": "^9.0.0"},
            }),
            "tools/cli.mjs": (
                "const map = {\n"
                "  // --- read ---\n"
                "  list:   ['commands/list.mjs', '列出全部笔记'],\n"
                "  search: ['commands/search.mjs', '按关键词搜索笔记'],\n"
                "  // --- write ---\n"
                "  create: ['commands/create.mjs', '新建一条笔记'],\n"
                "  remove: ['commands/remove.mjs', '删除指定笔记'],\n"
                "};\n"),
            "commands/list.mjs": "",
            "commands/search.mjs": "",
            "commands/create.mjs": "",
            "commands/remove.mjs": "",
        }

    def test_no_bili_residue_for_other_plugin(self):
        """回归：v0.1 把 B站 专属文案写死进模板，转别的插件满篇胡话。"""
        a, dst = self.build(self.note_plugin())
        text = read_all_texts(dst)
        for bad in ("bili", "B站", "bilibili", "-352", "--go", "二维码", "风控",
                    "扫码", "cookie"):
            self.assertNotIn(bad, text, f"产物里出现了不属于这个插件的内容：{bad}")

    def test_docs_absent_when_not_probed(self):
        """没探测到的东西，一段都不许写。"""
        _, dst = self.build(self.note_plugin())
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        setup = (dst / "references" / "setup.md").read_text(encoding="utf-8")
        porting = (dst / "references" / "porting.md").read_text(encoding="utf-8")
        # 这个插件没有调度器、没有测试、没有登录态
        self.assertNotIn("调度器", skill)
        self.assertNotIn("scheduler", porting)
        self.assertNotIn("node --test", setup)
        self.assertNotIn("凭据", setup)
        # 有 deps，所以 npm install 该出现
        self.assertIn("npm install --omit=dev", skill)
        # 没有 LICENSE 文件时不许编造 MIT
        self.assertNotIn("，MIT。", skill)

    def test_license_detected(self):
        files = self.note_plugin()
        files["LICENSE"] = "MIT License\n\nCopyright (c) 2026\n"
        _, dst = self.build(files)
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("，MIT。", skill)

    def test_write_rules_only_when_writes_exist(self):
        files = self.note_plugin()
        _, dst = self.build(files)
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("安全约定", skill)
        self.assertIn("`remove`", skill)
        # 没有 --dry 的插件不许教人用 --dry
        self.assertNotIn("--dry", skill)

    def test_dry_flag_propagates(self):
        files = self.note_plugin()
        files["commands/create.mjs"] = "// 支持 --dry 预览\n"
        _, dst = self.build(files)
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--dry", skill)

    def test_network_write_triggers_ratelimit_rule(self):
        files = self.note_plugin()
        files["commands/create.mjs"] = (
            "await fetch(url, { method: 'POST', body: 'x' });\n")
        _, dst = self.build(files)
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("不许重试", skill)

    def test_env_vars_wired_into_wrapper(self):
        """回归：v0.1 的 wrapper 写死 DSH_HOME / BILI_WHALE_CONFIG，不看 env_vars。"""
        files = self.note_plugin()
        files["commands/list.mjs"] = (
            "const home = process.env.NOTE_HOME;\n"
            "const cfg = process.env.NOTE_TAKER_CONFIG;\n")
        a, dst = self.build(files)
        self.assertIn("NOTE_HOME", a.env_vars)
        self.assertIn("NOTE_TAKER_CONFIG", a.env_vars)

        sh = (dst / "scripts" / "note-taker.sh").read_text(encoding="utf-8")
        self.assertIn('export NOTE_HOME="$SKILL_DIR/data"', sh)
        self.assertIn('export NOTE_TAKER_CONFIG="$SKILL_DIR/data/', sh)
        self.assertNotIn("DSH_HOME", sh)
        self.assertNotIn("BILI_WHALE", sh)
        self.assertNotIn("D:/New Folder", sh)

        cmd = (dst / "scripts" / "note-taker.cmd").read_text(encoding="utf-8")
        self.assertIn('set "NOTE_HOME=%SKILL_DIR%\\data"', cmd)

        porting = (dst / "references" / "porting.md").read_text(encoding="utf-8")
        self.assertIn("路径类变量，包装脚本已经重定向到", porting)

    def test_non_path_env_not_redirected(self):
        """回归：v0.2 早期把所有 env 都指向 data/，BILI_ME_MID 这种账号 ID 会当场炸。

        只有名字像路径的才许重定向；ID / 开关 / 外部程序路径一律原样透传。
        """
        files = self.note_plugin()
        files["commands/list.mjs"] = (
            "const mid = process.env.NOTE_ME_MID;\n"
            "const noCache = process.env.NOTE_NO_CACHE;\n"
            "const ffmpeg = process.env.FFMPEG;\n"
            "const home = process.env.NOTE_HOME;\n")
        a, dst = self.build(files)
        for v in ("NOTE_ME_MID", "NOTE_NO_CACHE", "FFMPEG"):
            self.assertIn(v, a.env_vars)

        sh = (dst / "scripts" / "note-taker.sh").read_text(encoding="utf-8")
        self.assertIn('export NOTE_HOME="$SKILL_DIR/data"', sh)
        self.assertNotIn("NOTE_ME_MID=", sh)
        self.assertNotIn("NOTE_NO_CACHE=", sh)
        self.assertNotIn("FFMPEG=", sh)

        porting = (dst / "references" / "porting.md").read_text(encoding="utf-8")
        # 文档里要如实列出，只是不 export
        self.assertIn("`NOTE_ME_MID`", porting)
        self.assertIn("原样透传", porting)

    def test_runtime_noise_env_ignored(self):
        """Node 自己的运行时变量不该出现在产物里。"""
        files = self.note_plugin()
        files["commands/list.mjs"] = (
            "if (process.env.NODE_TEST_CONTEXT) {}\n"
            "if (process.env.ELECTRON_RUN_AS_NODE) {}\n")
        a, _ = self.build(files)
        self.assertNotIn("NODE_TEST_CONTEXT", a.env_vars)
        self.assertNotIn("ELECTRON_RUN_AS_NODE", a.env_vars)

    def test_wrapper_name_follows_plugin(self):
        _, dst = self.build(self.note_plugin())
        self.assertTrue((dst / "scripts" / "note-taker.sh").is_file())
        self.assertTrue((dst / "scripts" / "note-taker.cmd").is_file())

    def test_host_only_plugin_is_marked_unrunnable(self):
        """回归：v0.1 对 0 命令的插件照样生成引用 bili.sh 的 SKILL.md。"""
        a, dst = self.build({
            "package.json": json.dumps({"name": "dsh-host-only", "version": "0.1.0"}),
            "lib/svc.js": "export default (ctx) => { ctx.effect(() => () => {}); };\n",
        })
        self.assertFalse(a.runnable)
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("不要安装", skill)
        self.assertNotIn("怎么跑", skill)
        self.assertNotIn("scripts/", skill)
        self.assertEqual(list((dst / "scripts").glob("*")), [])

    def test_missing_everything_is_unrunnable(self):
        a, _ = self.build({
            "package.json": json.dumps({"name": "dsh-ghost"}),
            "tools/cli.mjs": (
                "const map = {\n"
                "  a: ['gone/a.mjs', '一'],\n"
                "  b: ['gone/b.mjs', '二'],\n"
                "};\n"),
        })
        self.assertEqual(len(a.commands), 2)
        self.assertEqual(len(a.missing), 2)
        self.assertFalse(a.runnable)

    def test_description_trigger_not_bili(self):
        _, dst = self.build(self.note_plugin())
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("note-taker 相关功能", skill)

    def test_defaults_snippet_extracted(self):
        files = self.note_plugin()
        files["lib/client.js"] = (
            "export function s(ctx) { ctx.effect(() => () => {}); }\n"
            "const DEFAULTS = { dir: '~/notes', autoSave: true };\n")
        _, dst = self.build(files)
        setup = (dst / "references" / "setup.md").read_text(encoding="utf-8")
        self.assertIn("const DEFAULTS = {", setup)
        self.assertIn("autoSave", setup)

    def test_setup_uses_real_config_name(self):
        files = self.note_plugin()
        files["lib/store.js"] = "const p = 'note-taker.config.json';\n"
        _, dst = self.build(files)
        setup = (dst / "references" / "setup.md").read_text(encoding="utf-8")
        self.assertIn("note-taker.config.json", setup)


# ---------------------------------------------------------------- 凭据路径


class TestCredentialPath(unittest.TestCase):
    def test_literal_path_in_comment(self):
        blob = " * 凭据（cookie）单独写到 ~/.bili-ai/credentials.json，且永远不回显。"
        self.assertEqual(m.detect_credential_path(blob), "~/.bili-ai/credentials.json")

    def test_join_form(self):
        blob = "const f = path.join(os.homedir(), '.app', 'token.json')"
        self.assertEqual(m.detect_credential_path(blob), "~/.app/token.json")

    def test_data_dir_not_mistaken_for_credential(self):
        """回归：只看第一段路径会把 `~/.dsh` 数据目录当成凭据文件。"""
        blob = "const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')"
        self.assertIsNone(m.detect_credential_path(blob))

    def test_no_credential_mentioned(self):
        self.assertIsNone(m.detect_credential_path("const x = 1;"))


# ---------------------------------------------------------------- 多运行时


class TestRuntime(PluginCase):
    def test_python_plugin(self):
        a, dst = self.build({
            "pyproject.toml": "[project]\nname='dsh-py'\n",
            "cli.py": (
                "COMMANDS = {\n"
                "    'list': ['cmd_list.py', '列出全部'],\n"
                "    'add': ['cmd_add.py', '新增一条'],\n"
                "}\n"),
            "cmd_list.py": "",
            "cmd_add.py": "",
        })
        self.assertEqual(a.runtime, "python")
        sh = (dst / "scripts" / "py.sh").read_text(encoding="utf-8")
        self.assertIn("PYTHON_BIN", sh)
        self.assertNotIn("NODE_BIN", sh)

    def test_ts_only_flagged(self):
        a, dst = self.build({
            "package.json": json.dumps({"name": "dsh-ts"}),
            "src/cli.ts": (
                "const map = {\n"
                "  list: ['src/commands/list.ts', '列出'],\n"
                "  show: ['src/commands/show.ts', '查看'],\n"
                "};\n"),
            "src/commands/list.ts": "",
            "src/commands/show.ts": "",
        })
        self.assertTrue(a.ts_only)
        self.assertFalse(a.runnable)
        skill = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("TypeScript", skill)


if __name__ == "__main__":
    unittest.main(verbosity=2)
