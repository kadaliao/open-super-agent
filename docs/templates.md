# 写一个 Bot 模板

Bot 模板把一个 Bot 的名字、头像、职责、技能、要用的应用和定时任务打包成一个 YAML 文件。别人装上就得到一个一样的 Bot。
对话、记忆、资料库、应用的密钥不在模板里。

用户在侧栏 ＋ → **新建 Bot** 里挑内置模板，或者粘贴别人分享的链接、选一个文件来装。装之前会把内容全部列出来给他看。

## 最快的写法：先做一个 Bot，再导出

1. 新建一个 Bot，在右栏里写好职责、技能、定时任务，在对话里试几次，确认好用。
2. 右栏 → 详情 → **做成模板** → 导出模板文件（`<名字>.osabot.yaml`），或者生成分享链接发给别人。
3. 打开导出的文件改一改：补上 `description`；职责里写死的个人信息（邮箱、仓库名、城市）换成说明，让装的人自己改。

导出的是 Bot 现在的样子：它自己的技能，加上定时任务用到的共用技能。

## 格式

```yaml
osa_template: 1                 # 必须写，格式版本
name: 资讯助手                  # Bot 的名字，装的时候可以改
description: 每天早上把你关心的领域的新闻整理成一份早报   # 在模板列表里显示
avatar: {shape: star, color: "#f6c453"}
instructions: |                 # 职责与要求（人设），每次对话都会告诉 Bot，最多 4000 字
  你是我的资讯助手，帮我跟踪这些领域的新动态：人工智能、科技公司。
  （在 Bot 详情里把上面这一行改成你关心的领域。）
apps: [exa, fetch]              # 要用的应用，写应用市场里的名字；只是提示，装的人要自己去连
skills:
  - name: 早报                  # 聊天里用 /早报 调用
    description: 搜集过去 24 小时的新闻，整理成一份早报页面
    body: |                     # 做法，最多 2 万字
      1. 用 @exa 搜……
      2. 用 save_page 存成页面
routines:
  - name: 每天早报
    skill: 早报                 # 用哪个技能（模板里要有），或者写 prompt
    trigger: {kind: daily, times: ["08:00"], days: []}
```

- `avatar.shape` 是 `square sparkle clover flower heart house star circle` 之一，`color` 是 `#` 加 6 位十六进制，写错了用默认的。
- `trigger` 和定时任务一样：`{kind: daily, times: ["09:00"], days: [1,2,3,4,5]}`（周一是 1，空列表是每天）、
  `{kind: every, minutes: 60}`（至少 5 分钟）、`{kind: webhook}`（名字只能用字母、数字、`-`、`_`，装的时候重名会自动加后缀）。
- 最多 30 个技能、20 个定时任务、20 个应用。
- 用户可以选“先暂停定时任务”，所以定时任务里别做装上就不能撤回的事（比如直接发邮件），先让 Bot 起草、等人确认。

## 英文版（`en:`）

界面是英文时，模板列表、预览和装出来的 Bot 用 `en:` 里的内容；中文界面不看它。没有 `en:` 的模板（包括导出、分享的）两种界面都原样装。

```yaml
en:
  name: News assistant
  description: Every morning, turns news on the topics you follow into a brief
  instructions: |
    You're my news assistant. ...
    - Write in English. ...
  apps: [exa, fetch]            # 可选：英文用户习惯用的应用不一样时才写（比如邮件管家换成 gmail）
  skills:                       # 和上面的 skills 一样多、顺序一样，第几个就是翻译第几个
    - name: morning-brief       # 技能名不能有空格，用 - 连起来
      description: Collects news from the past 24 hours into a morning brief page
      body: |
        1. ...
  routines:                     # 和上面的 routines 一样多、顺序一样
    - name: Daily morning brief   # 只写 name、prompt；skill 不用写，会自动换成同一位置的英文技能名
```

- 能写的字段：`name`、`description`、`instructions`、`apps`、`skills`（每项 `name`、`description`、`body`）、`routines`（每项 `name`、`prompt`）。头像、触发方式两种语言共用，写在外面。
- 没写或写空的字段用中文的。`skills`、`routines` 不需要换的那一项写 `{}`。
- 这些是写给 Bot 的指令，按英文重新写一遍，别逐字翻：“用中文写”写成 “Write in English”。`@应用名` 和工具名（`save_page` 这类）保持原样。
- 加技能、定时任务时记得在 `en:` 里同一位置也加一项，数目对不上检查会报错。

## 写好模板的几条建议

- **职责里写清楚要用户改的地方**，比如“（在 Bot 详情里把上面的仓库换成你关心的）”。模板没有填空，装好以后用户在详情里改。
- **用 `@应用名` 指定应用**，并把它写进 `apps`。备选的应用（“用 Gmail 的话……”）别带 `@`，不然导出时会被当成要用的应用。
- **只读和起草优先**：发送、删除、付款这类操作本来就要用户确认，做法里也写上“先给我看，我说了再做”。
- **别写密码、token、个人信息**。模板会被分享出去，密钥让用户在应用页自己填。
- 结果要留存的，做法最后写“用 save_page 存成页面”，结果会进资料库，手机上也能看。

## 加进内置模板

内置模板放在 [`osa/templates/`](../osa/templates/)，文件名（小写字母、数字、`-`）就是模板的 id。

1. 写好文件，本机装一次（新建 Bot → 选择文件），让定时任务跑一次（右栏 → 定时 → 测试），确认结果像样。
2. 运行检查：

   ```bash
   uv run python -m osa.templates
   ```

   会检查格式，`apps` 里的应用应用市场里要有；做法里 `@` 了却没写进 `apps` 的会提示出来。中文、英文两个版本都检查，
   英文版里还有中文（没写 `en:` 或漏了字段）也会提示。内置模板请写上 `en:`。CI 里也跑这个检查。
3. 提 PR，说明这个模板适合谁、你实际用了多久。

不想放进内置的，生成分享链接或者把文件放在 GitHub 上就行：别人在新建 Bot 时粘贴 `…/template/…` 链接或 GitHub 上文件的链接就能装。
