//! 菜单栏图标：旁边显示有几个操作等你确认；菜单里打开窗口、在浏览器里打开、切换服务器、开机启动、退出，
//! 以及服务器借用这台 Mac 时的“断开”（见 link.rs），没借出时是灰的。

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_autostart::ManagerExt;

use crate::tr;

struct Autostart(CheckMenuItem<Wry>);
struct Link(MenuItem<Wry>);

pub fn create(app: &AppHandle) -> tauri::Result<()> {
    let item = |id: &str, text: &str| MenuItem::with_id(app, id, text, true, None::<&str>);
    let enabled = app.autolaunch().is_enabled().unwrap_or(false);
    let autostart = CheckMenuItem::with_id(app, "autostart", tr("开机时启动", "Open at login"), true, enabled, None::<&str>)?;
    let unlink = MenuItem::with_id(app, "unlink", tr("断开服务器借用这台 Mac", "Stop lending this Mac to the server"), false, None::<&str>)?;
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
            &item("update", tr("检查更新…", "Check for updates…"))?,
            &PredefinedMenuItem::separator(app)?,
            &item("quit", tr("退出 Open Super Agent", "Quit Open Super Agent"))?,
        ],
    )?;
    app.manage(Autostart(autostart));
    app.manage(Link(unlink));
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

/// 借出这台 Mac 时“断开”可点
pub fn set_link(app: &AppHandle, linked: bool) {
    if let Some(l) = app.try_state::<Link>() {
        let _ = l.0.set_enabled(linked);
    }
}
