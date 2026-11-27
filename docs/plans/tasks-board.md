# 把事交出去、看得见做到哪：任务、看板、记忆整理、团队

> [!NOTE]
> 2026-11-27 定的计划，进行中。进度记在文末。

## 为什么、要得到什么

现在和 Bot 的关系是“聊天”：派出去的任务是一次性的，做完就结束；想补一句、纠正一下，只能再派一个新任务；
服务一重启，做到一半的任务直接标成“已中断”。事情一多，也没有一个地方能看到“我交代过哪些事、各自做到哪了”。

参照 [dsh-bots](https://github.com/kadaliao/dsh-bots) 在协作这一层的做法（任务有自己的会话、需求看板、记忆整理、团队），
分四块做，做完以后：

1. **任务能对话、能接着做**：能给正在做的任务补一句；任务能停下来问你；做完的任务回一句就接着做；服务重启后接着做；模型服务临时出错会自己重试。
2. **需求看板和项目**：你要的每件事一张卡，在哪一列由背后的任务决定；卡片归到项目下，带编号（`OPC-12`）。
3. **记忆会自己整理**：有日期的事带截止日，过期不再读；每天夜里合并、精简；整理前留底，能撤销。
4. **团队**：一键雇用一支团队，每个团队有负责人，主 Bot 当调度员。

底座（电脑、确认、保险库、事件、应用、文档、App）不动。

## 两个先定下来的事

- **新功能只做在 Rust 版**（`server/`）。Python 版（`osa/*.py`）从 0.27.0 起就不再加功能（文档只有 Rust 版有），这次也不改它；
  `tests/`（测 Python 版的）照旧要过。提示词和工具会变，所以 `contract/test_parity.py`（两个版本的请求逐字一样）从第一波起停用，
  文件留着，开头标明原因。提示词、界面文字、应用清单这些 Rust 版编进去的资源仍然在 `osa/` 下面，照旧在那里改。
- **不做话题自动换**。dsh-bots 需要它，是因为它的对话会一直变长；OSA 的上下文本来就只带最近 20–39 条（Inline & Spill），不会越聊越贵。

## 分三波

| 波 | 做什么 | 怎么做 |
|---|---|---|
| 第一波 | 任务（服务端、界面）、记忆整理、看板和项目（服务端、界面） | 五条线同时做，各在各的工作区 |
| 收拢 | 五条线合到一个分支，跑整套测试，用真模型走一遍 | 一个人做，不并行 |
| 第二波 | 把看板接进对话（派任务时归卡、消息里提编号、卡片等别的卡、项目记忆）；任务和卡片互相能点过去；聊天软件里回复任务 | 两条线（服务端、界面），再收拢 |
| 第三波 | 团队（团队和负责人、一键雇用、主 Bot 只调度） | 两条线（服务端、界面），再收拢；动手前先看下面“第三波”那一节 |
| 收尾 | 文档、两份 README、官网、更新日志、英文翻译检查 | 一个人做 |

## 接口约定

界面的线照着这里写，服务端的线照着这里实现。要改约定，先改这份文档并告诉收拢的人，不要各改各的。

### 任务（task-core 提供，task-ui 用）

- 任务的状态还是这五个：`running`、`done`、`failed`、`cancelled`、`interrupted`。任务的公开字段（WebSocket 的 `task` 事件、
  `GET /api/tasks`、`GET /api/tasks/{id}`）多三个：
  - `waiting_kind`：`"approval"`（等你确认）、`"help"`（请你接手电脑）、`"question"`（等你回答），没在等是 `null`；
  - `can_continue`：布尔。这个任务存着过程，做完以后还能接着说、能重试；
  - `round`：第几轮，从 1 开始。做完以后又接着做，就加 1。
- `POST /api/tasks/{id}/message`，请求 `{"text": "…"}`，返回 `{"ok": true, "task": {…}}`。
  - 任务正在做：这句话在它下一步之前交给它；
  - 任务在等你回答：这句话就是回答；
  - 任务已经结束（做完、失败、取消、中断）而且 `can_continue`：任务回到 `running`，带着这句话接着做，做完再出一条结果；
  - 没有这个任务 404；不能接着做（没存过程，比如 Heavy 模式、升级前的老任务）400，`detail` 里写原因。
- `POST /api/tasks/{id}/retry`：失败、取消、中断的任务从停下的地方接着做（没存过程的从头做），返回同上。
- 任务提问：Worker 的新工具 `ask_user(question, options?)`。问题走现成的确认卡片通道：`approval` 事件和待确认列表里多一种
  `kind: "question"`，带 `question`（文字）和 `options`（字符串数组，可以空）。回答用现成的 WebSocket 消息
  `{"type": "approve", "id": N, "ok": true, "note": "回答的话"}`；`ok: false` 是不回答。对话里的任务一直等；后台任务（定时任务、事件）照确认卡片的时限过期。
- 任务过程（`task_steps`）多一种 `kind: "user"`：你发给任务的话，`body` 是原文。
- 你发给任务的话在对话里存成一条消息：`role: "task"`，`meta: {"kind": "followup", "task_id": N}`，`content` 是 `[给任务 #N 的话]\n原文`。
  接着做出来的结果还是 `meta.kind: "result"`，多一个 `round`。
- 主 Bot 以外的 Bot 也一样：Router 多一个工具 `tell_task(task_id, text)`，你在对话里接着说某个任务的事（补充、纠正、追问结果）时，
  它把话转给那个任务，不再新派一个。
- 讨论串：在任务的消息（派发的那条或结果那条）下面回复，这句话直接交给任务，Bot 不被叫醒；之后的结果也发在这个讨论串里。界面不用为它加接口。
- 重启：重启前正在做、存着过程、最近 6 小时内动过的任务，启动后接着做。其余的照旧标成 `interrupted`。重启那一刻正在执行的那一步可能做了一半，处理的原则是
  **先弄清是哪一步，能查的 Bot 自己查，查不出来才问你，而且问的时候把这一步说清楚**：
  1. 少发生：正常停服务（部署、退出 App）时不再让任务开始新的一步，等正在执行的那一步做完、存好再退出，最多等 `limits.shutdown_wait_s`（默认 30 秒）。
     所以平常的部署不会留下做了一半的步，只有崩溃、被强杀、等超时才会。
  2. 知道是哪一步：每个工具调用开始之前已经写进 `task_steps`（状态 `running`，带工具名和参数）。恢复时“开始了、没有结果”的就是说不准的那几步，不靠猜。
  3. 只读的（查询、读网页、读文件这类不用确认的）直接重做。
  4. 写操作不重做，也不悄悄丢掉：模型收到的工具结果是“这一步在服务重启时正在执行，不知道有没有做成”，带工具名、参数、开始的时间，并要求它先用只读的办法查
     （已发送的邮件、文件在不在、页面现在的样子、记录有没有多一条），查到了就照查到的接着做。
  5. 查不出来、要把同一个工具的写操作再做一次时，一律弹卡片，“总是允许”的规则对这一次不算数。卡片上写明：这一步是重启时正在做的、几点开始的、
     具体内容（收件人、标题、命令……）、Bot 查过什么。三个选择：再做一次；已经做成了，不用再做；不做了，任务停下。
  6. 别的写操作照平常的确认规则，不因为重启多问。任务的结果里注明有一步当时说不准，最后是怎么处理的。

  同一套也用在另外两条路上（第二波补的）：任务某一步卡住被自动重试时、任务被停下或失败以后点“重试”或接着做时，
  那一步如果是开始了没有结果的写操作，同样不重做、先查、再做时一律弹卡片。“说不准”的清单存在数据库里，连着重启也还在。

  有的操作谁也查不了（比如往一个不能回查的接口发了一次请求）：这种只能在卡片上照实说“查不出来”，由你决定，做不到保证只做一次。
- 出错：模型服务临时出错（408、429、5xx、连接断了）等 15、30、60 秒重试，第四次还不行才算失败。3 分钟没动静，先从上一步重试一次，再卡住才取消。

### 看板和项目（board-core 提供，board-ui 用）

- 一张卡（需求）的字段：

  ```
  {id, key, no, project_id, title, auto_title, body, source_text, column, assignee_bot_id, after, blocked_by,
   tasks, progress, latest, created, updated, archived, origin}
  ```

  - `key`：`"OPC-12"`，不属于项目的是 `null`；`no` 是项目里的编号，不回收；
  - `auto_title`：标题是直接取你的原话（Bot 没有另外转述）；`source_text` 是你的原话；
  - `column`：`todo`（还没交出去）、`doing`（有任务在做）、`waiting`（有任务在等你，或者最近一个任务失败、取消、中断了）、`done`、`cancelled`。
    由任务算出来，不存；你手动标的完成、取消优先；
  - `assignee_bot_id`：交给谁，`null` 是主 Bot；`after`：要等哪些卡（id 数组）；`blocked_by`：其中还没完成的 `[{id, key, title, column}]`；
  - `tasks`：`[{id, bot_id, status, waiting_kind, goal, last_action, elapsed}]`；`progress`：`{done, total}`；`latest`：最新进展的一句话；
  - `origin`：`{chat_id, message_id}`，从哪条消息来的，自己建的卡是 `null`。
- `GET /api/board?project=&q=&from=&to=&archived=0&limit=200&before=` → `{"columns": {"todo": [], "doing": [], "waiting": [], "done": [], "cancelled": []}, "projects": [], "more": false}`。
  `project` 是项目 id，`none` 是不属于项目的；`q` 搜编号、标题、描述；`from`、`to` 是创建时间（秒）；`archived=1` 只看已归档的。
- `POST /api/needs`，请求 `{title, body?, project_id?, assignee_bot_id?, after?, start}` → 卡。`start: false` 放进待办。
- `GET /api/needs/{id}` → 卡，另带 `results`（`[{task_id, text}]`）和 `questions`（在等你的确认卡片，格式和待确认列表里的一样）。
- `PATCH /api/needs/{id}`，可改 `title`、`body`、`project_id`（在新项目里重新编号）、`assignee_bot_id`、`after`。
- `POST /api/needs/{id}/start`、`/done`、`/cancel`、`/archive`、`/restore` → 卡。`done` 和 `cancel` 会停掉它下面还在做的任务。
- 项目：`{id, key, name, brief, dir, repo, count, created}`。`GET /api/projects`、`POST /api/projects`（`{key, name, brief?, dir?, repo?}`，`key` 是 2–8 个大写字母数字）、
  `PATCH /api/projects/{id}`、`DELETE /api/projects/{id}`（卡片留下，变成不属于项目）。
- 卡片有变化时发 WebSocket 事件 `{"type": "board", "need_id": N}`，界面收到后重新取。
- 第一波里卡片怎么开始：交给主 Bot 的，作为你的一条消息发到它的私聊（开头带卡片编号或标题）；交给别的 Bot 的，直接让那个 Bot 派一个任务，目标是标题加描述。
  这两条路用的都是现成的函数，不改 `router.rs`。
- 给第二波留的函数（第一波就实现好、有测试，但还没人调）：
  - `board::attach(task_id, bot_id, chat_id, need, need_id, project, user_text) -> Option<i64>`：派任务时归到卡上，没有就新建；
  - `board::state_block(bot_id) -> String`：给 Router 看的项目列表和进行中的卡；
  - `board::mention(text) -> Option<(i64, Vec<i64>)>`：消息开头的卡片编号 → 卡和它正在做的任务；
  - `board::task_changed(task_id)`：任务状态变了，重新算列、发事件、让等它的卡开始；
  - `board::notes_for(task_id) -> String`：任务所属项目的简介和目录，接在任务交代后面。

### 记忆（memory 这条线自己做完服务端和界面）

- `memories` 表加两列：`until`（截止日，秒，`null` 是不过期）、`pinned`（置顶，0 或 1）。记忆的公开字段跟着多这两个。
- `remember` 工具多一个可选参数 `until`（`YYYY-MM-DD`）；提示词里加一句：有日期的事（活动、截止日、报价有效期）带上截止日。
- 过了截止日的记忆不再进提示词，下次整理时删掉。置顶的不过期，整理时也不动。
- 每天夜里整理一次（`memory.consolidate_at`，默认 `"03:00"`，空着就不整理）：删过期的；每个 Bot 的记忆和共享的记忆各用一次便宜的模型
  合并重复、改掉过时的、去掉只记进度的；把当天做过的事写成一条每日摘要（`kind: "digest"`，共享，只留最近 14 条，最新一条放进每轮的状态里，不进系统提示词）。
- 整理前把改到的记忆原样存一份（`memories_history`，留最近 7 次）。`POST /api/memories/consolidate` 立即整理，返回 `{"ok": true, "changed": N, "run": id}`；
  `POST /api/memories/consolidate/undo` 撤销最近一次。整理完发 `memories_changed`。
- `PATCH /api/memories/{id}` 多收 `until`、`pinned`。记忆面板上能置顶、设截止日、立即整理、撤销、看每日摘要。

## 第一波的分工

五条线，每条线一个工作区、一个分支。**只改归自己的文件**；“大家都要加一点”的文件按下面的办法加。

| 线 | 分支 | 做什么 | 归它的文件 |
|---|---|---|---|
| task-core | `feat/task-core` | “任务”一节的服务端部分 | `server/…/task.rs`、`worker.rs`、`router.rs`、`tasklog.rs`、`watchdog.rs`、新的 `taskstore.rs`、`prompts/router.txt`、`prompts/state.txt`、`contract/test_tasks.py`、`contract/test_parity.py`（停用） |
| task-ui | `feat/task-ui` | “任务”一节的界面：任务详情里的输入框、重试、等你回答；提问卡片；对话里“给任务的话”；过程里你说的话 | `web/src/components/Tasks.tsx`、`cards.tsx`、`Chat.tsx`、`Thread.tsx`、`store.ts`、`types.ts`、`styles.css` |
| memory | `feat/memory` | “记忆”一节，服务端和记忆面板 | `server/…/memories.rs`、新的 `consolidate.rs`、`web/src/components/Memories.tsx`、新的 `web/src/memories.css`、`contract/test_memories.py` |
| board-core | `feat/board-core` | “看板和项目”一节的服务端部分 | 新的 `server/…/board.rs`、`contract/test_board.py` |
| board-ui | `feat/board-ui` | 看板页（四列、筛选和搜索、已归档、新建卡片、卡片页、项目管理、两种拖法）、侧栏入口 | 新的 `web/src/components/Board.tsx`、`web/src/board.ts`（它自己的类型和请求）、`web/src/board.css` |

大家都要加一点的文件：

- `server/…/lib.rs`、`routes.rs`：只加自己那一行（`pub mod`、建表、后台循环、`.merge(xxx::routes())`），新接口写在自己模块的 `routes()` 里，不去改 `server.rs` 的路由表。
- `server/…/assets/schemas.json`：只手改自己的工具（task-core：`tell_task`；memory：`remember` 的 `until`），不要重新生成。
  Worker 的新工具（`ask_user`）在 `builtin.rs` 里加在列表**末尾**。
- `router.rs` 归 task-core。memory 只动 `remember` 那一个函数和读 `until` 的地方；board-core 第一波不碰它。
- `web/src/App.tsx`、`Sidebar.tsx`：board-ui 只加看板的入口和页面那几行。`store.ts`、`types.ts` 归 task-ui，别的线要的状态和类型放在自己的文件里。
- `osa/locales/en.json`、`web/src/locales/en.json`：各自加自己的句子，冲突由收拢的人合。
- `config.yaml`：新配置项各自加在对应的段里，带一行注释。
- `CHANGELOG.md`、`docs/`、README、`site/`：第一波不写，收尾时一起写。

每条线交活的标准：

- 服务端的线：`cargo build` 没有新警告；`OSA_IMPL=rust uv run pytest contract/test_core.py contract/test_smoke.py contract/test_<自己的>.py` 全过；
  自己的接口测试覆盖“接口约定”里归它的每一条。
- 界面的线：`npm run build` 通过；`uv run python -m osa.i18n`（带副本的环境变量，见下）里前端没有没包进 `t()` 的中文、没有缺的英文；
  用 Playwright 把新界面在 1280 和 390 两个宽度各截一张，明亮和夜间都看一眼。服务端还没有的接口用 `page.route` 按约定假造返回。
- 提交到自己的分支（按路径 `git add`，不用 `-A`），不 push。最后把做了什么、没做什么、和约定不一样的地方写进报告。

## 并行开发的规矩

为的是五条线同时做不打架，尤其是测试的时候。

1. **各用各的工作区**：`/Volumes/Data-External/workspace/osa-wt/<线名>`。不进主目录，不进别人的工作区，不切别人的分支。
2. **各编各的**：`server/target`、`web/node_modules`、`.venv`、`osa/static/dist` 都在自己的工作区里，不共用。编译时带 `CARGO_BUILD_JOBS=3`，三条服务端的线同时编才不会把机器占满。
3. **自动测试只跑自己的那几个文件**（上面写的）。接口测试每次起服务都用空闲端口和临时目录，几条线同时跑没关系。
   整套测试（`contract/` 全部、`tests/` 全部）只在收拢时由一个人跑，一次一套。`test_computer.py`、`test_mac.py`、`test_docs.py` 会用到 Docker、Chrome、文档服务，只在收拢时跑。
4. **手动起服务用自己的端口和数据目录**：

   | 线 | 服务端口 | 前端热更新端口 |
   |---|---|---|
   | 收拢 | 8790 | 5190 |
   | task-core | 8791 | — |
   | task-ui | 8792 | 5192 |
   | memory | 8793 | 5193 |
   | board-core | 8794 | — |
   | board-ui | 8795 | 5195 |

   数据目录用自己工作区里的 `.scratch/`（已经在 `.gitignore` 里的话直接用，不在就加到 `.git/info/exclude`）：`OSA_DATA_DIR=<工作区>/.scratch/data OSA_CONFIG=<工作区>/.scratch/config.yaml`。
   不用 8765、8766、8772（别的东西在用）。测完把自己起的服务停掉，用 `lsof -nP -iTCP -sTCP:LISTEN` 确认。
5. **不碰真的东西**：不读不写主目录的 `data/`；不用真的模型密钥（第一波全用假模型）；起服务带 `env -u INSTABOX_API_KEY`，配置里 `computer.enabled: false`；
   不动 Docker 里的 `osa-computer` 容器；`python -m osa.i18n` 一律带上面的 `OSA_DATA_DIR`、`OSA_CONFIG`（它会对配置里的数据库跑建表）。
6. **表结构**：只加新表、给 `memories` 加那两列；不给 `llm_usage` 加列。加列前看一眼 `osa/*.py` 里对这张表的 `insert` 是不是写了列名（按位置插的表不能加列）。
7. **不 push、不发版、不部署、不动 dsh-bots**。
8. 发现约定有问题、或者非改别人的文件不可：停下来写进报告，不要自己改过去。

## 收拢

在 `feat/tasks-board` 分支（工作区 `osa-wt/integration`）上，一个人做：

1. 按 task-core → memory → board-core → task-ui → board-ui 的顺序合进来，冲突当场解决（预计在 `lib.rs`、`routes.rs`、`schemas.json`、两个 `en.json`）。
2. `cargo build`、`npm run build`、`python -m osa.i18n`、`python -m osa.catalog`、`python -m osa.templates`。
3. 整套测试，一次一套：`cargo test`、`OSA_IMPL=rust uv run pytest contract`，再 `uv run pytest -q`（Python 版没改，应该和之前一样）。和合并前的基线比，不能多出失败。
   再照 CI 的办法在 Linux 上跑一遍（`docker build -f Dockerfile.rust --target test …` 和 `docker run …`，见 [开发 → 接口测试](../development.md#接口测试contract)）：
   CI 以前不跑 Rust 版的测试，这次加上了（`server` 任务跑单元测试，`docker` 任务在测试镜像里跑 `contract/`），新功能的测试要在那里也过。
4. 用真模型在数据副本上走一遍（8790 端口，办法见 [开发 → 不要用真实数据测试](../development.md#不要用真实数据测试)），Playwright 截图：
   - 派一个任务，中途补一句，任务照新的做；任务问一个问题，回答后接着做；做完后回一句，接着做出第二条结果；
   - 任务做到一半杀掉服务再起来，任务接着做完；
   - 建项目、建卡交给主 Bot，卡从待办到进行中到已完成；任务提问时卡在“等你处理”；
   - 记住一件带截止日的事，把日期改到过去，它不再进提示词；立即整理、撤销。
5. 有问题的退回给那条线改，或者当场修；修完重跑第 3 步。

第二波、第三波各自做完后同样收拢一次。全部做完，合进 `main`（只提交，不 push），再写文档和更新日志。

## 第二波：把看板接进对话

三条线，规矩和第一波一样（各用各的工作区和端口，只跑自己的测试，整套测试收拢时跑）。

| 线 | 分支 | 端口 | 归它的文件 |
|---|---|---|---|
| wire | `feat/w2-wire` | 8791 换成 8796（8791 被别的东西占着） | `server/…/router.rs`、`board.rs`、`taskstore.rs`、`worker.rs`、`task.rs`、`tasklog.rs`、`memories.rs`、`consolidate.rs`、`server.rs`、`prompts/`、`assets/schemas.json`、`contract/test_board.py`、`test_tasks.py`、`test_memories.py`、新的 `contract/test_board_chat.py` |
| channels | `feat/w2-channels` | 8797 | `server/…/channels/`、`push.rs`、`contract/test_channels.py` |
| ui | `feat/w2-ui` | 8798 / 5198 | `web/src/` 全部 |

### 接口约定（wire 提供，ui 用）

- `dispatch` 多三个可选参数：`need`（用用户的语言写一句用户会得到什么，不写谁去做）、`need_id`（归到已有的卡，数字或 `"OPC-12"`）、`project`（项目编号）。
  用户在私聊或群里提的事才归卡：这一轮是用户的消息引起的（不是别的 Bot 的私信、交接，不是定时任务和事件）。都不写时新建一张卡，标题取用户的原话，`auto_title` 为真。
  同一轮里派的几个任务归同一张卡。任务的派发消息 `meta` 里多 `need_id`。
- 从看板开始一张交给主 Bot 的卡：替你发的那条消息 `meta` 里带 `need_id`，这一轮派的任务都归这张卡（换掉第一波“这一轮之后派的都算”的办法）。
- Router 的状态里加一段项目列表和进行中的卡（`board::state_block`）。新工具 `update_need(id, action, …)`：`id` 是数字或编号；
  `action` 是 `rename`（`title`）、`merge`（`into`，把这张并进另一张，任务跟过去）、`cancel`、`done`、`move`（`project`，空着是移出项目）。
- 任务的公开字段多 `need`：`{id, key, title}`，没有是 `null`（WebSocket 的 `task` 事件、任务列表、任务详情都带）。
- 消息以卡片编号开头（`OPC-12 加上英国`）、那张卡正好有一个任务在做：这句话直接交给那个任务（和讨论串里回复任务一样，存成 followup，Bot 不被叫醒）。
  网页和聊天软件走同一个入口（`server::safe_handle`），所以两边都生效。有几个任务在做、或者没有任务在做时，照常交给 Bot，它在状态里能看到这张卡。
- 任务状态变了（开始、做完、接着做、开始等你、不等了）调 `board::task_changed`：重新算列、发 `board` 事件、让等它的卡开始（带上前面卡的结果；有取消或失败的不开始）。
- 项目的简介和目录接在任务交代后面（`board::notes_for`）。
- 项目记忆：`memories` 加 `project_id`（`null` 是原来的记忆）。只有这个项目的任务的 Worker 读到，Router 在状态里看到当前对话里进行中的卡所属项目的记忆。
  `remember` 多可选参数 `project`（项目编号）。接口：`GET /api/projects/{id}/memories` → 记忆数组，`POST /api/projects/{id}/memories`（`{text, until?}`），
  改和删用现成的 `PATCH`、`DELETE /api/memories/{id}`。夜里整理时每个项目的记忆单独一组。删项目时它的记忆一起删。
- `GET /api/board` 的返回多 `counts`：`{todo, doing, waiting, done, cancelled}`，是不受分页影响的总数（侧栏的数字用它）。

### 各线要做的

- **wire**：上面“接口约定”全部；给任务的话（followup）不算卡片的新任务；接着做的任务让卡片回到进行中。
- **channels**：聊天软件（Telegram、飞书、钉钉、企业微信、Discord）和推送里，
  提问卡片（`kind: "question"`）显示问题，选项做成按钮，另有“不回答”；这个对话里正好有一个问题在等时，你直接回的一句话就是回答。
  说不准那一步的卡片（带 `doubt`）显示那一步是什么、几点开始的，三个按钮：再做一次、已经做成了、不做了停下任务（对应 `ok:true`、`ok:false, note:"done"`、`ok:false, note:"stop"`）。
  钉钉没有按钮，照它现在“回复 允许一次 #编号”的办法给出三种回复的写法。推送的文字跟着换（等你回答、有一步要你定）。
- **ui**：卡片页能改“交给谁”和“在这之后开始”；任务详情和任务卡片上显示所属的卡，点了打开卡片页；对话里从看板来的那条消息标出是哪张卡；
  记忆面板里看和改项目记忆；侧栏数字改用 `counts`；`Memory`、提问卡片的类型挪进 `types.ts`，去掉各处本地的重复定义。

## 第三波：团队（动手前要定）

打算这样接到 OSA 现有的东西上，有三处会改变现有用户看到的行为，动手前要确认：

- **团队**：`teams` 表（名字、简介、颜色、负责人），`bots` 加 `team_id`。Bot 列表和 Router 看到的“其他 Bot”按团队分组，负责人排第一。
- **一键雇用**：`osa/templates/teams/*.yaml`（一人公司、研发小队、内容工作室、研究台），一次建好几个 Bot、各自的技能和定时任务，最后可以一键把开场任务发给主 Bot。
  现有的 4 个单 Bot 模板留着。
- **主 Bot 当调度员**（要确认 1）：有团队以后，主 Bot 默认只调度：自己不派 Worker，活交给团队负责人（`message_bot`），由负责人拆给队友、汇总后回你一次；
  你点名某个成员时直接找它。只有它一个 Bot 时照旧自己干。设置里能关。
- **群聊**（要确认 2）：团队不自动建群。群聊照旧是你自己拉的，协调者机制不变。
- **负责人汇总**（要确认 3）：队友的结果先回负责人，负责人答复你一次；现在是谁做完谁的结果就直接出现在你和主 Bot 的对话里。

## 进度

- 2026-11-27：计划写好。开工前的基线（本机 macOS）：`OSA_IMPL=rust uv run pytest contract` 108 过、6 跳过；`cargo test` 21 过。
- 2026-11-27：第一波五条线做完并合进 `feat/tasks-board`。合并后本机（macOS）：`cargo test` 27 过；`OSA_IMPL=rust uv run pytest contract` 138 过、8 跳过；`uv run pytest -q` 618 过、2 跳过；翻译检查缺 0。
  收拢时改的：看板用任务自己的等待类型；给任务的话在 Router 的上下文里写明是用户说的；讨论串的回复数算上给任务的话、未读不算；侧栏预览按任务在等什么来写。
  记忆那条线多做了一件约定里没有的事：看起来像密码或密钥的内容不再记进记忆（提示放进保险库）。
- 2026-11-27：CI 加上 Rust 版的测试（`.github/workflows/ci.yml` 的 `server` 任务和 `docker` 任务里的接口测试），`Dockerfile.rust` 的测试镜像改成只跑 Rust 版、
  退出码就是测试结果、带上文档服务的程序。还没验证：要等第一波收拢后在本机 Docker 里跑一遍；GitHub 上的那一遍要 push 以后才看得到。
- 2026-11-27：第二波做完（wire、channels、ui 三条线合进 `feat/tasks-board`）。收拢时修了：几个任务同时结束时服务卡死（`tasklog.rs` 两把锁的先后相反）；
  按意思查记忆查得到项目记忆和过期的记忆；重启后应用还在连时任务调它的工具拿到“未知工具”；提问和说不准卡片了结的事件带上回答（聊天软件里网页上答的也写得出“已回答：……”）。
- 2026-11-28：文档写好：更新日志 `[未发布]`、功能一览（中英）、两份 README 和官网的亮点、配置（任务重试和停服务、记忆整理、看板和项目）、Docker、聊天软件、桌面 App、架构、路线图。第三波（团队）还没动工。
