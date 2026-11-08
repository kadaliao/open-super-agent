# 桌面 App

macOS 的桌面 App，Apple 芯片和 Intel 都有。Windows 版以后再做。

到 [发布页](https://github.com/kadaliao/open-super-agent/releases) 下载对应芯片的 dmg，或者用 Homebrew：

```bash
brew install --cask kadaliao/tap/open-super-agent
```

App 自己会检查更新，所以 `brew upgrade` 默认跳过它。`brew uninstall --cask --zap open-super-agent` 会连数据一起删。

<p align="center"><img src="images/macos-app.png" width="860" alt="桌面 App：左边是 Bot 列表，中间的对话里有后台任务和等你确认的卡片，右边是 Bot 正在操作的 Chrome"></p>

## 两种用法

第一次打开时选，之后在菜单栏图标 → 切换服务器 里换：

- **在这台电脑上运行**：App 自带运行环境，不用装 Docker、Python。数据在 `~/Library/Application Support/open-super-agent`：
  - `data/`：服务的数据目录（数据库、密钥、工作目录），和服务器部署时的 `data/` 一样，可以用 设置 → 备份与迁移 搬到服务器上；
  - `logs/server.log`：服务的日志；
  - `desktop.json`：选的用法、服务器地址、端口。

  服务只监听本机的 `127.0.0.1:18765`（被占用时换一个并记住），开了登录：密码是第一次打开时随机生成的，存在钥匙串里（“Open Super Agent (desktop)”），
  App 自己登录，不用你输。菜单栏里的“在浏览器里打开”会让浏览器也登录上。
  Bot 用的就是这台电脑（本机），不用 Docker，电脑面板上的画面跟着 Bot 走。它有自己的浏览器窗口（用你装的 Chrome，配置和你日常用的分开），
  没装 Chrome 的话先装一个。给 Open Super Agent 打开“辅助功能”和“屏幕录制”权限后，Bot 还能看整个屏幕、用你的鼠标键盘操作微信、访达、
  备忘录这些 App（电脑面板上有“去打开”按钮）；每个任务第一次用屏幕先问你，Bot 操作时你动一下鼠标或键盘它就停下。
  细节见 [配置 → 电脑](configuration.md#电脑)。
- **连接到我的服务器**：填地址（比如 `https://osa.example.com`），在 App 里登录。App 只是一个客户端，服务和数据都在服务器上。
  电脑面板上方在 **Bot 电脑**（服务器那边给 Bot 的电脑）和 **本机** 之间选。

  **选“本机”**：Mac 上会弹窗问你，同意后服务器上的 Bot 就用这台 Mac，和 App 独立运行时一样（浏览器、屏幕和鼠标键盘），只是不能执行终端命令。
  选回“Bot 电脑”就收回；菜单栏图标 → 断开服务器借用这台 Mac 也行。
  - App 在后台起一个小程序（`osa/computer/agent.py`），它主动连到服务器（`wss://服务器/api/computer/link`），服务器不用连进你家里的网络，
    断线自动重连。凭据是服务器发的随机令牌，存在钥匙串里（“Open Super Agent (desktop)”，按服务器地址分开）；日志在 `logs/agent.log`。
  - Bot 的浏览器和“在这台电脑上运行”时是同一个配置目录，登录过的网站两边都能用。
  - 一次只借一台。退出 App、Mac 睡着时 Bot 用不了它，任务会说“你的 Mac 没连上”。在服务器那边作废了的话，App 隔几分钟才再试。
  - 在别的设备上（手机、浏览器）也能在 Bot 电脑和这台 Mac 之间切换，只是第一次借出要在这台 Mac 的 App 里选。

## 和网页比多了什么

- 关掉窗口只是藏到菜单栏，服务和定时任务接着跑；退出要用菜单栏图标 → 退出，或者 ⌘Q。
- Bot 的回复、任务结果、等你确认的操作发系统通知（正看着那个对话时不发），点通知打开对应的对话；菜单栏图标旁边显示有几个操作等你确认。
- ⌘⇧空格 呼出或藏起窗口。
- 菜单栏图标 → 开机时启动：开机后在菜单栏里待着，不弹窗口。
- 把文件拖进窗口就能发给 Bot（网页版也能）。
- 别的网站的链接用系统浏览器打开；应用的登录授权在 App 里的小窗完成。
- 自动更新：启动半分钟后和之后每 6 小时看一次有没有新版，有就在后台装好，下次打开时生效。菜单栏图标 → 检查更新 可以手动看。

暂时没有：语音输入（App 里的 WebKit 不支持网页的语音识别）。

## 从源码运行

需要 [Rust](https://rustup.rs)、Node ≥ 20、uv。

```bash
(cd web && npm ci && npm run build)          # 服务端要用构建好的前端
cd desktop && npm ci
npx tauri dev                                # 开发版：用仓库里的 .venv 起服务
```

开发版的数据也在 `~/Library/Application Support/open-super-agent`。测试时用 `OSA_DESKTOP_HOME=/某个临时目录` 换个地方，不碰真正的数据；
这时本机服务的密码存在那个目录里，不进钥匙串。

打一个能单独用的 App：

```bash
desktop/scripts/build-runtime.sh             # 准备 Python 运行环境（desktop/build/），可以加 aarch64 或 x86_64
cd desktop && npx tauri build --bundles app  # 带自动更新包时要设 TAURI_SIGNING_PRIVATE_KEY，见下面
```

运行环境：独立版 Python 3.13（python-build-standalone）+ 按 `uv.lock` 装的依赖 + uv（uvx 应用要用），去掉了 pip、调试符号这些，约 106 MB；
`.pyc` 写到数据目录旁边。Intel 版的 cryptography 用 48.0：从 49.0 起它不再提供 Intel Mac 的安装包。

## 发布

打 `v*` 的 tag 时，`.github/workflows/desktop.yml` 打两个 dmg 和自动更新包，传到这个版本的 release，
再更新 `desktop-updates` 这个 release 里的 `latest.json`（装好的 App 从这里看有没有新版，不要删）。tag 里带 `-` 的（比如 `v0.7.0-preview.1`）发成 pre-release。
`desktop/package.json` 的版本号要和 tag 一致，不然打包会失败。
正式版（不带 `-`）发出后，再改 [kadaliao/homebrew-tap](https://github.com/kadaliao/homebrew-tap) 里的 `Casks/open-super-agent.rb`：`version` 和两个 `sha256`（`gh release view vx.y.z --json assets` 能看到每个 dmg 的 sha256）。

仓库的 Secrets：

| 名字 | 用途 |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY`、`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | 给自动更新包签名。公钥在 `desktop/src-tauri/tauri.conf.json`，换了密钥，已经装好的 App 就收不到更新了 |
| `APPLE_CERTIFICATE`、`APPLE_CERTIFICATE_PASSWORD` | “Developer ID Application” 证书导出的 `.p12`（base64）和它的密码 |
| `APPLE_API_KEY_P8`、`APPLE_API_KEY_ID`、`APPLE_API_ISSUER` | App Store Connect 的 API key（`.p8` 文件的内容）、Key ID 和 Issuer ID，公证用 |

配了 Apple 的那几项时：App 里带的 Python 运行环境先逐个签名（`desktop/scripts/sign-runtime.sh`），Tauri 签名并公证 App，CI 再公证 dmg 并把结果钉上，下载后双击就能打开。没配的话安装包只有临时签名：下载后第一次打开会被拦下，要去 系统设置 → 隐私与安全性 点“仍要打开”。
