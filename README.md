# dsh2wb

**DSH（DeepSeek Harness）插件 → WorkBuddy Skill 转换器**

把 Cordis 架构的 DSH 插件拆开，能搬的搬成 WorkBuddy 能跑的 skill，不能搬的如实写进报告——不造假 skill。

```
python dsh2wb.py <插件目录|git地址> [-o 输出目录] [--vendor copy|link|none]
```

单文件，纯 Python 标准库（3.8+），无第三方依赖。

---

## 为什么需要它

两种扩展模型根本不是一个东西：

| | DSH 插件 | WorkBuddy Skill |
|---|---|---|
| 形态 | npm 包，挂在宿主进程里 | 目录，提示词 + 脚本 |
| 能力来源 | `ctx.effect` / `ctx.webServer` / `ctx.connection` / 客户端注入 | agent 直接跑 bash |
| 界面 | 自带设置页、调度器、路由 | 无 |
| 生命周期 | 宿主启动时挂载，`onUnload` 清理 | 每次调用现起 |

所以转换的核心判断只有一条：

> **这个插件的能力，有多少是脱离宿主也能跑的纯命令行？**

- **有 CLI**（命令分发表）→ 正常转，一条条映射
- **只有宿主半边**（设置页 / 调度器 / 客户端注入）→ 转不了，写进 `porting.md` 说明原因，退出码 2

硬转的产物是「文档写了但跑不起来」，比不转更糟。

---

## 唯一的硬规则：所有内容都由探测结果决定

这是 v0.2 重写的全部理由。

v0.1 把「B站 / 扫码登录 / `--dry --go` / 风控错误码 `-352` / 内置调度器 / `node --test`」直接写死在模板里。转 dsh-bili-whale 没问题，转一个本地笔记插件就成了满篇胡话——一个没有登录、没有调度器、没有测试的插件，产物里却写着「先 `session` 确认是哪个号」「遇到 `-352` 立刻停手」。

那正是这个工具声称要避免的「造假」。

现在每一项都先探测，探不到就不写：

| 探测项 | 决定什么 |
|---|---|
| `runtime` | wrapper 调 `node` 还是 `python` 还是 `bash` |
| `deps` | 写不写 `npm install` 那一段 |
| `has_credentials` + `cred_path` | 凭据章节，以及凭据文件的真实路径 |
| `has_scheduler` | 「调度器丢了，用 automation 顶」这条建议 |
| `has_network_write` | 风控 / 限流那条硬规则 |
| `has_dry_flag` | 教 `--dry/--go`，还是教「先跑只读命令确认」 |
| `has_tests` | 提不提「跑源仓库测试验证 vendor 完整」 |
| `license_id` | 写 `MIT`，还是写「源仓库未声明 License」 |
| `env_vars` 分类 | 哪些变量该重定向到 `data/`，哪些一个字节都不许碰 |
| `config_names` | 配置模板该写成哪个文件名 |

`tests/test_dsh2wb.py` 里有一条测试专门盯这个：转一个本地笔记插件，断言产物里**不出现** `bili` / `B站` / `-352` / `扫码` / `cookie` 任何一个。

---

## 产物结构

```
<out>/<name>/
├── SKILL.md                  agent 读的入口：何时用、怎么跑、安全约定、命令速查
├── README.md
├── references/
│   ├── commands.md           从源码解析的完整命令表（原文说明，不重写）
│   ├── porting.md            能力对照：转了哪些、丢了哪些、还缺什么
│   └── setup.md              首次配置 + 从源码摘出的配置默认值
├── scripts/<slug>.sh         bash 包装器（钉住数据目录 + 找运行时）
├── scripts/<slug>.cmd        Windows 版
├── vendor/<name>/            插件源码原件（copy / link / none）
└── data/                     插件运行时数据（原宿主数据目录的位置）
```

`<slug>` 是包名去掉 `dsh-` 前缀：`dsh-bili-whale` → `scripts/bili-whale.sh`。

重新生成只覆盖 `SKILL.md` 和 `references/`，**不动 `data/` 和 `vendor/`**。

不可转换的插件（无 CLI / 只有 TS / 实现文件全缺）不生成 `scripts/`，`SKILL.md` 开头顶一句「这个 skill 跑不起来，不要安装」。

---

## 四个关键设计

### 1. 命令读写分类：读源码里的分区注释，不猜关键词

好的插件会在命令表里自己分好区：

```js
// —— 读（不需要写操作，安全） ——
session: [...],
// —— 写（会真的发出去；都必须显式 --dry 或 --go） ——
comment: [...],
```

这是作者给的权威信号，比任何启发式都准。中英文都认，四种写法都认：

```
—— 读 ——        --- read ---      // [只读]      // 写操作
```

没分区的才退回关键词兜底，且**写优先**——把写操作误判成只读，代价是用户的账号。

> 这里踩过两个坑。第一个：标题「读（不需要写操作，安全）」整句同时含「读」和「写」，
> 按子串匹配会先命中「写」，把 27 条只读命令全标成危险操作。修法是只取括号前的头一个词。
>
> 第二个：`_READ_HINTS` 里一度放着 `--dry` 和 `--go`。这俩是**写命令的开关**，
> 一旦出现在写命令的说明里，`classify()` 就把整条命令判成只读。测试里那条
> `test_write_wins_over_dry_flag` 就是钉死这事的。

### 2. 命令表解析：不认变量名，只认内容

```
name: ['file.mjs', '说明']      name: ["file.mjs"]
name: 'file.mjs'                name: { file: 'file.mjs', desc: '说明' }
```

`const map` / `const commands` / `const COMMANDS` / `const cmdMap` / 随便什么名字都一样处理——判据是「解出来的条目够多、且值看起来像文件路径」，不是变量名。普通的配置对象（`{ timeout: [1000, 'ms'] }`）会被排除。

同时校验每条命令的目标文件**真的存在**。不存在的标为「缺失」，写进 `porting.md`，命令表里也带标记。v0.1 不校验，对着一个 `commands/` 目录根本不存在的插件照样输出「全部可用」——那是直接造假。

### 3. 数据目录隔离：只动该动的环境变量

不少插件支持用环境变量改数据位置，只是没人注意。转换器会扫出全部 `process.env.X` 泄漏点，由包装脚本把它们导向 `data/`：

```bash
export DSH_HOME="$SKILL_DIR/data"
export BILI_WHALE_CONFIG="$SKILL_DIR/data/bili-whale.config.json"
```

但**不是所有环境变量都是路径**。`BILI_ME_MID` 是账号 ID，`FFMPEG` 是可执行文件路径，`ELECTRON_RUN_AS_NODE` 是布尔开关——把它们指到一个目录上，插件当场就坏。

所以按名字分类：像路径的（`HOME`/`DIR`/`PATH`/`CONFIG`/`JSON`/`FILE`）才重定向，其余原样透传，文档里单独列出来。Node 自己的运行时变量（`NODE_TEST_CONTEXT` 之类）直接忽略。

这样**插件源码一行不用改**就脱离了宿主，也不污染原来的全局目录。

### 4. 设置页丢了，配置从源码里摘

宿主半边被丢掉后，用户没了改配置的地方。转换器会把源码里的 `const DEFAULTS = { … }` 字面量原样摘进 `setup.md`——是代码里的真东西，不是推测出来的模板。

配置**文件名**也不是猜的：扫源码里所有 `*.json` 字面量，按出现频次 + 名字里有没有 `config` 排序。一个插件可能有十几种子 json（配额、缓存、待办），主配置往往排在后面，按扫描顺序取就会被截掉。

---

## Windows 上的一个坑

Git Bash 的 `pwd` 返回 `/g/xxx`，传给原生 `node.exe` 时 MSYS 会把它当「当前盘根下的相对路径」重写成 `G:\g\xxx`：

```
Error: Cannot find module 'G:\g\dsh2wb\out\...\tools\bili.mjs'
```

包装器里必须用 `pwd -W` 拿 Windows 风格路径。这个坑只在 Windows + Git Bash 组合出现，Linux/macOS 无感。

---

## 已验证的转换

### dsh-bili-whale v0.4.6

[B站自动化插件](https://github.com/Alterssss/dsh-bili-whale)，转换结果：

| 项 | 结果 |
|---|---|
| CLI 命令 | 38 条（只读 27 / 写 11） |
| 实现文件 | 全部核实存在 |
| 宿主侧丢弃 | 3 文件 · 6 项能力 |
| 探测特征 | 调度器、凭据、网络写、`--dry`、测试 |
| 凭据路径 | `~/.bili-ai/credentials.json`（从源码注释里摘出） |
| 环境变量 | 2 个重定向到 `data/`，7 个原样透传 |

丢弃的部分：设置页（`lib/client.js`）、HTTP 路由、`ctx.effect` 生命周期、调度器。替代方案写在 `porting.md` 里——调度器用宿主的定时任务顶，设置页用手改 `data/*.json` 顶。

### 预期不可转换的类型

- **只有 `ctx.*` 注册、没有独立 CLI 的插件** → 产出报告并返回 2。
- **命令表只在 TypeScript 里** → 返回 2，报告里提示先 `npm run build`。

```
命令    : 0 条（只读 0 / 写 0 / 其他 0）
[警告] 没解析到 CLI 命令，这个插件只有宿主半边。产出的 SKILL.md 已标注不可运行。
```

---

## 已知局限

- **只认「键 → 文件路径」形态的命令表**。yargs / commander / 手工 `switch (cmd)` 会解析失败并报「不可转换」——**这是保守的失败，不是错误的失败**。
- **不编译 TS**。`.ts` 里发现命令表会标 `ts_only` 并在报告里说明，但不会替你 build。
- **不安装依赖**。`vendor/` 里的 `npm install` 要手动跑，报告里给了命令。位置必须在 `vendor/<name>/` 内，装到 skill 根目录会找不到模块。
- **会改 `data/` 归属**。设了环境变量后，插件读写的是 skill 自己的 `data/`，与原来那份**彻底分家**。想共用就把变量指回去，但两边同时写会互相覆盖。
- **凭据路径靠正则碰**。探到就写，探不到就明说探不到，不编一个像样的路径糊弄人。

---

## 用法

```bash
# 从远程仓库转
python dsh2wb.py https://github.com/Alterssss/dsh-bili-whale -o ./out

# 从本地目录转，源码用 junction 链接省空间
python dsh2wb.py ./my-plugin -o ./out --vendor link

# 只看分析结果，不生成（方便先判断值不值得转）
python dsh2wb.py ./my-plugin --json

# 覆盖已有产物（data/ 和 vendor/ 保留）
python dsh2wb.py ./my-plugin -o ./out --force
```

退出码：`0` 可转换，`2` 不可转换。

转完装到 WorkBuddy：

```bash
cp -r out/<name> ~/.workbuddy/skills/<name>
```

---

## 测试

```bash
python -m unittest discover -s tests -v
```

49 条，覆盖转义还原、分区识别、读写判定、命令表六种形态、入口选择、凭据路径探测、多运行时，以及每条已知 bug 的回归。

---

## License

MIT
