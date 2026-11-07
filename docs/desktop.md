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
  Bot 的电脑默认用这台电脑上装的 Chrome（开一个单独的配置目录，和你日常用的分开），不用 Docker，
  说明见 [配置 → 本机 Chrome](configuration.md#电脑)。没装 Chrome 的话先装一个；也可以在电脑面板上方换成本地 Docker 或 Instabox。
- **连接到我的服务器**：填地址（比如 `https://osa.example.com`），在 App 里登录。App 只是一个客户端，服务和数据都在服务器上。
  Bot 用的电脑也在服务器那边（电脑面板写着“服务器上的 Chrome / Docker”或 Instabox），用不了这台 Mac 上的 Chrome；
  要用它，从菜单栏 → 切换服务器… 改成“在这台电脑上运行”。

## 和网页比多了什么

- 关掉窗口只是藏到菜单栏，服务和定时任务接着跑；退出要用菜单栏图标 → 退出，或者 ⌘Q。
- Bot 的回复、任务结果、等你确认的操作发系统通知（正看着那个对话时不发），点通知打开对应的对话；菜单栏图标旁边显示有几个操作等你确认。
- ⌘⇧空格 呼出或藏起窗口。
- 菜单栏图标 → 开机时启动：开机后在菜单栏里待着，不弹窗口。
- 把文件拖进窗口就能发给 Bot（网页版也能）。
- 别的网站的链接用系统浏览器打开；应用的登录授权在 App 里的小窗完成。
- 自动更新：启动半分钟后和之后每 6 小时看一次有没有新版，有就在后台装好，下次打开时生效。菜单栏图标 → 检查更新 可以手动看。

暂时没有：语音输入（App 里的 WebKit 不支持网页的语音识别）、控制整个桌面（Bot 现在只操作它自己的 Chrome）。

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
