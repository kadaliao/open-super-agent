# 本地电脑镜像：虚拟桌面 + Chromium + 终端 + VNC，供 Bot 操作和你实时观看；你接手时任务栏上有“浏览器”“终端”两个按钮
FROM debian:trixie-slim
# 国内可用 --build-arg APT_MIRROR=mirrors.ustc.edu.cn 加速
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi
ENV DEBIAN_FRONTEND=noninteractive LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc jwm xterm xdotool xclip scrot imagemagick dbus-x11 x11-utils \
      chromium fonts-noto-cjk fonts-noto-color-emoji locales tzdata \
      ca-certificates curl wget git jq procps python3 python3-pip python3-venv sudo less nano \
  && sed -i 's/# zh_CN.UTF-8/zh_CN.UTF-8/' /etc/locale.gen && locale-gen \
  && rm -rf /var/lib/apt/lists/*
# 容器里以 root 运行 Chromium 需要 --no-sandbox；浏览器配置放在 /data 里，登录状态能保留
# DevTools 端口只监听容器内的 127.0.0.1，给 Bot 的浏览器工具（CDP）用。后三个参数让被挡住的窗口照常加载（几个任务并行时要），
# 和 cdp_helper.FLAGS 一样：你从任务栏打开的浏览器和 Bot 开的是同一个，Bot 不用为了补参数重开它
RUN printf '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox --test-type --no-first-run --hide-crash-restore-bubble --disable-dev-shm-usage --user-data-dir=/data/chrome --window-size=1280,800 --window-position=0,0 --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 "--remote-allow-origins=*" --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling "$@"\n' \
      > /usr/local/bin/chromium && chmod +x /usr/local/bin/chromium
COPY start-desktop.sh computerd.py osa-terminal /usr/local/bin/
COPY jwmrc /root/.jwmrc
RUN chmod +x /usr/local/bin/start-desktop.sh /usr/local/bin/osa-terminal && mkdir -p /data/workspace
WORKDIR /data/workspace
CMD ["/usr/local/bin/start-desktop.sh"]
