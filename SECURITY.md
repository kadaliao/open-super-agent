# 安全说明

## 当前的安全边界

- **登录鉴权要自己打开。** 设置环境变量 `OSA_PASSWORD` 后，除登录页、分享链接、webhook 和前端静态文件外，页面、接口和 WebSocket 都要先登录；
  只有一个密码，没有多用户和权限区分。**不设置时不用登录**，能访问到这个端口的人就能和你的 Bot 对话、操作电脑、使用你接入的应用，
  这时请只在本机或内网（例如 Tailscale）使用。部署到公网时务必设置一个足够长的随机密码，并通过 HTTPS 访问。
- 登录状态是一个签了名的 HttpOnly cookie（SameSite=Lax，HTTPS 下带 Secure），签名密钥在 `data/session.key`。开启登录后，
  来源（Origin）和本站不一致的写请求与 WebSocket 会被拒绝。同一个 IP 10 分钟内输错 5 次会暂停登录。
- 本地电脑的 VNC 没有密码，只监听电脑容器里的 127.0.0.1，不映射到宿主机端口，由服务端经 `docker exec` 转发给浏览器。电脑里 Chromium 的 DevTools 端口（9222）只监听电脑内部，也不做端口映射。
- 用 Docker Compose 部署时，服务的容器挂了宿主机的 `docker.sock`（本地电脑要用），等于有宿主机的 root 权限；服务里运行的第三方应用（MCP）也在这个容器里，请只添加信得过的应用。电脑用 Instabox 时可以去掉这个挂载。
- 模型提供方的密钥只从环境变量读取。应用密钥可以存进保险库：用 Fernet 加密写进数据库，主密钥来自 `OSA_VAULT_KEY` 或 `data/vault.key`（权限 600）。两样都要妥善保管，`data/` 不进 git。
- 接口和模型只能看到保险库里密钥的名字；明文只在启动应用时注入它的进程环境变量。
- 设置了 `OSA_WEBHOOK_TOKEN` 后，webhook 必须带上 `X-OSA-Token` 才会被接受；没设置时只接受本机请求。
- 电脑里的浏览器登录状态由所有 Bot 共享，请留意让它登录了哪些账号。
- 页面的分享链接（`/s/<token>`）只是一个不可猜的地址，取消分享后失效。没设置 `OSA_PASSWORD` 时，“未分享”的页面同样能被访问到这个端口的人看到。
- Bot 生成的 HTML 页面在沙箱里显示：带 CSP 的 `sandbox` 和 `connect-src 'none'`，脚本拿不到本站的数据，也不能调接口或连 WebSocket（服务端还会拒绝来源为 `null` 的 WebSocket）。
- 资料库的文件接口只能访问共用电脑里的 `/data/workspace`。
- “演示一遍”会记下你在浏览器里点了什么、输入了什么。密码框和名字像密码、验证码、卡号的输入框只记 `******`，但这是按输入框判断的，
  录制时请不要在普通输入框里填敏感信息。录到的步骤会交给模型写技能草稿。
- 推送内容（标题和一两句摘要）经浏览器厂商的推送服务转发，按 RFC 8291 端到端加密，推送服务只看得到密文和设备地址。VAPID 私钥存在数据库里。

## 报告漏洞

请**不要**为安全问题公开提 issue。请通过 GitHub 的 [Private vulnerability reporting](https://github.com/kadaliao/open-super-agent/security/advisories/new) 私下报告。
