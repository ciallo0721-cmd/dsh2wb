# dsh2wb

**DSH（DeepSeek Harness）插件 → WorkBuddy Skill 转换器**

把 Cordis 架构的 DSH 插件拆开，能搬的搬成 WorkBuddy 能跑的 skill，不能搬的如实写进报告——不造假 skill。

```
python dsh2wb.py <插件目录|git地址> [-o 输出目录] [--vendor copy|link|none]
```

单文件，纯 Python 标准库，无第三方依赖。

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

- **有 CLI**（`tools/*.mjs` 里带命令分发表）→ 正常转，一条条映射
- **只有宿主半边**（设置页 / 调度器 / 客户端注入）→ 转不了，写进 `porting.md` 说明原因，退出码 2

硬转的产物是「文档写了但跑不起来」，比不转更糟。

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
├── scripts/
│   ├── bili.sh               bash 包装器（钉住数据目录 + 找 node）
│   └── bili.cmd              Windows 版
├── vendor/<name>/            插件源码原件（copy / link / none）
└── data/                     插件运行时数据（原 ~/.dsh 的位置）
```

重新生成只覆盖 `SKILL.md` 和 `references/`，**不动 `data/` 和 `vendor/`**。

---

## 三个关键设计

### 1. 命令读写分类：读源码里的分区注释，不猜关键词

好的插件会在命令表里自己分好区：

```js
// —— 读（不需要写操作，安全） ——
session: [...],
// —— 写（会真的发出去；都必须显式 --dry 或 --go） ——
comment: [...],
```

这是作者给的权威信号，比任何启发式都准。没分区的才退回关键词兜底，且**宁可判 write**——把写操作误判成只读，代价是用户的账号。

> 这里踩过一个坑：标题「读（不需要写操作，安全）」整句同时含「读」和「写」，
> 按子串匹配会先命中「写」，把 27 条只读命令全标成危险操作。
> 修法是只取括号前的头一个词。

### 2. 数据目录隔离：靠环境变量，不靠改代码

不少插件其实支持用环境变量改数据位置，只是没人注意。转换器会扫出全部 `process.env.X` 泄漏点，再由包装脚本把它们导向 `data/`：

```bash
export DSH_HOME="$SKILL_DIR/data"
export BILI_WHALE_CONFIG="$SKILL_DIR/data/bili-whale.config.json"
```

这样**插件源码一行不用改**就脱离了宿主，也不污染 `~/.dsh/`。扫不出这类变量的才需要在 vendor 里动手。

### 3. 设置页丢了，配置从源码里摘

宿主半边被丢掉后，用户没了改配置的地方。转换器会把源码里的 `const DEFAULTS = { … }` 字面量原样摘进 `setup.md`——是代码里的真东西，不是推测出来的模板。

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
| 宿主侧丢弃 | 3 文件 · 6 项能力 |
| 可运行性 | `help` / `traffic` / `deps` 实测通过 |
| 数据隔离 | `~/.dsh/` 零污染 |

丢弃的部分：设置页（`lib/client.js`）、HTTP 路由、`ctx.effect` 生命周期、调度器。替代方案写在 `porting.md` 里——调度器用宿主的定时任务顶，设置页用手改 `data/*.json` 顶。

### 预期不可转换的类型

只有 `ctx.*` 注册、没有独立 CLI 的插件。转换器会正常产出报告并返回 2：

```
命令    : 0 条（只读 0 / 写 0 / 其他 0）
[警告] 没解析到 CLI 命令，转换产出的 skill 不可运行。
```

---

## 已知局限

- **只认一种 CLI 形态**：`const map = { 命令: ['文件', '说明'] }` 这种分发表。其他约定（yargs、commander、手工 switch）会解析失败并报「不可转换」——**这是保守的失败，不是错误的失败**。
- **不解析 TS**。`.ts` 源码只做 `ctx.*` 能力扫描，不做命令表提取。
- **不安装依赖**。`vendor/` 里的 `npm install` 要手动跑，报告里给了命令。位置必须在 `vendor/<name>/` 内，装到 skill 根目录会找不到模块。
- **会改 `data/` 归属**。设了环境变量后，插件读写的是 skill 自己的 `data/`，与原来 DSH 里那份**彻底分家**。想共用就把变量指回去，但两边同时写会互相覆盖。

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

转完装到 WorkBuddy：

```bash
cp -r out/<name> ~/.workbuddy/skills/<name>
```

---

## License

MIT
