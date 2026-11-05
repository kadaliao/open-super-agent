#!/bin/sh
# 启动虚拟桌面 :1、窗口管理器和 VNC（没有密码，只监听容器里的 127.0.0.1，由服务经 docker exec 转发）
W=${SCREEN_W:-1280}; H=${SCREEN_H:-800}
mkdir -p /data/workspace
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
Xvfb :1 -screen 0 ${W}x${H}x24 -nolisten tcp &
for i in $(seq 1 50); do DISPLAY=:1 xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
# fluxbox 默认主题设壁纸会弹 fbsetbg 报错框；改成纯色背景
mkdir -p /root/.fluxbox
grep -q rootCommand /root/.fluxbox/init 2>/dev/null || printf 'session.screen0.rootCommand: xsetroot -solid "#2b2b30"\n' >> /root/.fluxbox/init
printf 'background: none\n' > /root/.fluxbox/overlay
DISPLAY=:1 fluxbox >/dev/null 2>&1 &
DISPLAY=:1 xsetroot -solid '#2b2b30' 2>/dev/null
exec x11vnc -display :1 -forever -shared -nopw -localhost -rfbport 5900 -quiet -noxdamage
