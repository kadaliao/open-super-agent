# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

## [0.2.0] - 2026-11-04
### 新增
- **电脑（P2）**：Instabox 和本地 Docker 两个后端；所有 Bot 共用一台电脑，任务排队独占
- 右栏实时画面（noVNC）；接手和交还时自动放大画面；任务中途“请你帮忙”的卡片
- Router 可以直接看屏幕，也能直接做简单的电脑操作（打开网页、点击、输入、按键、滚动）
- 本地镜像支持 `APT_MIRROR` 构建参数，用国内镜像源加速

### 变更
- 遇到验证码、登录等步骤，立刻请用户接手，不再自己尝试
- 派任务时只写用户要求的内容；新指令和正在跑的任务冲突时，先取消旧任务

### 修复
- 中文输入丢字（改为通过剪贴板粘贴）
- Router 工具轮数用完时回复为空
- 浅色主题下代码块的文字看不见
- 桌面弹出 fbsetbg 报错框；Chromium 的“恢复页面”提示条

## [0.1.0] - 2026-11-04
### 新增
- **Bot 与对话（P1）**：多 Bot、流式输出、思考过程折叠、任务和结果卡片、设置页
- Router / Worker / Heavy / Watchdog 架构；Inline & Spill 记忆
- 定时和 webhook 触发器，规则过滤加 LLM 过滤
- 按角色、任务和天统计费用，区分高峰和空闲价格；每日预算
- MCP 应用接入；写操作前的确认卡片

[未发布]: https://github.com/kadaliao/open-super-agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kadaliao/open-super-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kadaliao/open-super-agent/releases/tag/v0.1.0
