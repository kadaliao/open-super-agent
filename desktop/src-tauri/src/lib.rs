//! Open Super Agent 桌面版：一个窗口打开服务器自己的网址（本机的 127.0.0.1，或者你的服务器），
//! 再补上网页做不到的：系统通知、菜单栏、全局快捷键、开机启动，以及在这台电脑上起停服务。

mod link;
mod server;
mod store;
mod tray;

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::json;
use tauri::ipc::CapabilityBuilder;
use tauri::webview::{DownloadEvent, NewWindowResponse, PageLoadEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_updater::UpdaterExt;
use url::Url;

/// 页面加载前放进去的 window.osaDesktop（网页里的 web/src/desktop.ts 会用它）。
/// 只有当前服务器的网页界面拿到了调用这几个命令的授权，别的网页即使有这个对象也调不动。
/// 来源是 null 的（被 CSP sandbox 隔离的页面，比如 Bot 生成的报告页）不放
const BRIDGE: &str = r#"(function () {
  if (window.osaDesktop || window.top !== window || !/^https?:$/.test(location.protocol) || location.origin === 'null') return;
  var call = function (cmd, args) {
    try {
      window.__TAURI_INTERNALS__.invoke(cmd, args).catch(function (e) {
        if (cmd !== 'desktop_log') call('desktop_log', { msg: cmd + ' failed: ' + String(e) });
      });
    } catch (e) {}
  };
  var ask = function (cmd, args) { return window.__TAURI_INTERNALS__.invoke(cmd, args || {}); };
  window.osaDesktop = {
    notify: function (title, body, chatId, active) { call('desktop_notify', { title: String(title), body: String(body), chatId: chatId == null ? null : chatId, active: !!active }); },
    setBadge: function (count) { call('desktop_set_badge', { count: count | 0 }); },
    openExternal: function (url) { call('desktop_open_external', { url: String(url) }); },
    link: {
      state: function () { return ask('desktop_link_state'); },
      connect: function (token, id) { return ask('desktop_link', { token: String(token), id: id | 0 }); },
      disconnect: function () { return ask('desktop_unlink'); }
    },
    speech: false
  };
})();"#;

pub struct State {
    cfg: Mutex<store::Config>,
    origin: Mutex<Option<Url>>, // 当前打开的服务器
    phase: Mutex<(String, String)>, // 启动页显示的状态："starting" / "error" 和说明
    server: Arc<server::Server>,
    agent: Arc<server::Server>, // 服务器借用这台 Mac 时在后台跑的小程序（link.rs）
    granted: Mutex<Vec<String>>, // 已经给过桥接授权的服务器
    popups: AtomicU32,
}

pub fn zh() -> bool {
    sys_locale::get_locale().map(|l| l.starts_with("zh")).unwrap_or(true)
}

pub fn tr(zh_text: &'static str, en: &'static str) -> &'static str {
    if zh() { zh_text } else { en }
}

fn state(app: &AppHandle) -> tauri::State<'_, State> {
    app.state::<State>()
}

fn set_phase(app: &AppHandle, phase: &str, message: impl Into<String>) {
    *state(app).phase.lock().unwrap() = (phase.into(), message.into());
}

fn is_local(app: &AppHandle) -> bool {
    state(app).cfg.lock().unwrap().mode.as_deref() == Some("local")
}

fn setup_url(query: &str) -> Url {
    let base = if cfg!(windows) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
    Url::parse(&format!("{base}{query}")).unwrap()
}

pub fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window("main")
}

pub fn show_main(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn show_setup(app: &AppHandle, query: &str) {
    if let Some(w) = main_window(app) {
        let _ = w.navigate(setup_url(query));
    }
}

// ---------- 两种用法 ----------

/// 在这台电脑上运行：起服务，等它好了，用一次性的票登录，再打开页面
fn start_local(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let st = state(&app);
        set_phase(&app, "starting", "");
        link::stop(&app); // 在这台电脑上运行时，用的就是本机电脑，不借给服务器
        {
            let mut cfg = st.cfg.lock().unwrap();
            cfg.mode = Some("local".into());
            store::save(&cfg);
        }
        let port = pick_port(&app);
        let secret = store::secret();
        let launch = match server::launch(app.path().resource_dir().ok(), port, &secret) {
            Ok(l) => l,
            Err(e) => return set_phase(&app, "error", e),
        };
        let failed = app.clone();
        st.server.start(launch, move |why| {
            set_phase(&failed, "error", format!("{why}\n\n{}", server::log_tail()));
            show_setup(&failed, "");
        });
        let srv = Arc::clone(&st.server);
        if !server::wait_ready(port, Duration::from_secs(120), || srv.alive()) {
            if state(&app).phase.lock().unwrap().0 != "error" {
                set_phase(&app, "error", format!("{}\n\n{}", tr("服务没能启动。", "The service didn't start."), server::log_tail()));
            }
            return;
        }
        let origin = Url::parse(&format!("http://127.0.0.1:{port}/")).unwrap();
        login_local(&app, &origin, "/");
    });
}

/// 端口定下来就一直用它；被别的程序占了才换一个并记住
fn pick_port(app: &AppHandle) -> u16 {
    let st = state(app);
    let mut cfg = st.cfg.lock().unwrap();
    let mut port = cfg.port.unwrap_or(store::DEFAULT_PORT);
    if !server::port_free(port) {
        // 可能是上次 App 异常退出留下的服务，它两秒内会自己退出
        std::thread::sleep(Duration::from_secs(3));
        if !server::port_free(port) {
            port = server::any_free_port();
        }
    }
    cfg.port = Some(port);
    store::save(&cfg);
    port
}

fn login_local(app: &AppHandle, origin: &Url, next: &str) {
    let port = origin.port().unwrap_or(store::DEFAULT_PORT);
    match server::ticket(port, &store::secret()) {
        Ok(t) => {
            let mut u = origin.join("/login/ticket").unwrap();
            u.query_pairs_mut().append_pair("t", &t).append_pair("next", next);
            set_phase(app, "", "");
            open_server(app, origin.clone(), Some(u));
        }
        Err(e) => {
            set_phase(app, "error", format!("{}{e}", tr("登录本机服务失败：", "Couldn't sign in to the local service: ")));
            show_setup(app, "");
        }
    }
}

/// 打开服务器：记下它的地址（只在它里面跳转），给它桥接的授权，窗口跳过去
fn open_server(app: &AppHandle, origin: Url, first: Option<Url>) {
    *state(app).origin.lock().unwrap() = Some(origin.clone());
    grant(app, &origin);
    if let Some(w) = main_window(app) {
        let _ = w.navigate(first.unwrap_or(origin));
    }
}

/// 网页界面自己会落到的路径（`/`、`/apps`、`/library`、`/library/pages/12`，见 web/src/App.tsx）。
/// 服务器上别的页面（Bot 生成的报告页 /api/pages/1/raw、分享页 /s/…）不给授权，免得它们里面的脚本调 App 的命令
const UI_PATHS: &str = r"/:p(|apps/?|library/?|library/pages/\d+/?)";

fn server_pattern(origin: &Url) -> String {
    format!("{}{UI_PATHS}", origin.origin().ascii_serialization())
}

fn grant(app: &AppHandle, origin: &Url) {
    let key = origin.origin().ascii_serialization();
    let st = state(app);
    let mut granted = st.granted.lock().unwrap();
    if granted.contains(&key) {
        return;
    }
    let cap = CapabilityBuilder::new(format!("server-{}", granted.len()))
        .remote(server_pattern(origin))
        .local(false)
        .window("main")
        .permission("allow-desktop-notify")
        .permission("allow-desktop-set-badge")
        .permission("allow-desktop-open-external")
        .permission("allow-desktop-log")
        .permission("allow-desktop-link-state")
        .permission("allow-desktop-link")
        .permission("allow-desktop-unlink");
    if app.add_capability(cap).is_ok() {
        granted.push(key);
    }
}

/// 用户填的地址只留协议、域名和端口；没写协议的当成 https
fn normalize(input: &str) -> Option<Url> {
    let s = input.trim();
    let s = if s.contains("://") { s.to_string() } else { format!("https://{s}") };
    let u = Url::parse(&s).ok()?;
    if !matches!(u.scheme(), "http" | "https") || u.host_str().is_none() {
        return None;
    }
    Url::parse(&format!("{}/", u.origin().ascii_serialization())).ok()
}

// ---------- 启动页调用的命令 ----------

#[tauri::command]
fn setup_state(app: AppHandle) -> serde_json::Value {
    let st = state(&app);
    let cfg = st.cfg.lock().unwrap().clone();
    let (phase, message) = st.phase.lock().unwrap().clone();
    json!({ "mode": cfg.mode, "url": cfg.url, "phase": phase, "message": message, "zh": zh() })
}

#[tauri::command]
fn choose_local(app: AppHandle) {
    start_local(&app);
}

#[tauri::command(async)]
fn choose_remote(app: AppHandle, url: String) -> Result<(), String> {
    let origin = normalize(&url).ok_or_else(|| tr("地址不对，比如 https://osa.example.com", "That address doesn't look right, e.g. https://osa.example.com").to_string())?;
    let base = origin.origin().ascii_serialization();
    server::check_remote(&base).map_err(|e| format!("{}{e}", tr("连不上这个地址：", "Can't reach this address: ")))?;
    let st = state(&app);
    st.server.stop();
    link::stop(&app);
    {
        let mut cfg = st.cfg.lock().unwrap();
        cfg.mode = Some("remote".into());
        cfg.url = Some(base);
        store::save(&cfg);
    }
    set_phase(&app, "", "");
    open_server(&app, origin, None);
    link::start(&app); // 借给过这个服务器的话接着借
    Ok(())
}

#[tauri::command]
fn open_logs(app: AppHandle) {
    let _ = app.opener().open_path(store::log_dir().display().to_string(), None::<&str>);
}

// ---------- 网页通过 window.osaDesktop 调用的命令 ----------

/// 开发版把网页发来的调用打到终端，方便确认桥接通了
fn trace(what: std::fmt::Arguments) {
    if cfg!(debug_assertions) {
        eprintln!("[desktop] {what}");
    }
}

#[tauri::command]
fn desktop_notify(app: AppHandle, title: String, body: String, chat_id: Option<i64>, active: Option<bool>) {
    let looking = main_window(&app).is_some_and(|w| w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false));
    trace(format_args!("notify {title:?} {body:?} chat={chat_id:?} active={active:?} looking={looking}"));
    if looking && active.unwrap_or(false) {
        return; // 你正看着这个对话
    }
    let mut n = app.notification().builder().title(title).body(body);
    if let Some(id) = chat_id {
        n = n.id(id as i32); // 同一个对话的通知用同一个 id：新的顶掉旧的，点了也知道打开哪个对话
    }
    let _ = n.show();
}

/// 开发版：网页里桥接调用失败的原因
#[tauri::command]
fn desktop_log(msg: String) {
    trace(format_args!("{msg}"));
}

#[tauri::command]
fn desktop_set_badge(app: AppHandle, count: i64) {
    trace(format_args!("badge {count}"));
    tray::set_count(&app, count);
    if let Some(w) = main_window(&app) {
        let _ = w.set_badge_count(if count > 0 { Some(count) } else { None });
    }
}

#[tauri::command]
fn desktop_open_external(app: AppHandle, url: String) {
    trace(format_args!("open {url}"));
    if let Ok(u) = Url::parse(&url) {
        if matches!(u.scheme(), "http" | "https" | "mailto") {
            let _ = app.opener().open_url(u.as_str(), None::<&str>);
        }
    }
}

// ---------- 自动更新 ----------

static UPDATING: AtomicBool = AtomicBool::new(false);

/// 启动半分钟后看一次有没有新版，之后每 6 小时看一次。开发版不看
fn start_update_loop(app: &AppHandle) {
    if cfg!(debug_assertions) {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(30));
        loop {
            tauri::async_runtime::block_on(check_update(&app, false));
            std::thread::sleep(Duration::from_secs(6 * 3600));
        }
    });
}

/// 有新版就在后台下载、装好（换掉 App 本身），下次打开时生效。manual：从菜单栏点的，结果都告诉你
async fn check_update(app: &AppHandle, manual: bool) {
    if UPDATING.swap(true, SeqCst) {
        return;
    }
    let result = async {
        let Some(update) = app.updater()?.check().await? else {
            return Ok::<_, tauri_plugin_updater::Error>(None);
        };
        let version = update.version.clone();
        update.download_and_install(|_, _| {}, || {}).await?;
        Ok(Some(version))
    }
    .await;
    UPDATING.store(false, SeqCst);
    let current = app.package_info().version.to_string();
    let (title, body) = match result {
        Ok(Some(v)) => (format!("{}{v}", tr("已经更新到 ", "Updated to ")), tr("下次打开 App 时生效", "It takes effect the next time you open the app").to_string()),
        Ok(None) if manual => (tr("已经是最新版", "You're up to date").to_string(), current),
        Err(e) if manual => (tr("检查更新失败", "Couldn't check for updates").to_string(), e.to_string()),
        _ => return,
    };
    let _ = app.notification().builder().title(title).body(body).show();
}

// ---------- 菜单栏 ----------

pub fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "show" => show_main(app),
        "browser" => {
            let app = app.clone();
            std::thread::spawn(move || open_in_browser(&app));
        }
        "switch" => {
            show_main(app);
            show_setup(app, "?switch=1");
        }
        "logs" => open_logs(app.clone()),
        "update" => {
            let app = app.clone();
            std::thread::spawn(move || tauri::async_runtime::block_on(check_update(&app, true)));
        }
        "autostart" => tray::toggle_autostart(app),
        "unlink" => link::menu_unlink(app),
        "quit" => app.exit(0),
        _ => {}
    }
}

/// 在系统浏览器里打开。本机服务要密码，这里用一次性的票让浏览器也登录上
fn open_in_browser(app: &AppHandle) {
    let Some(origin) = state(app).origin.lock().unwrap().clone() else { return };
    let mut url = origin.clone();
    if is_local(app) {
        if let Ok(t) = server::ticket(origin.port().unwrap_or(store::DEFAULT_PORT), &store::secret()) {
            url = origin.join("/login/ticket").unwrap();
            url.query_pairs_mut().append_pair("t", &t).append_pair("next", "/");
        }
    }
    let _ = app.opener().open_url(url.as_str(), None::<&str>);
}

// ---------- 窗口 ----------

/// 跳转：WebKit 分不清是页面在跳还是页面里的 iframe 在加载，所以这里都放行，
/// 只拦本机服务的登录页（自动再登一次）。真跳到别的网站时，由 on_page_load 送去系统浏览器
fn allow_navigation(app: &AppHandle, u: &Url) -> bool {
    if u.path() == "/login" && is_local(app) {
        let origin = state(app).origin.lock().unwrap().clone();
        if let Some(o) = origin.filter(|o| o.origin() == u.origin()) {
            let next = u.query_pairs().find(|(k, _)| k == "next").map(|(_, v)| v.into_owned()).unwrap_or_else(|| "/".into());
            let app = app.clone();
            std::thread::spawn(move || login_local(&app, &o, &next));
            return false;
        }
    }
    true
}

/// 主窗口开始加载别的网站时：用系统浏览器打开它，窗口回到服务器
fn on_main_load(app: &AppHandle, w: &WebviewWindow, event: PageLoadEvent, u: &Url) {
    if event != PageLoadEvent::Started || !matches!(u.scheme(), "http" | "https") || u.host_str() == Some("tauri.localhost") {
        return;
    }
    let origin = state(app).origin.lock().unwrap().clone();
    if let Some(o) = origin {
        if o.origin() != u.origin() {
            let _ = app.opener().open_url(u.as_str(), None::<&str>);
            let _ = w.navigate(o);
        }
    }
}

/// 新窗口：同一个服务器的（应用登录授权、文件预览、分享页）在 App 里开小窗，和主窗口共用登录状态；别的网站用系统浏览器
fn new_window(app: &AppHandle, u: Url, features: tauri::webview::NewWindowFeatures) -> NewWindowResponse<tauri::Wry> {
    let origin = state(app).origin.lock().unwrap().clone();
    let same = u.scheme() == "about" || origin.as_ref().is_some_and(|o| o.origin() == u.origin());
    if !same {
        let _ = app.opener().open_url(u.as_str(), None::<&str>);
        return NewWindowResponse::Deny;
    }
    let n = state(app).popups.fetch_add(1, SeqCst);
    let built = WebviewWindowBuilder::new(app, format!("popup-{n}"), WebviewUrl::External("about:blank".parse().unwrap()))
        .window_features(features)
        .title("Open Super Agent")
        .on_document_title_changed(|w, title| {
            let _ = w.set_title(&title);
        })
        .on_page_load(|w, p| {
            // 应用授权完成页会在 1 秒多后自己关；App 里的小窗关不掉自己，由这里关
            if p.event() == PageLoadEvent::Finished && p.url().path() == "/oauth/callback" {
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(2500));
                    let _ = w.close();
                });
            }
        })
        .build();
    match built {
        Ok(window) => NewWindowResponse::Create { window },
        Err(_) => NewWindowResponse::Deny,
    }
}

fn build_main(app: &AppHandle, visible: bool) -> tauri::Result<WebviewWindow> {
    let (nav, load, popup, dl) = (app.clone(), app.clone(), app.clone(), app.clone());
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Open Super Agent")
        .inner_size(1280.0, 840.0)
        .min_inner_size(420.0, 560.0)
        .visible(visible)
        .initialization_script(BRIDGE)
        .disable_drag_drop_handler() // 文件拖进窗口交给网页处理（网页自己会上传）
        .on_navigation(move |u| allow_navigation(&nav, u))
        .on_page_load(move |w, p| on_main_load(&load, &w, p.event(), p.url()))
        .on_new_window(move |u, f| new_window(&popup, u, f))
        .on_download(move |_, event| {
            if let DownloadEvent::Finished { url, success, .. } = event {
                let name = url.path_segments().and_then(|s| s.last().map(String::from)).unwrap_or_default();
                let _ = dl.notification().builder()
                    .title(if success { tr("下载好了", "Download finished") } else { tr("下载失败", "Download failed") })
                    .body(if success { tr("在“下载”文件夹里", "It's in your Downloads folder") } else { name.as_str() })
                    .show();
            }
            true
        })
        .build()
}

fn toggle_main(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        if w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false) {
            let _ = w.hide();
        } else {
            show_main(app);
        }
    }
}

/// 只在调试版里：自动测试的通道（终端里没法点窗口）。数据目录下的 autotest.js 出现时，在主窗口的页面里执行它再删掉
#[cfg(debug_assertions)]
fn start_autotest(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(500));
        let f = store::home().join("autotest.js");
        if let Ok(js) = std::fs::read_to_string(&f) {
            let _ = std::fs::remove_file(&f);
            if let Some(w) = main_window(&app) {
                let _ = w.eval(js);
            }
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let hidden = std::env::args().any(|a| a == "--hidden"); // 开机自动启动时不弹窗口，在菜单栏里待着
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| show_main(app)))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, Some(vec!["--hidden"])))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _, event| {
                    if event.state == ShortcutState::Pressed {
                        toggle_main(app);
                    }
                })
                .build(),
        )
        .manage(State {
            cfg: Mutex::new(store::load()),
            origin: Mutex::new(None),
            phase: Mutex::new((String::new(), String::new())),
            server: Arc::new(server::Server::default()),
            agent: Arc::new(server::Server::default()),
            granted: Mutex::new(vec![]),
            popups: AtomicU32::new(0),
        })
        .invoke_handler(tauri::generate_handler![
            setup_state, choose_local, choose_remote, open_logs,
            desktop_notify, desktop_set_badge, desktop_open_external, desktop_log,
            link::desktop_link_state, link::desktop_link, link::desktop_unlink
        ])
        .on_window_event(|w, e| {
            // 关掉主窗口只是藏起来：服务和定时任务接着跑，菜单栏里还能打开
            if let WindowEvent::CloseRequested { api, .. } = e {
                if w.label() == "main" {
                    api.prevent_close();
                    let _ = w.hide();
                }
            }
        })
        .setup(move |app| {
            let handle = app.handle().clone();
            build_main(&handle, !hidden)?;
            #[cfg(debug_assertions)]
            start_autotest(&handle);
            tray::create(&handle)?;
            start_update_loop(&handle);
            // ⌘⇧空格（Windows 上 Ctrl+Shift+空格）呼出或藏起窗口；被别的程序占了就算了
            let _ = handle.global_shortcut().register(Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::Space));
            // 点了系统通知：打开窗口和对应的对话
            let clicked = handle.clone();
            let _ = handle.notification().on_action(move |a| {
                let Some(id) = a.notification().map(|n| n.id()) else { return };
                show_main(&clicked);
                if let Some(w) = main_window(&clicked) {
                    let _ = w.eval(format!("window.dispatchEvent(new CustomEvent('osa-open-chat', {{ detail: {id} }}))"));
                }
            });
            let cfg = state(&handle).cfg.lock().unwrap().clone();
            match (cfg.mode.as_deref(), cfg.url) {
                (Some("local"), _) => start_local(&handle),
                (Some("remote"), Some(url)) => {
                    if let Some(origin) = normalize(&url) {
                        open_server(&handle, origin, None);
                        link::start(&handle);
                    }
                }
                _ => {}
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("启动 Open Super Agent 失败");

    app.run(|app, event| match event {
        // 菜单栏里点“退出”走 ExitRequested；⌘Q 和系统的“退出”直接走 Exit。两种都先停服务（重复调用没关系）
        RunEvent::ExitRequested { .. } | RunEvent::Exit => {
            state(app).server.stop();
            link::stop(app);
        }
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main(app),
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::utils::acl::RemoteUrlPattern;

    #[test]
    fn only_the_web_ui_gets_the_bridge() {
        let origin = Url::parse("https://h.example:8443/").unwrap();
        let p: RemoteUrlPattern = server_pattern(&origin).parse().unwrap();
        let ok = |u: &str| p.test(&Url::parse(u).unwrap());
        for u in ["/", "/?chat=3&thread=4", "/apps", "/library", "/library/", "/library/pages/12"] {
            assert!(ok(&format!("https://h.example:8443{u}")), "{u} should be allowed");
        }
        for u in ["/api/pages/1/raw", "/api/shared/x/raw", "/s/abc", "/appsx", "//api", "/library/pages/1/raw", "/login"] {
            assert!(!ok(&format!("https://h.example:8443{u}")), "{u} should be denied");
        }
        assert!(!ok("https://other.example/"));
        assert!(!ok("http://h.example:8443/"));
    }
}
