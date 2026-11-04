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

- 有 CLI 的 → 正常转，命令一条条映射
- 只有宿主半边的（ctx.xxx 注册的 tool / 设置页 / 调度器）→ 转不了，如实列进 porting.md
  硬转只会得到一个「文档写了但跑不起来」的假 skill，比不转更糟

产物
----
    <out>/<name>/
        SKILL.md                 frontmatter + 何时用 + 命令速查 + 安全约定
        references/commands.md   从源码解析出的完整命令表
        references/porting.md    能力对照：转了哪些、丢了哪些、为什么
        references/setup.md      首次配置（凭据 / 依赖 / 环境变量）
        scripts/<slug>.sh|.cmd   包装脚本（钉住数据目录）
        data/                    插件运行时数据目录（原宿主数据目录的位置）
        vendor/<原仓库>           插件本体（copy 或 link）

设计原则
--------
**所有产物内容都由探测结果决定，没有一处假设。**

早期版本把「B站 / 扫码登录 / --dry --go / 风控错误码 / 调度器」写死在模板里，
换个插件转出来满篇胡话。现在每一段都问一句「这个插件真的有这东西吗」，
探测不到就不写那一段。

用法示例
--------
    python dsh2wb.py https://github.com/Alterssss/dsh-bili-whale -o ./out
    python dsh2wb.py ./repo --vendor link
    python dsh2wb.py ./repo --json          # 只看分析，不生成

退出码
------
    0  可转换（有命令且至少一条实现文件存在）
    2  不可转换（无命令 / 只有 TS / 实现文件全缺失）
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

__version__ = "0.2.0"

BS = "\\"  # Windows 路径分隔符，生成 .cmd 时用

# ---------------------------------------------------------------- 常量

_CODE_EXTS = {".mjs", ".cjs", ".js", ".jsx", ".py", ".sh", ".bash"}
_CODE_EXTS_WITH_TS = _CODE_EXTS | {".ts", ".tsx"}

# 扫描时跳过的目录：依赖、产物、缓存
_SKIP_DIRS = {
    "node_modules", ".git", ".hg", ".svn", "dist", "build", "out", "output",
    "coverage", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", "vendor", ".next", ".cache",
}
# 测试目录：算代码但不参与命令表竞争（测试里常有假 map）
_TEST_MARKERS = {"test", "tests", "__tests__", "spec", "specs", "fixtures", "examples"}

_MAX_FILE_BYTES = 1_000_000


# ---------------------------------------------------------------- 数据结构


@dataclass
class Command:
    """CLI 子命令。"""
    name: str
    target: str            # 相对路径，如 commands/comment.mjs
    desc: str
    kind: str = "unknown"  # read / write / meta
    exists: bool = True    # target 文件在源码里是否真的存在


@dataclass
class Analysis:
    src: Path
    pkg: dict

    # 身份
    name: str = ""
    version: str = "0.0.0"
    desc: str = ""
    license_id: str | None = None

    # CLI
    runtime: str = "unknown"       # node / python / shell / unknown
    cli_entry: Path | None = None
    commands: list[Command] = field(default_factory=list)
    ts_only: bool = False          # 只找到 TS 命令表（跑不了）

    # 宿主侧
    host_files: list[str] = field(default_factory=list)
    host_caps: list[str] = field(default_factory=list)
    has_client: bool = False
    has_patch: bool = False

    # 探测到的特征（决定模板里写什么）
    env_vars: list[str] = field(default_factory=list)
    config_names: list[str] = field(default_factory=list)
    has_scheduler: bool = False
    has_credentials: bool = False
    has_network_write: bool = False
    has_dry_flag: bool = False
    has_tests: bool = False
    cred_path: str | None = None

    # 其他
    deps: dict = field(default_factory=dict)
    docs: list[str] = field(default_factory=list)
    defaults_snippet: str | None = None

    # -------------------------------------------------- 派生属性

    @property
    def slug(self) -> str:
        """`dsh-bili-whale` → `bili-whale`。用来命名包装脚本和配置文件。"""
        s = self.name[4:] if self.name.startswith("dsh-") else self.name
        s = re.sub(r"[^\w.\-]", "-", s).strip("-")
        return s or "plugin"

    @property
    def fs_name(self) -> str:
        return self.name if self.name.startswith("dsh-") else f"dsh-{self.name}"

    @property
    def vendor_dir(self) -> str:
        """vendor/ 下的子目录名。用包名，不用克隆目录名 —— 否则 cp 出来叫 `repo`。"""
        return re.sub(r"[^\w.\-]", "-", self.name)

    @property
    def wrapper(self) -> str:
        return self.slug

    @property
    def counts(self) -> tuple[int, int, int]:
        r = sum(1 for c in self.commands if c.kind == "read")
        w = sum(1 for c in self.commands if c.kind == "write")
        return r, w, len(self.commands) - r - w

    @property
    def missing(self) -> list[Command]:
        return [c for c in self.commands if not c.exists]

    @property
    def runnable(self) -> bool:
        """有命令表、有可执行入口、且至少一条的实现文件真的存在。"""
        return (self.cli_entry is not None
                and bool(self.commands)
                and any(c.exists for c in self.commands))


# ---------------------------------------------------------------- 文本工具


def _js_unescape(s: str) -> str:
    """还原 JS 字符串字面量里的转义序列，中文原样保留。

    不能用 `s.encode().decode("unicode_escape")` —— 那会把 UTF-8 字节按
    latin-1 解释，含 \\u 的描述整条变乱码。这里手写状态机，只处理反斜杠，
    其余字符直接透传。
    """
    if "\\" not in s:
        return s
    plain = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'",
             '"': '"', "/": "/", "b": "\b", "f": "\f", "0": "\0"}
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt in plain:
            out.append(plain[nxt])
            i += 2
            continue
        if nxt == "u" and i + 5 < n:
            try:
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        if nxt == "x" and i + 3 < n:
            try:
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _pyproject_meta(src: Path) -> dict:
    """从 pyproject.toml / setup.py 里摸 name / version / description。

    不引 tomllib 之外的依赖，也不用完整 TOML 解析 —— 只要这三个字段。
    （tomllib 是 3.11+ 才有，这里手抓正则，兼容面更宽。）
    """
    meta: dict = {}
    text = _read_text(src / "pyproject.toml")
    if not text:
        text = _read_text(src / "setup.py")
    if not text:
        return meta
    for key in ("name", "version", "description"):
        m = re.search(rf"""^\s*{key}\s*=\s*['"]([^'"\n]+)['"]""", text, re.M)
        if m:
            meta[key] = m.group(1)
    return meta


def _iter_code_files(src: Path, include_ts: bool = False, include_tests: bool = False):
    """遍历源码文件。跳过依赖 / 产物 / 缓存；ts 与测试按需包含。"""
    exts = _CODE_EXTS_WITH_TS if include_ts else _CODE_EXTS
    for p in sorted(src.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        try:
            rel = p.relative_to(src)
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
        except (ValueError, OSError):
            continue
        parts = {part.lower() for part in rel.parts[:-1]}
        if parts & _SKIP_DIRS:
            continue
        if not include_tests and parts & _TEST_MARKERS:
            continue
        yield p


# ---------------------------------------------------------------- 命令表解析


_SECTION_WORDS = (
    # 中文
    ("读", "read"), ("查", "read"), ("列", "read"), ("安全", "read"), ("只读", "read"),
    ("写", "write"), ("发", "write"), ("删", "write"), ("改", "write"), ("创", "write"),
    ("排查", "meta"), ("诊断", "meta"), ("检", "meta"), ("其他", "meta"), ("杂", "meta"),
    # 英文
    ("read", "read"), ("query", "read"), ("safe", "read"), ("list", "read"),
    ("get", "read"), ("view", "read"), ("inspect", "read"),
    ("write", "write"), ("mutat", "write"), ("destruct", "write"), ("send", "write"),
    ("post", "write"), ("creat", "write"), ("delet", "write"), ("updat", "write"),
    ("meta", "meta"), ("diag", "meta"), ("debug", "meta"), ("admin", "meta"),
    ("util", "meta"), ("other", "meta"),
)

# 关键词后面允许跟的常见后缀。`## 写操作` / `// 只读命令` 都能命中。
_SECTION_SUFFIXES = ("", "命令", "操作", "命令区", "区", "部分", "相关", "类", "组", "专用")

# 分区标题的几种写法，按可信度从高到低：
#   0  —— X ——        明确包裹，最可信
#   1  --- X ---      同上
#   2  // [X]         方括号标注
#   3  // X / ## X    纯注释行，最弱，判定最严（见 _section_kind 的 strict）
_RE_SECTIONS = (
    re.compile(r"——\s*([^—\n]{1,40}?)\s*——"),
    re.compile(r"-{2,}\s*([^-\n]{1,40}?)\s*-{2,}"),
    re.compile(r"^\s*(?:/{2,}|#{1,4})\s*\[\s*([^\]\n]{1,24})\s*\]"),
    re.compile(r"^\s*(?:/{2,}|#{1,4}|\*)\s*([^\n]{1,40})$"),
)


def _section_kind(line: str) -> str | None:
    """从一行注释里判断分区类型。返回 read / write / meta 或 None。

    两条约束都是踩过坑才加的：

    1. 只看标题的**第一个词**，不扫整句。反例「读（不需要写操作，安全）」
       整句同时含「读」和「写」，按子串匹配会命中「写」，把整张只读表标危险。
    2. 纯注释行（模式 3）必须「关键词(+常见后缀)」**完全相等**，不能用 startswith。
       否则一句普通注释 `// 列出全部命令` 就会因为以「列」开头而把后面所有命令
       标成只读 —— 这比漏判还糟。
    """
    for idx, pat in enumerate(_RE_SECTIONS):
        m = pat.search(line)
        if not m:
            continue
        head = m.group(1).strip()
        token = re.split(r"[（(\[：:，,\s\-—|/]", head)[0].strip().lower()
        if not token or len(token) > 12:
            continue
        strict = idx >= 2
        for word, kind in _SECTION_WORDS:
            if strict:
                if any(token == word + suf for suf in _SECTION_SUFFIXES):
                    return kind
            elif token == word or (len(token) <= 8 and token.startswith(word)):
                return kind
    return None


# 命令表条目。四种形态都吃：
#   name: ['file.mjs', '说明']
#   name: ["file.mjs"]
#   name: 'file.mjs'
#   name: { file: 'file.mjs', desc: '说明' }
_RE_ENTRY = re.compile(
    r"""^\s*
        (?:
            ['"](?P<qkey>[^'"\n]+)['"]
          | (?P<key>[A-Za-z_$][\w$\-]*)
        )
        \s*:\s*
        (?:
            \[
              \s*['"](?P<t_arr>[^'"\n]+)['"]
              (?:\s*,\s*['"](?P<d_arr>(?:[^'"\\\n]|\\.)*)['"])?
            |
            ['"](?P<t_str>[^'"\n]+)['"]
            |
            \{
              \s*(?:file|path|entry|module|script)\s*:\s*['"](?P<t_obj>[^'"\n]+)['"]
              (?:\s*,\s*(?:desc|description|help|about)\s*:\s*
                 ['"](?P<d_obj>(?:[^'"\\\n]|\\.)*)['"])?
        )
    """,
    re.X,
)

# 目标看着像不像一个可执行文件。把命令表和普通配置对象区分开。
_RE_TARGETISH = re.compile(r"\.(?:mjs|cjs|jsx?|tsx?|py|sh|bash|rb|pl|go|rs)$", re.I)


def _looks_like_target(s: str) -> bool:
    if not s or " " in s or len(s) > 200:
        return False
    return bool(_RE_TARGETISH.search(s)) or "/" in s


def parse_command_table(path: Path) -> list[Command]:
    """从一个文件里抽命令表。抽不到就返回空列表，不猜。"""
    text = _read_text(path)
    if ":" not in text:
        return []
    cmds: list[Command] = []
    section: str | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith(("//", "#", "/*", "*", "--", "'''", '"""')):
            kind = _section_kind(s)
            if kind:
                section = kind
            continue
        m = _RE_ENTRY.match(raw)
        if not m:
            continue
        key = m.group("qkey") or m.group("key")
        target = m.group("t_arr") or m.group("t_str") or m.group("t_obj")
        desc = m.group("d_arr") or m.group("d_obj") or ""
        if not key or not target or not _looks_like_target(target):
            continue
        cmds.append(Command(
            name=_js_unescape(key),
            target=target,
            desc=_js_unescape(desc).strip(),
            kind=section or "unknown",
        ))
    return cmds


def _target_exists(src: Path, entry: Path | None, target: str) -> bool:
    """target 是相对路径，可能相对仓库根，也可能相对入口文件所在目录。"""
    bases = [src]
    if entry is not None:
        bases.insert(0, entry.parent)
    for base in bases:
        try:
            if (base / target).is_file():
                return True
        except (OSError, ValueError):
            continue
    return False


def find_cli_entry(src: Path) -> tuple[Path | None, list[Command], bool]:
    """找一个带命令分发表的入口。

    不靠文件名、不靠变量名 —— 谁解析出的命令最多、且实现文件真的存在，谁就是入口。
    返回 (入口, 命令列表, 是否只找到 TS)。
    """
    best: tuple[Path | None, list[Command], int] = (None, [], -10 ** 9)
    ts_best: tuple[Path | None, list[Command], int] = (None, [], -10 ** 9)

    def consider(f: Path, into_ts: bool) -> None:
        nonlocal best, ts_best
        cmds = parse_command_table(f)
        if len(cmds) < 2:
            return
        for c in cmds:
            c.exists = _target_exists(src, f, c.target)
        # 打分：条数为主，缺失文件重罚（宁可选中「条数少但真能跑」的表）
        gaps = sum(0 if c.exists else 1 for c in cmds)
        score = len(cmds) * 10 - gaps * 25
        if into_ts:
            if score > ts_best[2]:
                ts_best = (f, cmds, score)
        elif score > best[2]:
            best = (f, cmds, score)

    for f in _iter_code_files(src):
        consider(f, False)
    if best[0] is None:
        # 兜底看一眼 TS：跑不了，但能告诉用户「东西在这儿，只是要编译」
        for f in _iter_code_files(src, include_ts=True):
            if f.suffix.lower() in (".ts", ".tsx"):
                consider(f, True)

    if best[0] is not None:
        return best[0], best[1], False
    if ts_best[0] is not None:
        return None, ts_best[1], True
    return None, [], False


# ---------------------------------------------------------------- 读写判定


# 没有分区时的兜底关键词。
# **写优先**：宁可把只读命令误判成写（多一道确认，代价是麻烦），
# 也不能把写操作误判成只读（代价是用户的账号）。
_WRITE_HINTS = (
    # 中文
    "发一条", "发布", "发送", "投递", "删除", "移除", "投稿", "私信", "上传",
    "新建", "创建", "修改", "更新", "写入", "关注", "取关", "点赞", "投币",
    "收藏", "评论", "回复", "转发", "下单", "提交", "清空", "重置",
    # 英文 / 参数
    "--go", "delete", "remove", "create", "update", "send", "upload",
    "write", "submit", "publish", "post", "insert", "drop", "rm ",
)

# 只读提示词。注意：不要放 `--dry` / `--go` —— 它们是**写命令的开关**，
# 出现在写命令的说明里会被误判成只读（v0.1 的真实 bug）。
_READ_HINTS = (
    # 中文
    "只读", "导出", "复核", "查看", "查询", "列出", "只打印", "状态", "体检",
    "排查", "抽样", "汇总", "还原", "统计", "阅读", "获取", "显示", "搜索",
    "检索", "浏览", "看看", "打印",
    # 英文
    "list", "show", "get", "read", "query", "status", "check", "search",
    "find", "fetch", "view", "inspect", "export", "dump", "info",
)


def classify(cmd: Command) -> str:
    """分区没给答案时，用关键词兜底。写优先。"""
    if cmd.kind in ("read", "write", "meta"):
        return cmd.kind
    blob = f"{cmd.name} {cmd.desc}".lower()
    if any(k in blob for k in _WRITE_HINTS):
        return "write"
    if any(k in blob for k in _READ_HINTS):
        return "read"
    return "meta"


# ---------------------------------------------------------------- 探测


_CTX_HUMAN = {
    "effect": "生命周期钩子（onUnload 清理）",
    "webServer": "注册 HTTP 路由 / 设置页后端",
    "connection": "回环请求信任栅栏",
    "get": "读取已挂载的服务实例",
    "on": "事件订阅",
    "inject": "依赖注入",
    "plugin": "动态挂载子插件",
    "logger": "宿主日志",
    "emit": "事件派发",
    "config": "宿主配置读写",
    "storage": "宿主存储",
    "http": "宿主 HTTP 通道",
    "fs": "宿主文件系统视图",
}

_RE_SCHED = re.compile(
    r"setInterval|node-cron|node-schedule|scheduleJob|\bcron\b|scheduler|"
    r"定时任务|每小时|每分钟|每隔\s*\d+|crontab",
    re.I,
)
_RE_CRED = re.compile(
    r"cookie|credential|passwd|password|api[_-]?key|access[_-]?token|"
    r"sessions?\.json|login|signin|扫码|登录|授权|密钥",
    re.I,
)
_RE_NETWRITE = re.compile(
    r"method\s*:\s*['\"]POST|['\"]POST['\"]|\.post\s*\(|requests\.post|"
    r"urlopen\s*\([^)]*data\s*=|http\.request\s*\(",
    re.I,
)
_RE_CONFIG_JSON = re.compile(r"""['"]([\w.\-]{2,60}\.json)['"]""")
_CONFIG_NOISE = {
    "package.json", "package-lock.json", "tsconfig.json", "jsconfig.json",
    "composer.json", "manifest.json", "renovate.json", "vercel.json",
}

# 凭据落点：join(homedir(), '.bili-ai', 'credentials.json') 这类写法。
# 探得到就如实写进 setup.md，探不到就说探不到 —— 不编一个像样的路径糊弄人。
_RE_HOME_JOIN = re.compile(
    r"(?:join|resolve)\s*\(\s*(?:homedir\(\)|os\.homedir\(\)|"
    r"expanduser\(\s*['\"]~['\"]\s*\)|process\.env\.HOME)"
    r"((?:\s*,\s*['\"][^'\"\n]{1,60}['\"])+)"
)
# 字面量路径，最常见的是注释里的 `~/.bili-ai/credentials.json`
_RE_TILDE_PATH = re.compile(r"~[/\\][\w.\-/\\]{2,80}")
_RE_CRED_SEG = re.compile(r"cred|token|cookie|auth|login|session|passw", re.I)
_RE_FILE_TAIL = re.compile(r"\.(json|txt|tok|token|pem|key|db|sqlite)$", re.I)


def scan_host_side(src: Path) -> tuple[list[str], list[str], bool, bool]:
    """找出只能在 DSH 宿主里跑的部分。

    扫全仓库（含 ts、含 tests），不再只看 lib/ —— ctx 用法出现在 src/ 或
    根目录同样算宿主侧，漏扫会让 porting.md 少报能力丢失。
    """
    host_files: list[str] = []
    caps: set[str] = set()
    has_client = any((src / "lib" / n).exists() for n in ("client.js", "client.ts", "client.mjs"))
    has_patch = (src / "cordis.patch.yml").exists() or (src / "cordis.patch.yaml").exists()

    for f in _iter_code_files(src, include_ts=True):
        text = _read_text(f)
        if "ctx." not in text:
            continue
        hits = set(re.findall(r"\bctx\.([A-Za-z_$][\w$]*)", text))
        if not hits:
            continue
        host_files.append(f.relative_to(src).as_posix())
        caps |= hits

    human = sorted({_CTX_HUMAN.get(c, f"ctx.{c}") for c in caps})
    return host_files, human, has_client, has_patch


def collect_env(src: Path) -> list[str]:
    names: set[str] = set()
    for f in _iter_code_files(src):
        text = _read_text(f)
        names |= set(re.findall(r"process\.env\.([A-Z][A-Z0-9_]{2,})", text))
        names |= set(re.findall(
            r"os\.environ(?:\.get)?\s*[\[(]\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]", text))
    skip = {
        "NODE_ENV", "NODE_OPTIONS", "NODE_NO_WARNINGS", "NODE_TEST_CONTEXT",
        "ELECTRON_RUN_AS_NODE", "PATH", "HOME", "USERPROFILE", "TMPDIR",
        "TEMP", "TMP", "PWD", "SHELL", "LANG", "TERM", "HOSTNAME", "USER",
        "CI", "DEBUG", "PORT", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
        "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "SYSTEMROOT", "COMSPEC",
    }
    return sorted(names - skip)


# 只有名字长得像「路径 / 配置文件」的变量才能被重定向到 data/。
# 其余（用户 ID、开关、可执行文件路径、数值阈值）一律只写进文档，**绝不 export** ——
# 把 BILI_ME_MID 指到一个目录上，插件当场就坏。
_RE_ENV_PATHISH = re.compile(
    r"(HOME|DIR|DIRECTORY|PATH|ROOT|FOLDER|CONFIG|SETTINGS|JSON|FILE|DATA|STORE)",
    re.I,
)


def is_path_env(var: str) -> bool:
    return bool(_RE_ENV_PATHISH.search(var))


def collect_config_names(src: Path) -> list[str]:
    """源码里被读写的 *.json 配置文件名，按「像不像主配置」排序。

    不能按扫描顺序取前几个 —— 一个插件可能有十几种子 json（配额、缓存、
    待办），主配置往往排在后面被截掉。按出现频次 + 名字里有没有 config 排。
    """
    counter: dict[str, int] = {}
    for f in _iter_code_files(src):
        for name in _RE_CONFIG_JSON.findall(_read_text(f)):
            if name in _CONFIG_NOISE or name.startswith("package"):
                continue
            counter[name] = counter.get(name, 0) + 1
    items = sorted(
        counter.items(),
        key=lambda kv: (0 if "config" in kv[0].lower() else 1, -kv[1], kv[0]),
    )
    return [n for n, _ in items[:8]]


def detect_credential_path(blob: str) -> str | None:
    """尽力从源码里摘出凭据文件路径，摘不到返回 None。

    两种写法都试：
      1. 字面量 `~/.bili-ai/credentials.json`（常在注释或文档字符串里）
      2. `join(os.homedir(), '.x', 'cred.json')` 拼出来的

    两条都必须同时满足「末端像文件」+「路径里带凭据关键字」。
    只看第一段会把 `~/.dsh`（数据目录）误当成凭据文件。
    """
    def accept(path: str) -> str | None:
        p = path.replace("\\", "/").rstrip("/.,;:)）】」")
        tail = p.rsplit("/", 1)[-1]
        if not _RE_FILE_TAIL.search(tail):
            return None
        if not _RE_CRED_SEG.search(p):
            return None
        return p

    # 1. 字面量路径
    for m in _RE_TILDE_PATH.finditer(blob):
        got = accept(m.group(0))
        if got:
            return got

    # 2. join(homedir(), ...) 拼出来的
    for m in _RE_HOME_JOIN.finditer(blob):
        segs = [s for s in re.findall(r"['\"]([^'\"\n]{1,60})['\"]", m.group(1))
                if s and not s.startswith("$")]
        if segs:
            got = accept("~/" + "/".join(segs))
            if got:
                return got
    return None


def detect_license(src: Path) -> str | None:
    for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENCE"):
        p = src / n
        if not p.is_file():
            continue
        head = _read_text(p)[:600].lower()
        for key, label in (
            ("mit license", "MIT"), ("apache license", "Apache-2.0"),
            ("gnu general public", "GPL"), ("mozilla public", "MPL-2.0"),
            ("bsd", "BSD"), ("the unlicense", "Unlicense"),
        ):
            if key in head:
                return label
        return "见源 LICENSE"
    lic = _read_json(src / "package.json").get("license")
    return str(lic) if isinstance(lic, str) and lic else None


def detect_runtime(src: Path, pkg: dict, entry: Path | None) -> str:
    if entry is not None:
        ext = entry.suffix.lower()
        if ext in (".mjs", ".cjs", ".js", ".jsx"):
            return "node"
        if ext == ".py":
            return "python"
        if ext in (".sh", ".bash"):
            return "shell"
    if pkg.get("bin"):
        return "node"
    if (src / "pyproject.toml").is_file() or (src / "setup.py").is_file():
        return "python"
    if (src / "__main__.py").is_file():
        return "python"
    return "unknown"


def detect_tests(src: Path) -> bool:
    for n in ("tests", "test", "__tests__", "spec"):
        if (src / n).is_dir():
            return True
    return bool(any(src.glob("*.test.*")) or any(src.glob("*_test.py")))


def collect_docs(src: Path) -> list[str]:
    docs: list[str] = []
    for pat in ("docs/*.md", "tools/docs/*.md", "doc/*.md", "*.md"):
        for p in sorted(src.glob(pat)):
            if p.is_file():
                rel = p.relative_to(src).as_posix()
                if rel not in docs:
                    docs.append(rel)
    return docs


def extract_defaults(src: Path) -> str | None:
    """把源码里的 `const DEFAULTS = { … }` 字面量摘出来。

    设置页丢了以后，用户没有别的办法知道配置项长什么样。这不是猜测 ——
    是源码里的字面量，原样搬进文档。
    """
    for f in _iter_code_files(src, include_ts=True):
        text = _read_text(f)
        m = re.search(
            r"const\s+(?:DEFAULTS|DEFAULT_CONFIG|DEFAULT_OPTIONS)\s*=\s*\{", text)
        if not m:
            continue
        start = m.end() - 1  # 指向那个 {
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[m.start():i + 1].strip()
    return None


def analyze(src: Path, name_override: str | None) -> Analysis:
    pkg = _read_json(src / "package.json")
    meta = _pyproject_meta(src)   # Python 项目的 name/version 兜底
    entry, cmds, ts_only = find_cli_entry(src)
    for c in cmds:
        c.kind = classify(c)

    host_files, host_caps, has_client, has_patch = scan_host_side(src)
    env_vars = collect_env(src)

    # 特征探测只扫非测试代码，避免测试夹具里的假数据干扰判断
    blob = "\n".join(_read_text(f) for f in _iter_code_files(src))

    return Analysis(
        src=src,
        pkg=pkg or meta,
        name=name_override or pkg.get("name") or meta.get("name") or src.name,
        version=str(pkg.get("version") or meta.get("version") or "0.0.0"),
        desc=(pkg.get("description") or meta.get("description") or "").strip(),
        license_id=detect_license(src),
        runtime=detect_runtime(src, pkg, entry),
        cli_entry=entry,
        commands=cmds,
        ts_only=ts_only,
        host_files=host_files,
        host_caps=host_caps,
        has_client=has_client,
        has_patch=has_patch,
        env_vars=env_vars,
        config_names=collect_config_names(src),
        has_scheduler=bool(_RE_SCHED.search(blob)),
        has_credentials=bool(_RE_CRED.search(blob)),
        has_network_write=bool(_RE_NETWRITE.search(blob)),
        has_dry_flag=bool(re.search(r"--dry\b", blob)),
        has_tests=detect_tests(src),
        cred_path=detect_credential_path(blob),
        deps=pkg.get("dependencies") or {},
        docs=collect_docs(src),
        defaults_snippet=extract_defaults(src),
    )


# ---------------------------------------------------------------- 公共片段


_DEAD_WORDS = ("DSH", "设置页", "配置页", "客户端注入", "插件挂载")


def _clean_desc(a: Analysis) -> str:
    """剥掉原始 description 里转换后已不成立的宿主说法。"""
    scrubbed = re.sub(
        r"[（(][^）)]*[）)]",
        lambda m: "" if any(k in m.group(0) for k in _DEAD_WORDS) else m.group(0),
        a.desc,
    )
    kept = [
        seg.strip()
        for seg in re.split(r"[。；]\s*", scrubbed)
        if seg.strip() and not any(k in seg for k in _DEAD_WORDS)
    ]
    core = "。".join(kept).rstrip("。")
    if not core and a.desc:
        body = re.split(r"[。；]", scrubbed)[0]
        core = (re.sub(r"DSH\s*里的\s*agent", "agent", body)
                .replace("DSH", "").strip()[:140].strip("：: ，,"))
    return core or a.slug


def _trigger_line(a: Analysis) -> str:
    names = [c.name for c in a.commands[:8]]
    if not names:
        return ""
    return f"当用户需要 {'、'.join(names)} 这类操作，或提到 {a.slug} 相关功能时使用。"


def _pick_config_name(a: Analysis) -> str:
    """挑一个最像「主配置文件」的名字：优先名字里带 config 的。"""
    for n in a.config_names:
        if "config" in n.lower():
            return n
    return a.config_names[0] if a.config_names else f"{a.slug}.config.json"


def _env_value(a: Analysis, var: str) -> str | None:
    """环境变量该被重定向到哪里。返回 None 表示「这个变量不是路径，不许动」。"""
    if not is_path_env(var):
        return None
    if "CONFIG" in var or var.endswith(("_JSON", "_FILE", "_SETTINGS")):
        return f"data/{_pick_config_name(a)}"
    return "data"


def _license_line(a: Analysis) -> str:
    if a.license_id:
        return f"源仓库 `{a.name}` v{a.version}，{a.license_id}。"
    return f"源仓库 `{a.name}` v{a.version}（源仓库未声明 License）。"


def _run_prefix(a: Analysis) -> str:
    """生成文档里用的调用前缀。三种运行时都走 bash 包装器，由它再转给具体解释器。"""
    return f'bash "$SKILL_DIR/scripts/{a.wrapper}.sh"'


# ---------------------------------------------------------------- SKILL.md


def _dropped_phrase(a: Analysis) -> str:
    """说明「转掉了什么」的那半句 —— 只列这个插件真的有的东西。

    写死「设置页与调度器已去掉」对没有调度器的插件就是胡说，
    所以这里按探测结果拼。什么都没丢就整句省掉。
    """
    gone: list[str] = []
    if a.has_client:
        gone.append("客户端设置页")
    if a.has_scheduler:
        gone.append("调度器")
    if a.host_caps:
        gone.append("宿主 API 绑定")
    if not gone:
        return ""
    return f"原插件的{'/'.join(gone)}已去掉；"


def gen_skill_md(a: Analysis) -> str:
    reads, writes, metas = a.counts
    prefix = _run_prefix(a)

    desc_lines = [
        f"{_clean_desc(a)}。",
        f"（由 DSH 插件 {a.name} v{a.version} 转换：命令实现原样保留；"
        f"{_dropped_phrase(a)}数据目录改为 skill 内的 data/。）",
        f"共 {len(a.commands)} 条命令：只读 {reads} / 写 {writes} / 其他 {metas}。",
    ]
    if a.missing:
        desc_lines.append(
            f"注意：其中 {len(a.missing)} 条命令的实现文件在源仓库里不存在，已标为缺失。")
    trig = _trigger_line(a)
    if trig:
        desc_lines.append(trig)

    L: list[str] = ["---", f"name: {a.fs_name}", "description: |"]
    L += [f"  {line}" for line in desc_lines]
    L += ["agent_created: true", "---", ""]

    # ---- 不可运行的产物：先说清楚，别让 agent 当它能跑
    if not a.runnable:
        L += [f"# {a.name}（转换失败）", "", "> **这个 skill 跑不起来，不要安装。**", ">"]
        if a.ts_only:
            L += [f"> `{a.name}` 的命令表只在 TypeScript 源码里，需要先编译成 JS 才能执行，"
                  f"本工具不做编译。"]
        elif a.commands:
            L += [f"> 解析到 {len(a.commands)} 条命令，但所有实现文件都不在源仓库里。"]
        else:
            L += [f"> `{a.name}` 没有可用的命令行入口 —— 它的能力全部挂在 DSH 宿主 API 上"
                  f"（`ctx.effect` / `ctx.webServer` / 客户端注入），WorkBuddy 没有对应物。"]
        L += ["", "具体缺什么、有没有替代方案，见 `references/porting.md`。", ""]
        return "\n".join(L)

    L += [
        f"# {a.name}（DSH → WorkBuddy 转换版）",
        "",
        f"{_license_line(a)}**命令实现一行没改**，只重写了外壳："
        f"DSH 宿主 API 换成包装脚本直接调用，配置目录搬到 `data/`。",
        "",
        "## 怎么跑",
        "",
        "**只用包装脚本，不要直接跑源码。** 包装脚本负责钉住数据目录，"
        "绕过它的话插件会去写原来的全局目录，两套配置立刻分叉。",
        "",
        "```bash",
        f'SKILL_DIR="$HOME/.workbuddy/skills/{a.fs_name}"   # 会话里没定义就先设这一行',
    ]
    if any(c.name in ("help", "-h", "--help") for c in a.commands):
        L.append(f"{prefix} help          # 看全部命令")
    # 示例优先挑一条只读的：拿写命令当第一条示例等于教人先动手
    first = next((c for c in a.commands if c.kind == "read" and c.exists), a.commands[0])
    L.append(f"{prefix} {first.name}          # 示例：{first.desc or first.name}")
    L += ["```", "", "参数清单见 `references/commands.md`；每条命令都支持什么开关，"
          "以它自己的输出为准。", ""]

    # ---- 前置：只写探测到的东西
    pre: list[str] = []
    if a.deps:
        pre += ["**先装依赖**（必须在 `vendor/` 里装，装到 skill 根目录会找不到模块）：",
                "", "```bash",
                f'cd "$SKILL_DIR/vendor/{a.vendor_dir}" && npm install --omit=dev',
                "```", ""]
    path_vars = [v for v in a.env_vars if is_path_env(v)]
    plain_vars = [v for v in a.env_vars if not is_path_env(v)]
    if path_vars:
        pre += ["包装脚本已把这些路径类变量重定向到 `data/`："
                + "、".join(f"`{v}`" for v in path_vars), ""]
    if plain_vars:
        pre += ["另有非路径变量（账号 ID / 开关 / 可执行文件路径之类），"
                "包装脚本**不动它们**，需要时自己设："
                + "、".join(f"`{v}`" for v in plain_vars), ""]
    if a.has_credentials:
        pre += ["**首次使用要准备凭据**（登录态 / cookie / token），见 `references/setup.md`。", ""]
    if pre:
        L += ["## 前置", ""] + pre

    # ---- 安全约定：有写命令才写，规则内容按探测结果给
    if writes:
        wnames = "、".join(f"`{c.name}`" for c in a.commands if c.kind == "write")
        L += ["## 安全约定（硬规则，先读完再动手）", "",
              f"写操作命令一共 {writes} 条：{wnames}。", ""]
        rules: list[str] = []
        if a.has_credentials:
            rules.append("**先确认当前凭据属于哪个账号** —— 打错号不可逆。"
                         "有只读的身份查询命令就先跑它。")
        if a.has_dry_flag:
            rules.append("**写操作一律先 `--dry`**（只打印将要发出的请求），"
                         "确认无误再 `--go`。")
        else:
            rules.append("**先跑对应的只读命令确认目标存在、内容正确**，再执行写操作。"
                         "这一条不能省。")
        rules.append("**一次一条。** 不批量、不定时轰炸、不多开并发。")
        read_names = [c.name for c in a.commands if c.kind == "read"]
        if read_names:
            rules.append(f"**写完立刻用只读命令回读复核**（如 `{read_names[0]}`），"
                         "不要凭返回值假设成功。")
        if a.has_network_write:
            rules.append("**遇到限流 / 风控 / 验证码类错误立刻停手，一次都不许重试。**"
                         "重试只会把风险评分推得更高。换 UA、换 IP、绕验证码只会更糟。")
        rules.append("**删除类操作前先备份 `data/`**，整个目录打包，别只拷 json。")
        for i, rule in enumerate(rules, 1):
            L.append(f"{i}. {rule}")
        L.append("")

    # ---- 命令速查
    L += ["## 命令速查", "", "| 命令 | 说明 | 类型 |", "|---|---|---|"]
    icon = {"read": "只读", "write": "**写**", "meta": "其他"}
    for c in a.commands:
        d = (c.desc or "—").replace("|", "\\|")
        if len(d) > 70:
            d = d[:70] + "…"
        kind = icon[c.kind]
        if not c.exists:
            kind += " · 缺失"
        L.append(f"| `{c.name}` | {d} | {kind} |")
    L += ["", "完整参数见 `references/commands.md`；能力取舍见 `references/porting.md`。", ""]

    # ---- 丢失的宿主能力
    lost: list[str] = list(a.host_caps)
    if a.has_client:
        lost.append("设置页面（客户端 section 注入）")
    if a.has_scheduler:
        lost.append("内置调度器（定时自主行为）")
    if lost:
        L += ["## 这个版本没有的东西", "",
              "原插件里挂在 DSH 宿主上的部分**全部去掉了**，因为 WorkBuddy 没有对应的宿主 API：",
              ""]
        L += [f"- {cap}" for cap in lost]
        L.append("")
        alts: list[str] = []
        if a.has_scheduler:
            alts.append("调度器 → 用 WorkBuddy 的定时任务（automation）触发对应命令，"
                        "不要自己写 `while true` 循环。")
        if a.has_client:
            alts.append("设置页 → 直接手改 `data/` 下的 json，改完不用重启。")
        if alts:
            L += ["替代方案：", ""] + [f"- {x}" for x in alts] + [""]
        else:
            L += ["这些能力在 WorkBuddy 里没有对应物，只能绕开："
                  "凡是必须靠宿主钩子触发的行为，用 agent 主动调用对应命令来模拟。", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- references


def gen_commands_md(a: Analysis) -> str:
    prefix = _run_prefix(a)
    src_ref = a.cli_entry.relative_to(a.src).as_posix() if a.cli_entry else "N/A"
    L = [f"# {a.name} 命令参考", "",
         f"源码解析自 `{src_ref}`（v{a.version}）。"
         f"**说明文字是源码里的原文，不是重写的。**", ""]
    if a.missing:
        L += [f"> {len(a.missing)} 条命令的目标文件在源仓库里不存在，"
              f"下面标为「缺失」—— 运行前请先核实。", ""]

    for kind, title in (("read", "只读命令（安全）"),
                        ("write", "写命令（会真的改东西或发出去）"),
                        ("meta", "其他")):
        group = [c for c in a.commands if c.kind == kind]
        if not group:
            continue
        L += [f"## {title}", ""]
        for c in group:
            L.append(f"### `{c.name}`")
            L.append("")
            L.append(c.desc or "（源码未给说明）")
            L.append("")
            tail = "" if c.exists else "  **（缺失：该文件不存在）**"
            L.append(f"实现：`{c.target}`{tail}")
            L.append("")
            L.append("```bash")
            L.append(f"{prefix} {c.name} --help")
            L.append("```")
            L.append("")
    L += ["## 参数以运行时为准", "",
          "上面每条都给了 `--help`，但**不是所有 CLI 都支持这个开关**。"
          "如果 `--help` 报错，直接跑不带参数的命令，或看源码里对应的实现文件。"
          "源码说明和实际参数不一致的情况确实存在，参数以运行时输出为准。", ""]
    return "\n".join(L)


def gen_porting_md(a: Analysis) -> str:
    L = [f"# 转换报告：{a.name} v{a.version}", "", "## 结论", ""]

    if not a.commands:
        L += ["**不可转换。** 没找到 CLI 分发入口，这个插件只有宿主半边"
              "（设置页 / 调度器 / 客户端注入），WorkBuddy 无法承载。"
              "硬转只会得到一个跑不起来的空壳。", ""]
    elif a.ts_only:
        L += ["**不可直接转换。** 命令表只在 TypeScript 源码里，需要先编译成 JS 才能跑。"
              "本工具不做编译 —— 请在源仓库里跑一次 `npm run build`，"
              "再用编译产物重跑本工具。", ""]
    elif not a.runnable:
        L += [f"**不可转换。** 解析到 {len(a.commands)} 条命令，"
              f"但**所有实现文件都不在源仓库里**（可能没提交、或在 .gitignore 里）。"
              f"文档能生成，跑不起来，所以不建议安装。", ""]
    else:
        L += [f"**可转换。** CLI 层共 {len(a.commands)} 条命令，"
              f"其中 {len(a.commands) - len(a.missing)} 条的实现文件已核实存在；"
              f"宿主层 {len(a.host_files)} 个文件的能力丢失，已在下面列出。", ""]

    L += ["## 能力对照", "", "| DSH 里的东西 | 转换成什么 | 状态 |", "|---|---|---|"]
    if a.commands:
        state = "完整可用" if not a.missing else f"{len(a.missing)} 条缺失"
        L.append(f"| CLI（{len(a.commands)} 命令） | `scripts/{a.wrapper}.sh` 包装调用 | {state} |")
    if a.env_vars:
        L.append(f"| 配置目录 | 环境变量 {'/'.join(a.env_vars[:3])} 指向 `data/` | 已隔离 |")
    if a.has_patch:
        L.append("| `cordis.patch.yml` 挂载声明 | 无需（不挂进程） | 丢弃 |")
    if a.has_client:
        L.append("| 客户端设置页 | 手改 `data/*.json` | 丢弃 |")
    if a.has_scheduler:
        L.append("| 内置调度器 | WorkBuddy automation | 丢弃，有替代 |")
    if a.has_credentials:
        L.append("| 宿主里的登录态 / cookie | 源插件自己的凭据文件（见 setup.md） | 需手动准备 |")
    for cap in a.host_caps:
        L.append(f"| {cap} | 无对应物 | 丢弃 |")
    L.append("")

    if a.host_files:
        L += ["### 丢掉的文件", ""] + [f"- `{f}`" for f in a.host_files] + [""]

    if a.missing:
        L += ["### 缺失的命令实现", "",
              "这些命令在命令表里登记了，但对应文件在源码里找不到：", ""]
        L += [f"- `{c.name}` → `{c.target}`" for c in a.missing]
        L += ["", "可能原因：没提交进仓库、被 .gitignore 排除、构建时才生成。"
              "确认之前不要指望这些命令。", ""]

    L += ["## 环境变量", ""]
    path_vars = [v for v in a.env_vars if is_path_env(v)]
    plain_vars = [v for v in a.env_vars if not is_path_env(v)]
    if path_vars:
        L += ["路径类变量，包装脚本已经重定向到 `data/`：", "",
              "| 变量 | 本 skill 里的值 |", "|---|---|"]
        for v in path_vars:
            L.append(f"| `{v}` | `{_env_value(a, v)}` |")
        L += ["", "**注意：** 设了这些变量后，插件读写的是 skill 自己的 `data/`，"
              "**与原来宿主里的那份配置彻底分家**。想共用就把变量指回去，"
              "但两边同时写会互相覆盖。", ""]
    if plain_vars:
        L += ["下面这些**不是路径**（账号 ID / 开关 / 外部程序路径 / 阈值），"
              "包装脚本不碰它们，原样透传：", ""]
        L += [f"- `{v}`" for v in plain_vars]
        L += ["", "需要时自己 export，或写在 shell 配置里。", ""]
    if not a.env_vars:
        L += ["未发现自定义环境变量。", ""]

    L += ["## 依赖", ""]
    if a.deps:
        L += ["原插件 npm 依赖（未安装，需要时执行）：", "", "```bash",
              f"cd vendor/{a.vendor_dir} && npm install --omit=dev", "```", ""]
        L += [f"- `{k}` {v}" for k, v in a.deps.items()]
        L += ["", "**必须在 `vendor/` 里装，不能装在 skill 根目录** —— "
              "源码用相对路径 import，`node_modules` 位置不对就找不到模块。", ""]
    else:
        L += ["无第三方依赖（纯内置模块）。", ""]

    gaps: list[str] = []
    if a.has_credentials:
        loc = f"（源码里的位置：`{a.cred_path}`）" if a.cred_path else "（位置未探到）"
        gaps.append("**凭据**：源插件需要登录态 / cookie / token，见 `references/setup.md`。"
                    f"{loc}它在用户目录下，和 `data/` 不是一回事。" )
    if a.has_scheduler:
        gaps.append("**调度器**：原插件有内置定时行为，丢掉之后要用 WorkBuddy 的"
                    "自动任务顶上，不要自己写 `while true` 循环。")
    if a.has_tests:
        gaps.append("**测试**：源仓库自带 tests/，可以跑一遍确认 `vendor/` 目录完整。")
    if a.missing:
        gaps.append(f"**缺失实现**：{len(a.missing)} 条命令的源文件不在仓库里。")
    if not gaps:
        gaps.append("没有额外缺口 —— 依赖装好后即可使用。")

    L += ["## 还缺什么", ""] + [f"{i}. {g}" for i, g in enumerate(gaps, 1)] + [""]
    return "\n".join(L)


def gen_setup_md(a: Analysis) -> str:
    prefix = _run_prefix(a)
    L = [f"# {a.name} 首次配置", ""]
    step = 1

    if a.deps:
        L += [f"## {step}. 依赖", "", "```bash",
              f'cd "$SKILL_DIR/vendor/{a.vendor_dir}"',
              "npm install --omit=dev"]
        if a.has_tests:
            L.append("node --test           # 源仓库自带测试，可用来验证 vendor 完整")
        L += ["```", ""]
        step += 1
    elif a.has_tests:
        L += [f"## {step}. 验证", "", "```bash",
              f'cd "$SKILL_DIR/vendor/{a.vendor_dir}"', "node --test", "```", ""]
        step += 1

    if a.has_credentials:
        first_name = next((c.name for c in a.commands if c.kind == "read"),
                          a.commands[0].name if a.commands else "help")
        L += [f"## {step}. 凭据", "",
              "这个插件需要登录态才能工作。先跑只读命令看当前状态：", "",
              "```bash", f"{prefix} {first_name}", "```", ""]
        if a.cred_path:
            L += [f"凭据文件：`{a.cred_path}`（源码里硬编码的路径，原样摘出）。",
                  "",
                  "**它在用户目录下，不在 skill 目录内** —— 包装脚本管不到它，"
                  "所以多个 skill 会共用同一份登录态，换号就是换这份文件。", ""]
        else:
            L += ["凭据位置**没能从源码里定位**（可能是运行时拼出来的，或来自环境变量）。"
                  "翻一下登录相关模块，或跑一次登录命令，它自己会说读的是哪个文件。", ""]
        step += 1

    L += [f"## {step}. 数据目录", "",
          "插件的配置 / 运行时数据全部落在 `data/`。"]
    if a.env_vars:
        L.append("对应原来宿主里的位置，由包装脚本里的环境变量重定向过去。")
    L += ["", "```", "data/"]
    if a.config_names:
        L.append(f"  {a.config_names[0]}   主配置")
        for n in a.config_names[1:4]:
            L.append(f"  {n}")
    L += ["  ...                      插件自己创建的子目录", "```", "",
          "要备份就整个 `data/` 打包，别只拷 json。", ""]
    step += 1

    if a.defaults_snippet:
        cfg_name = a.config_names[0] if a.config_names else f"{a.slug}.config.json"
        L += [f"## {step}. 配置模板（设置页的替代品）", "",
              "原插件靠设置页写配置，这半边丢了。下面是**源码里默认值对象的字面量**，"
              "原样摘出，不是推测：", "", "```js", a.defaults_snippet, "```", "",
              f"要用就照这个结构手写 `data/{cfg_name}`（去掉注释、键名加引号），"
              f"或直接调对应的 CLI 让它自己落盘。**不确定的项先不写**，"
              f"大多数命令缺配置时会用内置默认值。", ""]
        step += 1

    if a.docs:
        L += ["## 源码自带文档", "",
              "这些是原作者的文档，比转换出的说明更权威：", ""]
        L += [f"- `vendor/{a.vendor_dir}/{d}`" for d in a.docs]
        L.append("")
    return "\n".join(L)


def gen_readme(a: Analysis) -> str:
    L = [f"# {a.name} skill", "",
         f"由 `dsh2wb.py` 从 DSH 插件 `{a.name}` v{a.version} 生成。", ""]
    if a.runnable:
        has_help = any(c.name == "help" for c in a.commands)
        L += ["```bash",
              f'bash scripts/{a.wrapper}.sh {"help" if has_help else a.commands[0].name}',
              "```", ""]
    else:
        L += ["> 这个 skill 不可运行，原因见 `references/porting.md`。", ""]
    L += ["- `SKILL.md` —— agent 读的入口",
          "- `references/` —— 命令表 / 转换报告 / 配置说明",
          "- `vendor/` —— 原插件源码（未改动）",
          "- `data/` —— 插件运行时数据",
          "",
          "重新生成会覆盖 `SKILL.md` 和 `references/`，**不会动 `data/` 和 `vendor/`**。", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- 包装脚本


def _env_exports(a: Analysis) -> list[tuple[str, str]]:
    """只返回**该被重定向**的变量。非路径变量一个都不碰。"""
    out: list[tuple[str, str]] = []
    for v in a.env_vars:
        rel = _env_value(a, v)
        if rel:
            out.append((v, rel))
    return out


def _invoke_sh(a: Analysis, cli_rel: str) -> str:
    if a.runtime == "python":
        return (
            'PY="${PYTHON_BIN:-}"\n'
            'if [ -z "$PY" ]; then\n'
            '  for c in python3 python py; do\n'
            '    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi\n'
            '  done\n'
            'fi\n'
            '[ -n "$PY" ] || { echo "找不到 python。设 PYTHON_BIN 指向解释器" >&2; exit 127; }\n'
            f'exec "$PY" "$VENDOR/{cli_rel}" "$@"'
        )
    if a.runtime == "shell":
        return f'exec bash "$VENDOR/{cli_rel}" "$@"'
    return (
        'NODE_BIN="${NODE_BIN:-}"\n'
        'if [ -z "$NODE_BIN" ]; then\n'
        '  if command -v node >/dev/null 2>&1; then\n'
        '    NODE_BIN="$(command -v node)"\n'
        '  else\n'
        '    for c in "$HOME/.nvm/current/bin/node" "/usr/local/bin/node" "/usr/bin/node"; do\n'
        '      if [ -x "$c" ]; then NODE_BIN="$c"; break; fi\n'
        '    done\n'
        '  fi\n'
        'fi\n'
        '[ -n "$NODE_BIN" ] || { echo "找不到 node。设 NODE_BIN 环境变量指向 node 可执行文件" >&2; exit 127; }\n'
        f'exec "$NODE_BIN" "$VENDOR/{cli_rel}" "$@"'
    )


def gen_wrapper_sh(a: Analysis, cli_rel: str) -> str:
    exports = "\n".join(
        f'export {v}="$SKILL_DIR/{rel}"' for v, rel in _env_exports(a)
    ) or "# 插件没有需要重定向的路径类环境变量"
    return f"""#!/usr/bin/env bash
# {a.name} 包装器（dsh2wb v{__version__} 生成）
# 作用只有两个：钉住数据目录；找到运行时。不为别的。
set -euo pipefail

# pwd -W 拿 Windows 风格路径（G:/…）。Git Bash 里普通 pwd 给 /g/…，
# 传给原生 node.exe 时 MSYS 会把它当"当前盘根下的相对路径"重写成 G:\\g\\…，
# 表现就是 Cannot find module 'G:\\g\\…' —— 这个坑只在 Windows 上出现。
_here="$(dirname "${{BASH_SOURCE[0]}}")"
SKILL_DIR="$(cd "$_here/.." && (pwd -W 2>/dev/null || pwd))"
VENDOR="$SKILL_DIR/vendor/{a.vendor_dir}"

{exports}

mkdir -p "$SKILL_DIR/data"

if [ ! -f "$VENDOR/{cli_rel}" ]; then
  echo "vendor 不完整：$VENDOR/{cli_rel} 不存在" >&2
  echo "先跑：cd \\"$VENDOR\\" && npm install --omit=dev" >&2
  exit 1
fi

{_invoke_sh(a, cli_rel)}
"""


def gen_wrapper_cmd(a: Analysis, cli_rel: str) -> str:
    win_cli = cli_rel.replace("/", BS)
    sets = "\n".join(
        f'set "{v}=%SKILL_DIR%{BS}{rel.replace("/", BS)}"' for v, rel in _env_exports(a)
    ) or "rem 插件没有需要重定向的路径类环境变量"

    if a.runtime == "python":
        finder = 'set "PY_BIN=python"\nwhere python >nul 2>nul || set "PY_BIN=py"'
        invoke = f'"%PY_BIN%" "%VENDOR%{BS}{win_cli}" %*'
    elif a.runtime == "shell":
        finder = "rem 需要 PATH 里有 bash"
        invoke = f'bash "%VENDOR%{BS}{win_cli}" %*'
    else:
        finder = ('set "NODE_BIN=node"\n'
                  'where node >nul 2>nul || set "NODE_BIN=%ProgramFiles%' + BS + 'nodejs' + BS + 'node.exe"')
        invoke = f'"%NODE_BIN%" "%VENDOR%{BS}{win_cli}" %*'

    return f"""@echo off
rem {a.name} 包装器（dsh2wb v{__version__} 生成）
setlocal
set "SKILL_DIR=%~dp0.."
set "SKILL_DIR=%SKILL_DIR:{BS}..=%"
set "VENDOR=%SKILL_DIR%{BS}vendor{BS}{a.vendor_dir}"

{sets}

if not exist "%SKILL_DIR%{BS}data" mkdir "%SKILL_DIR%{BS}data"

{finder}

if not exist "%VENDOR%{BS}{win_cli}" (
  echo vendor incomplete: %VENDOR%{BS}{win_cli} 1>&2
  exit /b 1
)
{invoke}
"""


# ---------------------------------------------------------------- 组装


def build(a: Analysis, out_root: Path, vendor: str, force: bool) -> Path:
    dst = out_root / a.fs_name
    skill_md = dst / "SKILL.md"
    if skill_md.exists() and not force:
        raise SystemExit(f"目标已存在：{dst}\n加 --force 覆盖（data/ 和 vendor/ 不会被删）")

    for sub in ("references", "data"):
        (dst / sub).mkdir(parents=True, exist_ok=True)
    if a.runnable:
        (dst / "scripts").mkdir(parents=True, exist_ok=True)

    cli_rel = a.cli_entry.relative_to(a.src).as_posix() if a.cli_entry else ""

    # 文本产物：永远写。报告本身有价值，即使不可运行。
    (dst / "SKILL.md").write_text(gen_skill_md(a), encoding="utf-8")
    (dst / "README.md").write_text(gen_readme(a), encoding="utf-8")
    (dst / "references" / "porting.md").write_text(gen_porting_md(a), encoding="utf-8")
    (dst / "references" / "setup.md").write_text(gen_setup_md(a), encoding="utf-8")

    # 可运行产物：只在真能跑时才写，避免「文档说有、实际没有」的脚本
    if a.runnable and cli_rel:
        (dst / "references" / "commands.md").write_text(gen_commands_md(a), encoding="utf-8")
        sh = dst / "scripts" / f"{a.wrapper}.sh"
        sh.write_text(gen_wrapper_sh(a, cli_rel), encoding="utf-8")
        sh.chmod(0o755)
        (dst / "scripts" / f"{a.wrapper}.cmd").write_text(
            gen_wrapper_cmd(a, cli_rel), encoding="utf-8")
    else:
        # 清掉上一次成功构建留下的脚本，别让旧脚本误导 agent
        for stale in (dst / "scripts").glob(f"{a.wrapper}.*"):
            try:
                stale.unlink()
            except OSError:
                pass

    vdst = dst / "vendor" / a.vendor_dir
    if vendor != "none" and not vdst.exists():
        vdst.parent.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(
            ".git", "node_modules", ".githooks", "*.log", "dist", "build",
            "__pycache__", ".venv", "*.pyc",
        )
        if vendor == "link":
            linked = False
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(vdst), str(a.src)],
                    check=False, capture_output=True,
                )
                linked = vdst.exists()
            else:
                try:
                    os.symlink(a.src, vdst, target_is_directory=True)
                    linked = True
                except OSError:
                    linked = False
            if not linked:
                shutil.copytree(a.src, vdst, ignore=ignore)
        else:
            shutil.copytree(a.src, vdst, ignore=ignore)
    return dst


def resolve_source(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    if re.match(r"^(https?://|git@|git://)", arg):
        key = re.sub(r"\W+", "-", arg).strip("-")[-50:]
        tmp = Path(os.environ.get("TEMP") or "/tmp") / f"dsh2wb-{key}"
        # 只在真的完整时才复用；上次 clone 失败留下的半成品不算
        if (tmp / ".git").exists() or (tmp / "package.json").is_file():
            return tmp.resolve()
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        try:
            subprocess.run(["git", "clone", "--depth", "1", arg, str(tmp)], check=True)
        except FileNotFoundError:
            raise SystemExit("找不到 git 命令，无法克隆远程仓库")
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit(f"git clone 失败（退出码 {exc.returncode}）：{arg}")
        return tmp.resolve()
    raise SystemExit(f"源不存在：{arg}")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="dsh2wb",
        description="DSH(Cordis) 插件 → WorkBuddy Skill 转换器",
    )
    ap.add_argument("source", help="插件目录 或 git 仓库地址")
    ap.add_argument("-o", "--out", default="./out", help="输出目录")
    ap.add_argument("--name", help="覆盖 skill 名（默认用 package.json 的 name）")
    ap.add_argument("--vendor", choices=("copy", "link", "none"), default="copy",
                    help="插件源码怎么放（link 用 junction / 符号链接，省空间）")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已有产物（data/ 与 vendor/ 保留）")
    ap.add_argument("--json", action="store_true", help="只输出分析结果，不生成")
    ap.add_argument("--version", action="version", version=f"dsh2wb {__version__}")
    args = ap.parse_args()

    src = resolve_source(args.source)
    a = analyze(src, args.name)
    reads, writes, metas = a.counts

    if args.json:
        print(json.dumps({
            "name": a.name, "version": a.version, "desc": a.desc,
            "license": a.license_id, "runtime": a.runtime,
            "cli_entry": a.cli_entry.relative_to(a.src).as_posix() if a.cli_entry else None,
            "ts_only": a.ts_only,
            "runnable": a.runnable,
            "commands": [
                {"name": c.name, "kind": c.kind, "target": c.target,
                 "exists": c.exists, "desc": c.desc}
                for c in a.commands
            ],
            "missing": [c.name for c in a.missing],
            "counts": {"read": reads, "write": writes, "meta": metas},
            "env_vars": a.env_vars, "config_names": a.config_names,
            "host_files": a.host_files, "host_caps": a.host_caps,
            "features": {
                "scheduler": a.has_scheduler, "credentials": a.has_credentials,
                "network_write": a.has_network_write, "dry_flag": a.has_dry_flag,
                "tests": a.has_tests, "client": a.has_client, "patch": a.has_patch,
            },
            "deps": a.deps, "docs": a.docs,
        }, ensure_ascii=False, indent=2))
        return 0

    out_root = Path(args.out).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    dst = build(a, out_root, args.vendor, args.force)

    rel = a.cli_entry.relative_to(src).as_posix() if a.cli_entry else "未找到"
    print(f"源       : {src}")
    print(f"插件     : {a.name} v{a.version}  [{a.runtime}, {a.license_id or '无 License'}]")
    print(f"CLI入口  : {rel}")
    print(f"命令     : {len(a.commands)} 条（只读 {reads} / 写 {writes} / 其他 {metas}）")
    if a.missing:
        print(f"缺失实现 : {len(a.missing)} 条 → "
              f"{'、'.join(c.name for c in a.missing[:8])}")
    print(f"宿主丢失 : {len(a.host_files)} 文件 · {len(a.host_caps)} 项能力")
    feats = [n for n, v in (
        ("调度器", a.has_scheduler), ("凭据", a.has_credentials),
        ("网络写", a.has_network_write), ("--dry", a.has_dry_flag),
        ("测试", a.has_tests),
    ) if v]
    print(f"探测特征 : {'、'.join(feats) if feats else '无'}")
    print(f"输出     : {dst}")

    if not a.runnable:
        if a.ts_only:
            print("\n[警告] 命令表只在 TypeScript 源码里，需要先编译。"
                  "产出的 SKILL.md 已标注不可运行。")
        elif not a.commands:
            print("\n[警告] 没解析到 CLI 命令，这个插件只有宿主半边。"
                  "产出的 SKILL.md 已标注不可运行。")
        else:
            print("\n[警告] 命令表存在但实现文件全都不存在，产物不可运行。")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
