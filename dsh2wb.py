#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh2wb —— DSH(Cordis) 插件 → WorkBuddy Skill 转换器

    python dsh2wb.py <插件目录|git地址> [-o 输出目录] [--name 名字] [--vendor copy|link|none]

为什么需要它
------------
DSH 插件是「宿主 + 生命周期」模型：靠 ctx.effect / ctx.webServer / ctx.connection 挂在
DeepSeek Harness 进程里，有设置页、有调度器、有客户端注入。
WorkBuddy Skill 是「提示词 + 可执行脚本」模型：没有宿主 API，agent 直接跑 bash。

两者唯一的交集是 **CLI**。所以转换的核心判断只有一条：
    这个插件的能力，有多少是脱离宿主也能跑的纯命令行？

- 有 CLI 的（tools/*.mjs 带命令分发表）→ 正常转，命令一条条映射
- 只有宿主半边的（ctx.xxx 注册的 tool / 设置页 / 调度器）→ 转不了，如实列进 porting.md
  硬转只会得到一个「文档写了但跑不起来」的假 skill，比不转更糟

产物
----
    <out>/<name>/
        SKILL.md              frontmatter + 何时用 + 命令速查 + 安全约定
        references/commands.md   从源码解析出的完整命令表
        references/porting.md    能力对照：转了哪些、丢了哪些、为什么
        references/setup.md      首次配置（凭据 / 依赖 / 环境变量）
        scripts/<cli>.sh|.cmd    包装脚本（钉住 DSH_HOME 等环境变量）
        data/                    插件运行时数据目录（原 ~/.dsh 的位置）
        vendor/<原仓库>           插件本体（copy 或 link）

用法示例
--------
    python dsh2wb.py https://github.com/Alterssss/dsh-bili-whale -o ./out
    python dsh2wb.py ./repo --vendor link
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 数据结构


@dataclass
class Command:
    """CLI 子命令。"""
    name: str
    target: str          # 相对路径，如 commands/comment.mjs
    desc: str
    kind: str = "unknown"  # read / write / meta


@dataclass
class Analysis:
    src: Path
    pkg: dict
    name: str
    version: str
    desc: str
    cli_entry: Path | None = None
    commands: list[Command] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    host_files: list[str] = field(default_factory=list)
    host_caps: list[str] = field(default_factory=list)
    deps: dict = field(default_factory=dict)
    docs: list[str] = field(default_factory=list)
    has_client: bool = False
    has_patch: bool = False

    @property
    def fs_name(self) -> str:
        return self.name if self.name.startswith("dsh-") else f"dsh-{self.name}"

    @property
    def vendor_dir(self) -> str:
        """vendor/ 下的子目录名。用包名，不用克隆目录名 —— 否则 cp 出来叫 `repo`。"""
        return re.sub(r"[^\w.\-]", "-", self.name)


# ---------------------------------------------------------------- 解析


# map 里的条目：'comment-del': ['commands/comment-del.mjs', '说明…'],
_RE_MAP_ENTRY = re.compile(
    r"""^\s*        # 缩进
        (?:'([^']+)'|"([^"]+)"|([A-Za-z_][\w-]*))   # 命令名
        \s*:\s*\[\s*
        (?:'([^']+)'|"([^"]+)")                      # 目标文件
        \s*,\s*
        (?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")  # 说明
        \s*,?\s*\]""",
    re.X,
)

# 分区标记：好插件会在命令表里用注释把读写分开，比如
#   // —— 读（不需要写操作，安全） ——
#   // —— 写（会真的发出去；都必须显式 --dry 或 --go） ——
# 这是作者自己给的权威信号，比任何关键词启发式都准，优先用它。
_RE_SECTION = re.compile(r"——\s*([^—\n]{1,30}?)\s*——")
# 只在分区标题的**第一个词**里判断，别扫整句。
# 反例：「读（不需要写操作，安全）」整句里同时有「读」和「写」，
# 按包含关系匹配会先命中「写」，把整张只读表标成写操作。
_SECTION_KIND = (
    ("读", "read"), ("写", "write"), ("排查", "meta"),
    ("诊断", "meta"), ("检", "meta"),
)

# 没有分区时的兜底关键词。宁可判 write（要求 --dry），也不要把写操作误判成只读。
_WRITE_HINTS = (
    "发一条", "发布", "发送", "删除", "投稿", "私信", "上传", "发送",
    "comment-add", "post /", "send", "--go",
)
# 无副作用关键词：优先级高于写词
_READ_HINTS = (
    "只读", "导出", "复核", "查", "看", "列出", "只打印", "--dry", "状态",
    "体检", "抽样", "汇总", "还原", "统计", "确认", "盯", "等新回复",
)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_cli_entry(src: Path) -> tuple[Path | None, list[Command]]:
    """找一个带命令分发表的 .mjs/.js 入口，返回 (入口, 命令列表)。

    判据：文件里出现 `const map = {` 且至少解析出 3 条 [文件, 说明] 数组。
    不猜、不用文件名硬匹配 —— 换个插件目录结构就失效的启发式没有价值。
    """
    best: tuple[Path | None, list[Command]] = (None, [])
    cands = list(src.glob("tools/*.mjs")) + list(src.glob("tools/*.js"))
    cands += list(src.glob("bin/*")) + list(src.glob("*.mjs"))
    for f in cands:
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "const map = {" not in text and "const COMMANDS" not in text:
            continue
        cmds: list[Command] = []
        section: str | None = None
        for line in text.splitlines():
            # 先更新当前分区（注释里写着「读」还是「写」）
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("#"):
                sm = _RE_SECTION.search(stripped)
                if sm:
                    # 只取括号前的头一个词：读 / 写 / 排查
                    head = re.split(r"[（(]", sm.group(1))[0].strip()
                    section = None
                    for key, kind in _SECTION_KIND:
                        if head.startswith(key):
                            section = kind
                            break
            m = _RE_MAP_ENTRY.match(line) or _RE_MAP_ENTRY.match("  " + line)
            if not m:
                continue
            name = m.group(1) or m.group(2) or m.group(3)
            target = m.group(4) or m.group(5)
            desc = (m.group(6) or m.group(7) or "").strip()
            desc = desc.encode().decode("unicode_escape") if "\\u" in desc else desc
            cmd = Command(name=name, target=target, desc=desc)
            cmd.kind = section or "unknown"   # 分区优先，先占位
            cmds.append(cmd)
        if len(cmds) > len(best[1]):
            best = (f, cmds)
    return best


def classify(cmd: Command) -> str:
    """分区没给答案时，用关键词兜底。"""
    if cmd.kind in ("read", "write", "meta"):
        return cmd.kind
    blob = f"{cmd.name} {cmd.desc}".lower()
    if any(k in blob for k in _READ_HINTS):
        return "read"
    if any(k in blob.lower() for k in _WRITE_HINTS):
        return "write"
    return "meta"


def scan_host_side(src: Path) -> tuple[list[str], list[str], bool, bool]:
    """找出只能在 DSH 宿主里跑的部分。"""
    host_files, caps = [], set()
    has_client = (src / "lib" / "client.js").exists() or (src / "lib" / "client.ts").exists()
    has_patch = (src / "cordis.patch.yml").exists()
    for f in list(src.glob("lib/**/*.js")) + list(src.glob("lib/**/*.ts")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        hits = set(re.findall(r"ctx\.([a-zA-Z]+)", text))
        if hits:
            host_files.append(str(f.relative_to(src)))
            caps |= hits
    # 已知 ctx API → 人话
    _MAP = {
        "effect": "生命周期钩子（onUnload 清理）",
        "webServer": "注册 HTTP 路由 / 设置页后端",
        "connection": "回环请求信任栅栏",
        "get": "读取已挂载的服务实例",
        "on": "事件订阅",
        "inject": "依赖注入",
        "plugin": "动态挂载子插件",
        "logger": "宿主日志",
    }
    human = sorted({_MAP.get(c, f"ctx.{c}") for c in caps})
    return host_files, human, has_client, has_patch


def collect_env(src: Path) -> list[str]:
    names = set()
    for f in list(src.glob("**/*.mjs")) + list(src.glob("**/*.js")):
        if "node_modules" in str(f) or "/tests/" in str(f).replace("\\", "/"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        names |= set(re.findall(r"process\.env\.([A-Z][A-Z0-9_]{2,})", text))
    # 过滤掉通用的、平台自带的
    skip = {"NODE_ENV", "NODE_OPTIONS", "PATH", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"}
    return sorted(names - skip)


def analyze(src: Path, name_override: str | None) -> Analysis:
    pkg = _read_json(src / "package.json")
    entry, cmds = find_cli_entry(src)
    for c in cmds:
        c.kind = classify(c)
    host_files, host_caps, has_client, has_patch = scan_host_side(src)
    docs = [str(p.relative_to(src)) for p in sorted((src / "docs").glob("*.md"))]
    if (src / "tools" / "docs").is_dir():
        docs += [str(p.relative_to(src)) for p in sorted((src / "tools" / "docs").glob("*.md"))]
    return Analysis(
        src=src,
        pkg=pkg,
        name=name_override or pkg.get("name") or src.name,
        version=str(pkg.get("version") or "0.0.0"),
        desc=(pkg.get("description") or "").strip(),
        cli_entry=entry,
        commands=cmds,
        env_vars=collect_env(src),
        host_files=host_files,
        host_caps=host_caps,
        deps=pkg.get("dependencies") or {},
        docs=docs,
        has_client=has_client,
        has_patch=has_patch,
    )


# ---------------------------------------------------------------- 生成


def gen_skill_md(a: Analysis) -> str:
    rel = f"vendor/{a.vendor_dir}"
    reads = [c for c in a.commands if c.kind == "read"]
    writes = [c for c in a.commands if c.kind == "write"]
    metas = [c for c in a.commands if c.kind == "meta"]

    trigger = "、".join(c.name for c in a.commands[:10])
    # 原始 description 里往往写着「设置有 XX 设置页」「与 DSH 配置共用」这类
    # 转换后已经不成立的话。先摘掉括号里的宿主说明，再逐句过滤。
    _DEAD = ("DSH", "设置页", "配置页", "客户端", "插件挂载")
    scrubbed = re.sub(r"[（(][^）)]*[）)]",
                      lambda m: "" if any(k in m.group(0) for k in _DEAD) else m.group(0),
                      a.desc)
    kept = [seg.strip() for seg in re.split(r"[。；]\s*", scrubbed)
            if seg.strip() and not any(k in seg for k in _DEAD)]
    core = "。".join(kept).rstrip("。")
    if not core:
        # 整段都沾宿主，退一步：只留第一句（通常是「这插件干什么」），删掉宿主词
        body = re.sub(r"DSH\s*里的\s*agent", "agent", scrubbed)
        core = re.split(r"[。；]", body)[0].replace("DSH", "").strip()[:140].strip("：: ，,")
    if not core:
        core = a.name
    desc_lines = [
        f"{core}。",
        f"（由 DSH 插件 {a.name} v{a.version} 转换：命令实现原样保留，宿主设置页与调度器已去掉，"
        f"数据目录改为 skill 内的 data/。）",
        f"共 {len(a.commands)} 条命令：只读 {len(reads)} / 写 {len(writes)} / 其他 {len(metas)}。",
        f"当用户提到 B站/bilibili 的自动化操作，或需要 {trigger} 这类命令时使用。",
    ]
    desc = " ".join(desc_lines)

    L: list[str] = []
    L.append("---")
    L.append(f"name: {a.fs_name}")
    L.append("description: |")
    for line in desc_lines:
        L.append(f"  {line}")
    L.append(f"trigger: {trigger}")
    L.append("agent_created: true")
    L.append("---")
    L.append("")
    L.append(f"# {a.name}（DSH → WorkBuddy 转换版）")
    L.append("")
    L.append(f"源仓库 `{a.name}` v{a.version}，MIT/见源 LICENSE。**命令实现一行没改**，"
             f"只重写了外壳：DSH 宿主 API 换成 bash 直接调用，配置目录搬到 `data/`。")
    L.append("")
    L.append("## 怎么跑")
    L.append("")
    L.append("**只用包装脚本，不要直接 `node` 源码。** 包装脚本负责钉住数据目录，"
             "绕过它的话插件会去写 `~/.dsh/`，两套配置立刻分叉。")
    L.append("")
    L.append("```bash")
    L.append(f'SKILL_DIR="$HOME/.workbuddy/skills/{a.fs_name}"   # 会话里没定义就先设这一行')
    L.append(f'bash "$SKILL_DIR/scripts/bili.sh" help          # 看全部命令')
    L.append(f'bash "$SKILL_DIR/scripts/bili.sh" session       # 先确认是哪个号')
    L.append("```")
    L.append("")

    if writes:
        L.append("## 安全约定（硬规则，先读完再动手）")
        L.append("")
        L.append("写操作命令一共 "
                 f"{len(writes)} 条：**" + "、".join(c.name for c in writes) + "**。")
        L.append("")
        for i, rule in enumerate([
            "**先 `session` 确认凭据属于哪个号。** 打错号是不可逆的。",
            "**写操作一律先 `--dry`**（只打印将要发出的请求），确认无误再 `--go`。",
            "**一次一条。** 不批量、不定时轰炸、不多开并发。",
            "**写完立刻回读复核**（`thread` / `check-comment` / `history`），不要凭返回值假设成功。",
            "**遇到风控错误码立刻停手，一次都不许重试。** `-352` `-412` `601` `-401` `-403` 这类"
            "是风险评分判定，重试只会把评分推得更高，最后连登录都保不住。换 UA / 换 IP 更蠢。",
        ], 1):
            L.append(f"{i}. {rule}")
        L.append("")

    L.append("## 命令速查")
    L.append("")
    L.append("| 命令 | 说明 | 类型 |")
    L.append("|---|---|---|")
    icon = {"read": "只读", "write": "**写**", "meta": "其他"}
    for c in a.commands:
        d = c.desc.replace("|", "\\|")
        if len(d) > 70:
            d = d[:70] + "…"
        L.append(f"| `{c.name}` | {d} | {icon[c.kind]} |")
    L.append("")
    L.append("完整参数见 `references/commands.md`；能力取舍见 `references/porting.md`。")
    L.append("")

    if a.host_caps or a.has_client:
        L.append("## 这个版本没有的东西")
        L.append("")
        L.append("原插件里挂在 DSH 宿主上的部分**全部去掉了**，因为 WorkBuddy 没有对应的宿主 API：")
        L.append("")
        for cap in a.host_caps:
            L.append(f"- {cap}")
        if a.has_client:
            L.append("- 设置页面（客户端 section 注入）")
        L.append("")
        L.append("替代方案写在 `references/porting.md`，主要是用 WorkBuddy 的 automation 顶替调度器。")
        L.append("")
    return "\n".join(L)


def gen_commands_md(a: Analysis) -> str:
    L = [f"# {a.name} 命令参考", ""]
    L.append(f"源码解析自 `{a.cli_entry.relative_to(a.src) if a.cli_entry else 'N/A'}`"
             f"（v{a.version}）。**说明文字是源码里的原文，不是我重写的。**")
    L.append("")
    for kind, title in (("read", "只读命令（安全）"), ("write", "写命令（会真的发出去）"), ("meta", "其他")):
        group = [c for c in a.commands if c.kind == kind]
        if not group:
            continue
        L.append(f"## {title}")
        L.append("")
        for c in group:
            L.append(f"### `{c.name}`")
            L.append("")
            L.append(f"{c.desc}")
            L.append("")
            L.append(f"实现：`{c.target}`")
            L.append("")
            L.append("```bash")
            L.append(f'bash "${{SKILL_DIR}}/scripts/bili.sh" {c.name} --help')
            L.append("```")
            L.append("")
    L.append("## 原生帮助")
    L.append("")
    L.append("任何命令加 `--help` 都会打印它自己的参数说明 —— **以那个为准**，"
             "本文只是索引。源码里说明和实际参数不一致的情况确实存在（作者自己也记过这个 bug），"
             "所以参数以运行时输出为准。")
    L.append("")
    return "\n".join(L)


def gen_porting_md(a: Analysis) -> str:
    L = [f"# 转换报告：{a.name} v{a.version}", ""]
    L.append("## 结论")
    L.append("")
    if a.commands:
        L.append(f"**可转换。** CLI 层共 {len(a.commands)} 条命令，全部可用；"
                 f"宿主层 {len(a.host_files)} 个文件的能力丢失，已在下面列出。")
    else:
        L.append("**不可转换。** 没找到 CLI 分发入口，这个插件只有宿主半边"
                 "（设置页 / 调度器 / 客户端注入），WorkBuddy 无法承载。"
                 "硬转只会得到一个跑不起来的空壳。")
    L.append("")
    L.append("## 能力对照")
    L.append("")
    L.append("| DSH 里的东西 | 转换成什么 | 状态 |")
    L.append("|---|---|---|")
    L.append(f"| `tools/` CLI（{len(a.commands)} 命令） | `scripts/bili.sh` 包装调用 | 完整可用 |")
    if a.env_vars:
        L.append(f"| 配置目录 `~/.dsh` | 环境变量{'/'.join(a.env_vars[:2])} 指向 `data/` | 已隔离 |")
    if a.has_patch:
        L.append("| `cordis.patch.yml` 挂载声明 | 无需（不挂进程） | 丢弃 |")
    if a.has_client:
        L.append("| `lib/client.js` 设置页 | 手改 `data/*.json` | 丢弃 |")
    for cap in a.host_caps:
        L.append(f"| {cap} | 无对应物 | 丢弃 |")
    L.append("")
    if a.host_files:
        L.append("### 丢掉的文件")
        L.append("")
        for f in a.host_files:
            L.append(f"- `{f}`")
        L.append("")
    L.append("## 环境变量")
    L.append("")
    if a.env_vars:
        L.append("插件读这些变量，包装脚本已预设：")
        L.append("")
        L.append("| 变量 | 作用 | 本 skill 里的值 |")
        L.append("|---|---|---|")
        for v in a.env_vars:
            note = ""
            if "HOME" in v:
                note = "插件数据根目录"
            elif "CONFIG" in v:
                note = "配置文件路径"
            L.append(f"| `{v}` | {note or '插件自定义'} | `data/` 下的对应位置 |")
        L.append("")
        L.append("**注意：** 设了这些变量后，插件读写的是 skill 自己的 `data/`，"
                 "**与原来 DSH 里的那份配置彻底分家**。想共用就把变量指回去，但两边同时写会互相覆盖。")
    else:
        L.append("未发现自定义环境变量。")
    L.append("")
    L.append("## 依赖")
    L.append("")
    if a.deps:
        L.append("原插件 npm 依赖（未安装，需要时执行）：")
        L.append("")
        L.append("```bash")
        L.append(f"cd vendor/{a.vendor_dir} && npm install --omit=dev")
        L.append("```")
        L.append("")
        for k, v in a.deps.items():
            L.append(f"- `{k}` {v}")
        L.append("")
        L.append("**必须在 `vendor/` 里装，不能装在 skill 根目录** —— 源码用相对路径 import，"
                 "`node_modules` 位置不对就找不到模块。")
    else:
        L.append("无第三方依赖（纯 Node 内置模块）。")
    L.append("")
    L.append("## 还缺什么")
    L.append("")
    L.append("1. **凭据**：见 `setup.md`。插件的 cookie 存储在 `~/.bili-ai/credentials.json`，"
             "和 skill 的 `data/` 不是一回事，需要单独准备。")
    L.append("2. **调度器**：原插件有内置 scheduler（定时自主行为）。丢掉之后要用 WorkBuddy 的"
             "自动任务顶上，不要自己写 `while true` 循环。")
    L.append("3. **测试**：原仓库有 tests/，可以直接 `node --test` 跑，用来确认 vendor 目录完整。")
    L.append("")
    return "\n".join(L)


def extract_defaults(src: Path, host_files: list[str]) -> str | None:
    """把宿主源码里的 `const DEFAULTS = { … }` 原文摘出来。

    设置页丢了以后，用户没有别的办法知道配置项长什么样。这不是猜测 ——
    是源码里的字面量，原样搬进文档，让用户自己按需改写成 JSON。
    """
    for rel in host_files:
        f = src / rel
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        idx = text.find("const DEFAULTS = {")
        if idx < 0:
            continue
        depth, end = 0, None
        for i in range(idx + len("const DEFAULTS = "), len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            return text[idx:end].strip()
    return None


def gen_setup_md(a: Analysis) -> str:
    L = [f"# {a.name} 首次配置", ""]
    L.append("## 1. 依赖")
    L.append("")
    L.append("```bash")
    L.append(f'cd "$SKILL_DIR/vendor/{a.vendor_dir}"')
    L.append("npm install --omit=dev    # 只有 qrcode 这类小包，不用 dev 依赖")
    L.append("node --test               # 原仓库自带测试，验证 vendor 完整")
    L.append("```")
    L.append("")
    L.append("## 2. 凭据")
    L.append("")
    L.append("```bash")
    L.append('bash "$SKILL_DIR/scripts/bili.sh" deps     # 依赖体检')
    L.append('bash "$SKILL_DIR/scripts/bili.sh" session  # 确认当前凭据是哪个号')
    L.append("```")
    L.append("")
    L.append("`deps` 会告诉你缺什么。凭据文件在 `~/.bili-ai/credentials.json`"
             "（**不在 skill 目录里**，因为它是按 OS 用户存的，跟插件根目录无关）。")
    L.append("")
    L.append("## 3. 数据目录")
    L.append("")
    L.append("插件的配置/日记/日志全部落在 `data/`，对应原来 `~/.dsh/` 的位置：")
    L.append("")
    L.append("```")
    L.append("data/")
    L.append("  bili-whale.config.json   主配置")
    L.append("  ...                      插件自己创建的子目录")
    L.append("```")
    L.append("")
    L.append("要备份就整个 `data/` 打包，别只拷 json。")
    L.append("")
    defaults = extract_defaults(a.src, a.host_files)
    if defaults:
        L.append("## 4. 配置模板（设置页的替代品）")
        L.append("")
        L.append("原插件靠设置页写配置，这半边丢了。下面是**宿主源码里 `DEFAULTS` 的字面量**，"
                 "原样摘出，不是我的推测：")
        L.append("")
        L.append("```js")
        L.append(defaults)
        L.append("```")
        L.append("")
        L.append("要用就照这个结构手写 `data/bili-whale.config.json`（去掉注释、键名加引号），"
                 "或直接调对应的 CLI 让它自己落盘（比如 `night plan`）。**不确定的项先不写**，"
                 "大多数命令缺配置时会用内置默认值，只有少数（生物钟、通知）必须显式配。")
        L.append("")
    if a.docs:
        L.append("## 源码自带文档")
        L.append("")
        L.append("这些是原作者的文档，比转换出的说明更权威：")
        L.append("")
        for d in a.docs:
            L.append(f"- `vendor/{a.vendor_dir}/{d}`")
        L.append("")
    return "\n".join(L)


def gen_wrapper_sh(a: Analysis, cli_rel: str) -> str:
    return f"""#!/usr/bin/env bash
# {a.name} 包装器（dsh2wb 生成）
# 作用只有两个：钉住数据目录；找到 node。不为别的。
set -euo pipefail

# pwd -W 拿 Windows 风格路径（G:/…）。Git Bash 里普通 pwd 给 /g/…，
# 传给原生 node.exe 时 MSYS 会把它当"当前盘根下的相对路径"重写成 G:\\g\\…，
# 表现就是 Cannot find module 'G:\\g\\…' —— 这个坑只在 Windows 上出现。
_here="$(dirname "${{BASH_SOURCE[0]}}")"
SKILL_DIR="$(cd "$_here/.." && (pwd -W 2>/dev/null || pwd))"
VENDOR="$SKILL_DIR/vendor/{a.vendor_dir}"
export DSH_HOME="$SKILL_DIR/data"
export BILI_WHALE_CONFIG="$SKILL_DIR/data/bili-whale.config.json"

mkdir -p "$DSH_HOME"

# node 解析：优先插件自带的，其次 PATH
NODE_BIN="${{NODE_BIN:-}}"
if [ -z "$NODE_BIN" ]; then
  if command -v node >/dev/null 2>&1; then
    NODE_BIN="$(command -v node)"
  elif [ -x "D:/New Folder/node.exe" ]; then
    NODE_BIN="D:/New Folder/node.exe"
  elif [ -x "/c/Program Files/nodejs/node.exe" ]; then
    NODE_BIN="/c/Program Files/nodejs/node.exe"
  else
    echo "找不到 node。设 NODE_BIN 环境变量指向 node.exe" >&2
    exit 127
  fi
fi

if [ ! -f "$VENDOR/{cli_rel}" ]; then
  echo "vendor 不完整：$VENDOR/{cli_rel} 不存在" >&2
  echo "先跑：cd vendor/{a.src.name} && npm install --omit=dev" >&2
  exit 1
fi

exec "$NODE_BIN" "$VENDOR/{cli_rel}" "$@"
"""


def gen_wrapper_cmd(a: Analysis, cli_rel: str) -> str:
    win_cli = cli_rel.replace("/", "\\")
    return f"""@echo off
rem {a.name} 包装器（dsh2wb 生成）
setlocal
set "SKILL_DIR=%~dp0.."
set "SKILL_DIR=%SKILL_DIR:\\..=%"
set "VENDOR=%SKILL_DIR%\\vendor\\{a.vendor_dir}"
set "DSH_HOME=%SKILL_DIR%\\data"
set "BILI_WHALE_CONFIG=%SKILL_DIR%\\data\\bili-whale.config.json"
if not exist "%DSH_HOME%" mkdir "%DSH_HOME%"

set "NODE_BIN=node"
where node >nul 2>nul || set "NODE_BIN=D:\\New Folder\\node.exe"

if not exist "%VENDOR%\\{win_cli}" (
  echo vendor incomplete: %VENDOR%\\{win_cli} 1>&2
  exit /b 1
)
"%NODE_BIN%" "%VENDOR%\\{win_cli}" %*
"""


def gen_readme(a: Analysis) -> str:
    return f"""# {a.name} skill

由 `dsh2wb.py` 从 DSH 插件 `{a.name}` v{a.version} 生成。

```bash
bash scripts/bili.sh help
```

- `SKILL.md` —— agent 读的入口
- `references/` —— 命令表 / 转换报告 / 配置说明
- `vendor/` —— 原插件源码（未改动）
- `data/` —— 插件运行时数据

重新生成会覆盖 `SKILL.md` 和 `references/`，**不会动 `data/` 和 `vendor/`**。
"""


def build(a: Analysis, out_root: Path, vendor: str, force: bool) -> Path:
    dst = out_root / a.fs_name
    skill_md = dst / "SKILL.md"
    if skill_md.exists() and not force:
        raise SystemExit(f"目标已存在：{dst}\n加 --force 覆盖（data/ 和 vendor/ 不会被删）")
    for sub in ("references", "scripts", "data"):
        (dst / sub).mkdir(parents=True, exist_ok=True)

    if not a.commands:
        # 只有宿主半边 —— 仍然生成，但如实说明转不了
        a.cli_entry = a.cli_entry
    cli_rel = str(a.cli_entry.relative_to(a.src)).replace("\\", "/") if a.cli_entry else "tools/cli.mjs"

    (dst / "SKILL.md").write_text(gen_skill_md(a), encoding="utf-8")
    (dst / "README.md").write_text(gen_readme(a), encoding="utf-8")
    (dst / "references" / "commands.md").write_text(gen_commands_md(a), encoding="utf-8")
    (dst / "references" / "porting.md").write_text(gen_porting_md(a), encoding="utf-8")
    (dst / "references" / "setup.md").write_text(gen_setup_md(a), encoding="utf-8")

    if a.commands:
        sh = dst / "scripts" / "bili.sh"
        sh.write_text(gen_wrapper_sh(a, cli_rel), encoding="utf-8")
        sh.chmod(0o755)
        (dst / "scripts" / "bili.cmd").write_text(gen_wrapper_cmd(a, cli_rel), encoding="utf-8")

    # vendor
    vdst = dst / "vendor" / a.vendor_dir
    if vendor != "none" and not vdst.exists():
        vdst.parent.mkdir(parents=True, exist_ok=True)
        if vendor == "link":
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(vdst), str(a.src)],
                               check=False, capture_output=True)
                if not vdst.exists():
                    shutil.copytree(a.src, vdst, ignore=shutil.ignore_patterns(".git", "node_modules"))
            else:
                os.symlink(a.src, vdst, target_is_directory=True)
        else:
            shutil.copytree(
                a.src, vdst,
                ignore=shutil.ignore_patterns(".git", "node_modules", ".githooks", "*.log"),
            )
    return dst


# ---------------------------------------------------------------- main


def resolve_source(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    if re.match(r"^(https?://|git@)", arg):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / ("dsh2wb-" + re.sub(r"\W+", "-", arg)[-40:])
        if not tmp.exists():
            subprocess.run(["git", "clone", "--depth", "1", arg, str(tmp)], check=True)
        return tmp.resolve()
    raise SystemExit(f"源不存在：{arg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH 插件 → WorkBuddy Skill")
    ap.add_argument("source", help="插件目录 或 git 仓库地址")
    ap.add_argument("-o", "--out", default="./out", help="输出目录")
    ap.add_argument("--name", help="覆盖 skill 名（默认用 package.json 的 name）")
    ap.add_argument("--vendor", choices=("copy", "link", "none"), default="copy",
                    help="插件源码怎么放（link 用 junction，省空间）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true", help="只输出分析结果，不生成")
    args = ap.parse_args()

    src = resolve_source(args.source)
    a = analyze(src, args.name)

    if args.json:
        print(json.dumps({
            "name": a.name, "version": a.version, "desc": a.desc,
            "cli_entry": str(a.cli_entry) if a.cli_entry else None,
            "commands": [{"name": c.name, "kind": c.kind, "desc": c.desc} for c in a.commands],
            "env_vars": a.env_vars, "host_files": a.host_files,
            "host_caps": a.host_caps, "deps": a.deps,
        }, ensure_ascii=False, indent=2))
        return 0

    out_root = Path(args.out).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    dst = build(a, out_root, args.vendor, args.force)

    r = sum(1 for c in a.commands if c.kind == "read")
    w = sum(1 for c in a.commands if c.kind == "write")
    m = len(a.commands) - r - w
    print(f"源      : {src}")
    print(f"插件    : {a.name} v{a.version}")
    print(f"CLI入口 : {a.cli_entry.relative_to(src) if a.cli_entry else '未找到（可能只有宿主半边）'}")
    print(f"命令    : {len(a.commands)} 条（只读 {r} / 写 {w} / 其他 {m}）")
    print(f"宿主丢失: {len(a.host_files)} 文件 · {len(a.host_caps)} 项能力")
    print(f"输出    : {dst}")
    if not a.commands:
        print("\n[警告] 没解析到 CLI 命令，转换产出的 skill 不可运行。")
    return 0 if a.commands else 2


if __name__ == "__main__":
    sys.exit(main())
