#!/bin/sh
# 启动虚拟桌面 :1、窗口管理器（带任务栏）和 VNC（没有密码，只监听容器里的 127.0.0.1，由服务经 docker exec 转发）。
# 用 Docker Compose 部署时（设了 OSA_COMPUTER_SOCKET）另外起 computerd：服务不碰 Docker，经共用卷里的 unix socket 让这里执行命令、转发 VNC
W=${SCREEN_W:-1280}; H=${SCREEN_H:-800}
mkdir -p /data/workspace
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
Xvfb :1 -screen 0 ${W}x${H}x24 -nolisten tcp &
for i in $(seq 1 50); do DISPLAY=:1 xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
# 桌面（JWM，配置见 jwmrc）：底部任务栏左边是“浏览器”“终端”两个按钮，你接手电脑时用；中间是开着的窗口，右边是时钟
DISPLAY=:1 jwm >/dev/null 2>&1 &
[ -n "$OSA_COMPUTER_SOCKET" ] && python3 /usr/local/bin/computerd.py &
exec x11vnc -display :1 -forever -shared -nopw -localhost -rfbport 5900 -quiet -noxdamage
