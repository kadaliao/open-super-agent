# 安全说明

## 当前的安全边界

- **网页没有登录鉴权。** 能访问到这个端口的人，就能和你的 Bot 对话、操作电脑、使用你接入的应用。请只在本机或内网（例如 Tailscale）使用，**不要暴露到公网**。
- 本地电脑的 VNC 只监听 `127.0.0.1`，由服务端转发给浏览器。电脑里 Chromium 的 DevTools 端口（9222）只监听电脑内部，不做端口映射。
- 模型提供方的密钥只从环境变量读取。应用密钥可以存进保险库：用 Fernet 加密写进数据库，主密钥来自 `OSA_VAULT_KEY` 或 `data/vault.key`（权限 600）。两样都要妥善保管，`data/` 不进 git。
- 接口和模型只能看到保险库里密钥的名字；明文只在启动应用时注入它的进程环境变量。
- 设置了 `OSA_WEBHOOK_TOKEN` 后，webhook 必须带上 `X-OSA-Token` 才会被接受；没设置时只接受本机请求。
- 电脑里的浏览器登录状态由所有 Bot 共享，请留意让它登录了哪些账号。
- 页面的分享链接（`/s/<token>`）只是一个不可猜的地址，取消分享后失效。整个服务目前没有鉴权，所以“未分享”的页面同样能被访问到这个端口的人看到。
- Bot 生成的 HTML 页面在沙箱里显示：带 CSP 的 `sandbox` 和 `connect-src 'none'`，脚本拿不到本站的数据，也不能调接口或连 WebSocket（服务端还会拒绝来源为 `null` 的 WebSocket）。
- 资料库的文件接口只能访问共用电脑里的 `/data/workspace`。

## 报告漏洞

请**不要**为安全问题公开提 issue。请通过 GitHub 的 [Private vulnerability reporting](https://github.com/kadaliao/open-super-agent/security/advisories/new) 私下报告。
