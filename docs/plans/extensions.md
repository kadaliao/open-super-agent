# 扩展体系改造：概念归位 + 权限分层

> 2026-11-09 评审后定的方案。背景见 [applets.md](applets.md)（插件化四步）。
> 本文是实施的依据，分四个子任务并行做，各自在 worktree 里完成后合回 main。

## 为什么

两个不满意，同一个根因：系统里唯一的信任边界是"服务进程"。进程内跑的（插件）和直接在宿主上跑的（本机终端、自定义 stdio 应用）
都等于服务器 root，所以只能用 `security.local_exec` 一位整体放开或整体掐死。"插件"又装了一堆本该是工具、应用、事件来源的东西，
因为进程内 Python 是今天唯一能"随手加个工具"的口子。

现状：

- 插件市场 21 个共 4136 行，`calc`、`unit_convert`、`date_calc`、`lunar`、`world_clock`、`text_count`、`random_pick`、`cn_holiday` 八个是纯函数，
  占近一半；`weather_now`、`exchange_rate`、`stock_quote` 是联网查询；`epic_free`、`earthquake`、`ip_change` 是事件来源；
  `push_phone`、`smtp_mail`、`task_webhook` 是通知渠道；只有 `audit_log`、`budget_alert`、`keyword_alert`、`rss_watch` 真的要碰 osa 内部状态。
- 插件 API 只有 `tool / on / task / provide / source`，加不了渠道、电脑后端、模型服务、设置页、卡片、路由。
- 技能只能用户 `/名字` 调或定时任务绑定，Bot 自己拿不到正文（`skills.summary` 只给名字，没有 `use_skill`）；只有文字，没有脚本和参考文件；
  `apps:` 依赖靠正则扫 `@名字`。
- `plugins.locked()` 在服务器锁下连市场插件也拒绝，应用那边早有 `from_market` 例外。
- 本机终端每条命令都问、禁止"总是允许"、没有按命令模式的规则。
- 沙箱电脑（Docker / Instabox）是现成的安全执行场所，只服务 Worker 的 `computer_shell`；Router、插件、技能都用不上。
- Router 场合不能等确认，所以插件的 `gated` 工具只能给 WORKER，文档写了但 `Context.tool` 不校验。

## 目标状态

用户面对三个词：

| 词 | 是什么 | 运行在哪 | 服务器锁下 |
|---|---|---|---|
| 应用 | 连外部能力（MCP） | 进程外 | 市场里的照常 |
| 技能 | 做事方法：SKILL.md + 可选 scripts/、references/ | 文字进提示词；脚本在沙箱电脑里跑 | 照常 |
| 插件（开发者，预览） | 扩展 osa 自己：订阅事件、碰内部状态 | 进程内 | 指纹等于随版本发布的放行，其余拒绝 |

执行场所：有 Docker / Instabox 电脑就在里面跑；host 模式退到受限子进程（macOS `sandbox-exec`，Linux `bwrap` / `unshare`，都没有就按 `host` 档走确认）。
服务器锁拆成策略表：`security.exec: sandbox | host | off`、`security.plugins: off | market | any`、`security.apps: market | any`；
`local_exec` / `demo` 继续认，按旧语义映射。

## 四个子任务

### A. 概念归位（catalog、sources、设置页、文档）

1. `epic_free`、`earthquake`、`ip_change` 从 `osa/catalog/plugins` 搬进 `osa/sources/`，成为内置事件来源（进 `SOURCES` 和 `ORDER`），
   连带测试搬过去；文档 docs/events.md 补上。
2. 下架 `calc`、`unit_convert`、`date_calc`、`lunar`、`world_clock`、`text_count`、`random_pick`、`cn_holiday`、
   `weather_now`、`exchange_rate`、`stock_quote`：删文件、删测试、docs/plugins.md 的市场列表改写，
   说明这类需求改用 `run_code`（子任务 B）。已装在 `data/plugins` 的不动，照旧能用。
3. 设置页：`Plugins.tsx` 的标题改"开发者插件（预览）"，从主导航挪到"高级"分组末尾；页首一句话说明它和应用、技能的分工，
   链到 docs/plugins.md。
4. `registry.Context.tool`：`gated=True` 且 `where` 含 ROUTER 的注册直接 `ValueError`，错误写清楚原因。
5. docs/plugins.md 开头加"先想清楚是不是插件"一节：接外部服务 → 应用；做事方法 → 技能；算东西 → `run_code`；只有要碰 osa 内部状态才是插件。
   docs/architecture.md、README 里涉及插件市场数量的地方同步。

不碰：`plugins.py` 的加载逻辑、`security.py`、`approvals.py`。

### B. 沙箱执行：`run_code`

1. 新模块 `osa/sandbox.py`：`async run(lang, code, files=None, timeout=…) -> (stdout+stderr 文字, 产出的文件列表)`。
   - 有电脑（`computer.get()` 的 backend 是 local / instabox）：写进电脑 `/data/workspace/.run/<id>/`，`exec` 跑，产出文件留在工作文件夹。
   - backend 是 host / linked：受限子进程。macOS 用 `sandbox-exec -p`（只读系统、只能写工作文件夹、可选禁网），Linux 优先 `bwrap`，
     没有就 `unshare`；都没有时返回"这台机器没有沙箱"，并说明可以用 `computer_shell`。环境变量照 `host._env()` 去掉密钥。
   - lang：`python`、`bash`、`node`（电脑里没装的返回说明）。输出截到 20K 字，超时默认 60 秒、Router 版 5 秒。
2. 在 `osa/builtin.py` 注册：`run_code`（WORKER，`when` 要求有电脑或有沙箱，不 gated，因为跑在沙箱里）和 `quick_code`
   （ROUTER，只 python，5 秒，无网，用于算数、换单位、算日期）。两个的 schema 说明里写清楚"在沙箱里跑，工作文件夹外改不了"。
3. Worker / Router 提示词各加一句（放在不变的部分，不破坏缓存前缀，跑 `tests/test_prompt_cache.py`）：算数、日期、单位换算、处理数据用它，
   不要心算。
4. `security`：`exec` 档读法先写在 `osa/sandbox.py` 自己的小函数里（`security.exec_mode()`，子任务 C 会把它并进策略表；
   先按 `local_exec()` 推：True → host 允许，False → 只有 sandbox）。
5. 测试 `tests/test_sandbox.py`：host 退路在当前平台实际跑一次 python（有 sandbox-exec / bwrap 时验证写工作文件夹外失败）；电脑后端用 fake exec。
   docs/configuration.md 补说明。

不碰：`approvals.py`、`computer/tools.py` 的确认逻辑、`plugins.py`。

### C. 策略表 + 规则化确认

1. `security.py`：`security.exec / plugins / apps` 三个策略键，带默认值和 `local_exec` / `demo` 的映射：
   - `local_exec: true` → exec=host, plugins=any, apps=any；`false` → exec=sandbox, plugins=market, apps=market；`demo: true` → exec=sandbox, plugins=off, apps=market（且 stdio 仍禁）。
   - 新键写了就以新键为准。`public()` 带出来给前端。
2. `plugins.locked()` 按 `plugins` 策略：`market` 档下，启用和加载时要求 `digest(entry) == digest(catalog entry)`，不等于的报错说明"这台服务器只能用随版本发布的原样插件"。
   `install` 在 `market` 档允许。文档 SECURITY.md、docs/plugins.md、config.yaml 注释同步。
3. `approvals`：规则模式扩成 `工具:参数前缀`（如 `computer_shell:git *`、`filesystem__write_file:/data/workspace/*`），
   `decide(tool, backend, args=None)` 匹配时把 args 的首个字串参数（shell 是 cmd）拼成 `tool:值` 再 fnmatch；旧的纯工具名规则照旧。
   确认卡片的"总是允许"对 shell 改成"总是允许这个命令前缀"（前端卡片加一个可编辑的前缀框，默认第一个词），`once` 不再对 host shell 整体禁止，只对 `desktop_submit` 这类保留。
4. 确认作用域：`resolve(..., scope="once|task|minutes:N|always")`。`task` 在 `pending` 里按 task_id 记一张允许表，任务结束清掉；
   `minutes` 记到期时间。卡片按钮：拒绝 / 允许一次 / 本次任务内允许 / 总是允许（shell 是前缀）。聊天软件渠道（`channels/*.py`）按钮加"本次任务"。
5. 测试：`tests/test_security.py` 加策略映射；`tests/test_plugins.py` 加 market 档；新 `tests/test_approval_rules.py`。

不碰：`builtin.py`、`sandbox`、技能。

### D. 技能升级

1. 技能包：`skills` 表加 `files` 列（JSON：相对路径 → 内容，只收文本，总量 ≤ 200K），对应 SKILL.md 包的 `scripts/`、`references/`。
   导出：单文件技能照旧 .md；带文件的导出 zip（SKILL.md + 文件）；导入认 .md、zip、GitHub 文件夹链接（`raw_url` 已经会取 SKILL.md，补一步列目录取 scripts/、references/）。
2. frontmatter 的 `apps:` 以声明为准，`apps_in` 只在没声明时回退到正则；导入时没连的应用列出来并给"去连接"。
3. `use_skill(name)` 工具（ROUTER + WORKER，不 gated）：返回 `skills.expand` 的正文，带文件的附上文件清单和每个文件开头 400 字；
   Worker 要用脚本时先 `computer_write_file` / `run_code` 带进沙箱（说明写在返回文字里）。系统提示词里技能列表保持"名字 + 描述"不变（缓存前缀）。
4. 技能市场：`osa/catalog/skills/<名字>/SKILL.md`，列表、安装（复制进 skills 表，source=market）、右栏"技能"面板里加入口；先放 3 个示例：
   周报整理、网页转 Markdown 存资料库、会议纪要（都只用现有工具）。
5. 测试 `tests/test_skills.py`：文件上限、导入导出往返、`use_skill`、apps 声明优先。docs 新建 docs/skills.md，README 链过去。

不碰：`approvals.py`、`security.py`、`plugins.py`。

## 共同约定

- 每个子任务在自己的 worktree 分支上做，提交到分支，不推送；合并由主会话做。
- 新 worktree 没有 `.venv` 和 `web/node_modules`：`uv sync`，`ln -s <主库>/web/node_modules web/node_modules`（symlink 不要 add）。
- 界面文字中文原文即键，英文补进 `osa/locales/en.json` 和 `web/src/locales/en.json`（看 docs/development.md），`uv run python -m osa.i18n` 要过。
- 测试用 `uv run pytest tests/<文件>`，建表的代码会碰 `data/osa.db`，schema 相关的测试设 `OSA_CONFIG` 指向 scratch。
- 前端 `cd web && npm run build` 要过。
- 提交信息按仓库惯例写中文一句话，`git add <具体路径>`，不要 `-A`。

## 之后再说

- 插件进程隔离（插件就是一个 MCP server 加 osa 扩展）：等归位之后看插件还剩多少再定。
- 通知渠道（push_phone、smtp_mail、task_webhook）归到设置里的"通知"或改成应用。
