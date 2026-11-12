//! 菜单栏图标：旁边显示有几个操作等你确认；菜单里打开窗口、在浏览器里打开、切换服务器、开机启动、退出，
//! 以及“不再让服务器派任务到这台 Mac”（见 link.rs），没允许时是灰的；新版本装好后“检查更新”变成“重启更新”。

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_autostart::ManagerExt;

use crate::tr;

struct Autostart(CheckMenuItem<Wry>);
struct Link(MenuItem<Wry>);
struct Update(MenuItem<Wry>);

pub fn create(app: &AppHandle) -> tauri::Result<()> {
    let item = |id: &str, text: &str| MenuItem::with_id(app, id, text, true, None::<&str>);
    let enabled = app.autolaunch().is_enabled().unwrap_or(false);
    let autostart = CheckMenuItem::with_id(app, "autostart", tr("开机时启动", "Open at login"), true, enabled, None::<&str>)?;
    let unlink = MenuItem::with_id(app, "unlink", tr("不再让服务器派任务到这台 Mac", "Stop taking tasks from the server on this Mac"), false, None::<&str>)?;
    let update = item("update", tr("检查更新…", "Check for updates…"))?;
    let menu = Menu::with_items(
        app,
        &[
            &item("show", tr("打开窗口", "Open window"))?,
            &item("browser", tr("在浏览器里打开", "Open in browser"))?,
            &PredefinedMenuItem::separator(app)?,
            &item("switch", tr("切换服务器…", "Switch server…"))?,
            &unlink,
            &PredefinedMenuItem::separator(app)?,
            &autostart,
            &item("logs", tr("打开日志文件夹", "Open logs folder"))?,
            &update,
            &PredefinedMenuItem::separator(app)?,
            &item("quit", tr("退出 Open Super Agent", "Quit Open Super Agent"))?,
        ],
    )?;
    app.manage(Autostart(autostart));
    app.manage(Link(unlink));
    app.manage(Update(update));
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Open Super Agent")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, e| crate::on_menu(app, e.id().as_ref()));
    // 单色模板图标：菜单栏是深色还是浅色，系统自己反色
    match tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png")) {
        Ok(icon) => tray = tray.icon(icon).icon_as_template(true),
        Err(_) => {
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
        }
    }
    tray.build(app)?;
    Ok(())
}

/// 菜单栏图标旁边的数字：等你确认的操作有几个，没有就不显示
pub fn set_count(app: &AppHandle, count: i64) {
    if let Some(t) = app.tray_by_id("main") {
        let _ = t.set_title(if count > 0 { Some(count.to_string()) } else { None });
    }
}

pub fn toggle_autostart(app: &AppHandle) {
    let auto = app.autolaunch();
    let on = !auto.is_enabled().unwrap_or(false);
    let _ = if on { auto.enable() } else { auto.disable() };
    if let Some(item) = app.try_state::<Autostart>() {
        let _ = item.0.set_checked(auto.is_enabled().unwrap_or(on));
    }
}

/// 允许了服务器派任务到这台 Mac 时“不再让……”可点
pub fn set_link(app: &AppHandle, linked: bool) {
    if let Some(l) = app.try_state::<Link>() {
        let _ = l.0.set_enabled(linked);
    }
}

/// 新版本装好、等重启时，“检查更新…”换成“重启更新到 x.y.z”（点了重启，见 lib.rs 的 on_menu）
pub fn set_update(app: &AppHandle, version: &str) {
    if let Some(u) = app.try_state::<Update>() {
        let _ = u.0.set_text(format!("{}{version}", tr("重启更新到 ", "Restart to update to ")));
    }
}
