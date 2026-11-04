# 安全说明

## 当前的安全边界

- **网页没有登录鉴权。** 能访问到这个端口的人，就能和你的 Bot 对话、操作电脑、使用你接入的应用。请只在本机或内网（例如 Tailscale）使用，**不要暴露到公网**。
- 本地电脑的 VNC 只监听 `127.0.0.1`，由服务端转发给浏览器。
- 密钥只从环境变量读取，不会写进 `config.yaml` 或数据库。
- 设置了 `OSA_WEBHOOK_TOKEN` 后，webhook 必须带上 `X-OSA-Token` 才会被接受；没设置时只接受本机请求。
- 电脑里的浏览器登录状态由所有 Bot 共享，请留意让它登录了哪些账号。

## 报告漏洞

请**不要**为安全问题公开提 issue。请通过 GitHub 的 [Private vulnerability reporting](https://github.com/kadaliao/open-super-agent/security/advisories/new) 私下报告。
