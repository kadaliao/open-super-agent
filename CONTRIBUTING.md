# 参与贡献

感谢你愿意帮忙。

## 开始之前

- 想做比较大的改动，请先看看 [路线图](docs/roadmap.md)，再开 issue 讨论方向，免得做完和路线图冲突。
- 读一遍 [docs/architecture.md](docs/architecture.md) 里的“设计约定”。那些规则都是踩过坑之后定下来的（共用电脑排队、验证码一律交给人、DeepSeek 的 reasoning 回传等）。
- 本地怎么开发和测试，见 [docs/development.md](docs/development.md)。

## 给应用市场加应用

不用改代码：写一份清单放进 `osa/catalog/apps/`，在本机试通后提 PR。格式、图标和本机测试方法见 [给应用市场加一个应用](docs/apps.md)。

## 加一个内置的 Bot 模板

也不用改代码：写一个 YAML 放进 `osa/templates/`，本机装一次、让定时任务跑一次，`uv run python -m osa.templates` 检查通过后提 PR。格式和建议见 [写一个 Bot 模板](docs/templates.md)。

## 提交 PR

1. 从 `main` 新建分支，一个 PR 只做一件事。
2. 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：`feat:`、`fix:`、`docs:`、`refactor:`、`chore:` 等。
3. 确保 CI 通过（Python 能编译和导入、应用清单和内置模板没有错，前端能构建）。
4. 用户能感知到的改动请写进 `CHANGELOG.md` 的 `[未发布]`。
5. 改了界面就附截图，改了 Bot 的行为就附一段实际对话。

## 代码风格

- Python：标准库优先，函数短小，注释用中文，说明“为什么”而不是“做什么”。
- 前端：React 函数组件，状态集中放在 `store.ts`，样式写在 `styles.css`。
- 界面文案用简洁、口语化的中文。

## 不会接受的改动

- 绕过验证码或人机校验的功能
- 把密钥写进代码或配置文件
