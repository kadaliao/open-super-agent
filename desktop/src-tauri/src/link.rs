//! 服务器借用这台 Mac：App 连着服务器时，你在电脑面板里点“借给服务器”，网页向服务器要一个借用令牌，经桥接交给 App；
//! App 在这里弹窗问你，同意了才存进钥匙串（按服务器地址分开），并在后台起 Mac 上的小程序（osa/computer/agent.py），
//! 它主动连到服务器，替服务器上的 Bot 操作这台 Mac：浏览器、屏幕和鼠标键盘（要 App 有“辅助功能”和“屏幕录制”权限）、工作文件夹里的文件。
//! 网页里电脑选“本机”就是借出，选“Bot 电脑”就是收回；菜单栏里也能断开。

use std::sync::Arc;

use serde_json::json;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use url::Url;

use crate::{is_local, main_window, server, state, store, tr, tray};

/// 现在连着的服务器（只在“连接到我的服务器”时有）
fn remote(app: &AppHandle) -> Option<Url> {
    if is_local(app) {
        return None;
    }
    state(app).origin.lock().unwrap().clone()
}

fn key(origin: &Url) -> String {
    origin.origin().ascii_serialization()
}

fn ws_url(origin: &Url) -> String {
    let base = key(origin);
    let base = base.strip_prefix("https://").map(|h| format!("wss://{h}"))
        .or_else(|| base.strip_prefix("http://").map(|h| format!("ws://{h}")))
        .unwrap_or(base);
    format!("{base}/api/computer/link")
}

/// 连着服务器、借用过：起 Mac 上的小程序（已经在跑就不动）
pub fn start(app: &AppHandle) {
    let Some(origin) = remote(app) else { return };
    let Some((token, _)) = store::link_get(&key(&origin)) else { return refresh(app) };
    match server::agent_launch(app.path().resource_dir().ok(), &ws_url(&origin), &token) {
        Ok(launch) => state(app).agent.start(launch, |why| eprintln!("[desktop] agent gave up: {why}")),
        Err(e) => eprintln!("[desktop] agent: {e}"),
    }
    refresh(app);
}

/// 停掉小程序（换服务器、改成在这台电脑上运行、退出 App 时）
pub fn stop(app: &AppHandle) {
    let agent = Arc::clone(&state(app).agent);
    agent.stop();
}

/// 菜单栏上的“断开”跟着现在的状态走
pub fn refresh(app: &AppHandle) {
    let linked = remote(app).is_some_and(|o| store::link_get(&key(&o)).is_some());
    tray::set_link(app, linked);
}

/// 在 Mac 上弹窗问你。开发版的自动测试可以用 OSA_DESKTOP_AUTOCONFIRM=1 跳过（终端里没法点窗口）
fn confirm(app: &AppHandle, title: &str, text: &str, ok: &str) -> bool {
    if cfg!(debug_assertions) && std::env::var_os("OSA_DESKTOP_AUTOCONFIRM").is_some() {
        return true;
    }
    let mut d = app.dialog().message(text).title(title).kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(ok.into(), tr("取消", "Cancel").into()));
    if let Some(w) = main_window(app) {
        d = d.parent(&w);
    }
    d.blocking_show()
}

// ---------- 网页经 window.osaDesktop.link 调用的命令（只授权给服务器的网页界面） ----------

#[tauri::command]
pub fn desktop_link_state(app: AppHandle) -> serde_json::Value {
    let origin = remote(&app);
    let saved = origin.as_ref().and_then(|o| store::link_get(&key(o)));
    json!({
        "available": origin.is_some(),
        "linked": saved.is_some(),
        "id": saved.map(|(_, id)| id),
        "running": state(&app).agent.alive(),
    })
}

/// 借给服务器：问过你再存令牌、起小程序。返回你同不同意
#[tauri::command(async)]
pub fn desktop_link(app: AppHandle, token: String, id: i64) -> Result<bool, String> {
    let origin = remote(&app).ok_or_else(|| tr("只有连着服务器时才能借出这台 Mac", "Only available when connected to a server").to_string())?;
    let host = origin.host_str().unwrap_or_default().to_string();
    let text = if crate::zh() {
        format!("让 {host} 上的 Bot 使用这台 Mac？\n\n它们能看你的屏幕、用你的鼠标键盘操作这台 Mac 上的 App，和这台 Mac 独立运行时一样。每个任务第一次用屏幕会先在对话里问你；Bot 操作时你动一下鼠标或键盘它就停下。不能执行终端命令。\n\n随时可以在电脑面板选回“Bot 电脑”，或者在菜单栏图标里断开。")
    } else {
        format!("Let the bots on {host} use this Mac?\n\nThey can see your screen and use your mouse and keyboard in the apps on this Mac, just like when the app runs on its own. Each task asks in the chat before it first uses the screen; move the mouse or press a key and the bot stops. No terminal commands.\n\nSwitch back to “Bot computer” in the computer panel, or stop from the menu bar icon, any time.")
    };
    if token.trim().is_empty() || !confirm(&app, tr("借出这台 Mac", "Lend this Mac"), &text, tr("借给它", "Lend")) {
        return Ok(false);
    }
    if !store::link_set(&key(&origin), token.trim(), id) {
        return Err(tr("存不进钥匙串", "Couldn't save to the Keychain").into());
    }
    stop(&app); // 换了令牌：重起
    start(&app);
    Ok(true)
}

#[tauri::command]
pub fn desktop_unlink(app: AppHandle) {
    unlink(&app);
}

fn unlink(app: &AppHandle) {
    stop(app);
    if let Some(o) = remote(app) {
        store::link_clear(&key(&o));
    }
    refresh(app);
}

// ---------- 菜单栏 ----------

/// 菜单栏里断开：网页那边也让服务器作废这个令牌（osa-unlinked 事件，见 web/src/components/Computer.tsx）
pub fn menu_unlink(app: &AppHandle) {
    let id = remote(app).and_then(|o| store::link_get(&key(&o))).map(|(_, id)| id);
    unlink(app);
    if let (Some(id), Some(w)) = (id, main_window(app)) {
        let _ = w.eval(format!("window.dispatchEvent(new CustomEvent('osa-unlinked', {{ detail: {id} }}))"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_connects_to_the_same_server() {
        let u = |s: &str| ws_url(&Url::parse(s).unwrap());
        assert_eq!(u("https://osa.example.com/"), "wss://osa.example.com/api/computer/link");
        assert_eq!(u("https://osa.example.com:8443/"), "wss://osa.example.com:8443/api/computer/link");
        assert_eq!(u("http://127.0.0.1:8799/"), "ws://127.0.0.1:8799/api/computer/link");
    }
}
