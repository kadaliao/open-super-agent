# iPhone App（开发中）

iOS 的原生 App，用 Tauri 2 做（和桌面 App 同一套），代码在 `mobile/`。App 只是客户端：手机上不跑服务，连的是你部署好的服务器，
界面就是服务器自己的网页界面，服务器升级界面就跟着升级。先在中国大陆以外的地区上架，先发 TestFlight 测试版。

## 连接服务器

- 在电脑上打开 设置 → 手机 App，用 iPhone 的相机扫二维码，直接进 App 并登录。二维码里是服务器地址和一张 5 分钟内有效、
  只能用一次的登录票（`POST /api/pair` 发的，要先登录）；没开登录的服务器只有地址。
- 也可以在 App 里手动填地址，在服务器的登录页输密码。
- 手机要能打开这个地址：局域网 IP、Tailscale 或者自己的域名都行，`localhost` 不行。

## 推送

自己部署的服务器拿不到我们的苹果推送密钥，所以推送经过一个中转（`push.cclsp.top`，代码是 `osa/relay.py`）。
对服务器来说它就是一个普通的网页推送服务：

1. App 在中转登记手机的推送令牌，拿到一个地址 `https://push.cclsp.top/w/<随机 id>`，再在手机上生成一对加密用的密钥。
2. 地址和公钥交给服务器的 `/api/push/subscribe`，和浏览器订阅网页推送一样。
3. 服务器照常加密（RFC 8291）、签名（VAPID）后发到这个地址。中转验签名（只认登记时那台服务器的公钥）、限速，
   把密文原样转给苹果；手机上的通知扩展用自己的私钥解开后显示。

中转**看不到**通知的内容，也拿不到解密的钥匙。它看得到的是：手机的推送令牌、手机和服务器的 IP、服务器的推送公钥
（相当于一个固定编号）、推送的时间和大小。它不写访问日志，90 天没成功推送过的地址自动清掉。

限制：每个地址每分钟 60 条、每天 2000 条；每条加密后不超过 2900 字节（服务器的推送标题最长 80 字、正文 200 字，远小于这个）。
苹果说令牌失效时中转回 410，服务器就删掉这个订阅。

### 自己运行中转

中转绑定我们的苹果开发者账号和 App 的 bundle id，别人自己跑一个也发不到官方 App 上，所以一般不需要。
要自己打包 App 的话：

```bash
uv sync --group relay          # 连苹果要 HTTP/2（h2）
OSA_RELAY_ORIGIN=https://push.example.com OSA_RELAY_DB=/var/lib/osa-relay/relay.db \
APNS_KEY_FILE=/path/AuthKey_XXXX.p8 APNS_KEY_ID=XXXX APNS_TEAM_ID=<团队 ID> APNS_TOPIC=<bundle id> \
uv run uvicorn osa.relay:app --port 8790 --no-access-log --proxy-headers
```

systemd 和 Caddy 的配置见 `deploy/relay/`。
