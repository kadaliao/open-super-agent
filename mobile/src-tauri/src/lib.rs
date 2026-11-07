//! Open Super Agent 的 iPhone App：只连服务器，手机上不跑服务。
//! 界面就是服务器自己的网页（和桌面版的“连接到我的服务器”一样，不打包进 App），外壳只加：
//! 扫码配对、推送（经推送中转，见 docs/mobile.md）、弹出页、下载后弹分享面板、图标上的待确认数。
mod store;

use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::ipc::CapabilityBuilder;
use tauri::webview::{DownloadEvent, PageLoadEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_osa_ios::OsaIosExt;
use url::Url;

/// 页面加载前放进去的 window.osaDesktop（网页里的 web/src/desktop.ts 会用它）。
/// 只有当前服务器的网页界面拿到了调用这几个命令的授权，别的网页即使有这个对象也调不动。
/// 来源是 null 的（被 CSP sandbox 隔离的页面，比如 Bot 生成的报告页）不放
const BRIDGE: &str = r#"(function () {
  if (window.osaDesktop || window.top !== window || !/^https?:$/.test(location.protocol) || location.origin === 'null') return;
  var call = function (cmd, args) { return window.__TAURI_INTERNALS__.invoke(cmd, args || {}); };
  var quiet = function (cmd, args) {
    try {
      call(cmd, args).catch(function (e) { if (cmd !== 'mobile_log') quiet('mobile_log', { msg: cmd + ' failed: ' + String(e) }); });
    } catch (e) {}
  };
  window.osaDesktop = {
    platform: 'ios',
    notify: function () {},
    setBadge: function (count) { quiet('mobile_set_badge', { count: count | 0 }); },
    openExternal: function (url) { quiet('mobile_open_external', { url: String(url) }); },
    speech: false,
    push: {
      current: function (key) { return call('mobile_push_current', { key: String(key) }); },
      subscribe: function (key) { return call('mobile_push_subscribe', { key: String(key) }); },
      unsubscribe: function (key) { return call('mobile_push_unsubscribe', { key: String(key) }); }
    },
    openSheet: function (url) { return call('mobile_open_sheet', { url: String(url) }); },
    switchServer: function () { quiet('mobile_switch_server'); },
    takePendingOpen: function () { return call('mobile_take_pending_open'); }
  };
})();"#;

/// 网页界面自己会落到的路径（`/`、`/apps`、`/library`、`/library/pages/12`，见 web/src/App.tsx），和桌面版一样。
/// 服务器上别的页面（Bot 生成的报告页 /api/pages/1/raw、分享页 /s/…）不给授权
const UI_PATHS: &str = r"/:p(|apps/?|library/?|library/pages/\d+/?)";

/// 扫到的二维码，等用户确认（已经连着别的服务器时）
#[derive(Clone)]
struct Pair {
    server: Url,
    ticket: String,
}

pub struct State {
    dir: PathBuf,
    cfg: Mutex<store::Config>,
    origin: Mutex<Option<Url>>,          // 当前打开的服务器
    granted: Mutex<Vec<String>>,         // 已经给过授权的服务器
    pair: Mutex<Option<Pair>>,
    error: Mutex<String>,                // 连接页上显示的错误
    downloads: Mutex<HashMap<String, PathBuf>>,
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

fn trace(what: std::fmt::Arguments) {
    if cfg!(debug_assertions) {
        eprintln!("[mobile] {what}");
    }
}

fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window("main")
}

fn setup_url(query: &str) -> Url {
    let base = if cfg!(any(windows, target_os = "android")) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
    Url::parse(&format!("{base}{query}")).unwrap()
}

fn is_setup(u: &Url) -> bool {
    u.scheme() == "tauri" || u.host_str() == Some("tauri.localhost")
}

fn show_setup(app: &AppHandle, query: &str) {
    *state(app).origin.lock().unwrap() = None;
    if let Some(w) = main_window(app) {
        let _ = w.navigate(setup_url(query));
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

fn server_pattern(origin: &Url) -> String {
    format!("{}{UI_PATHS}", origin.origin().ascii_serialization())
}

/// 主界面里可以停留的路径：网页界面本身、登录。别的（报告页、文件、分享页）改在弹出页里打开
fn main_path_ok(path: &str) -> bool {
    let p = path.trim_end_matches('/');
    matches!(p, "" | "/apps" | "/library" | "/login" | "/login/ticket" | "/logout")
        || p.strip_prefix("/library/pages/").is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
        || p.starts_with("/oauth/")   // 旧版服务器的网页不会用弹出页授权，在主界面里走完
}

/// 扫码打开的地址：opensuperagent://pair?u=<服务器>&t=<一次性登录票>
fn parse_pair(u: &Url) -> Option<Pair> {
    if u.scheme() != "opensuperagent" || u.host_str() != Some("pair") {
        return None;
    }
    let get = |k: &str| u.query_pairs().find(|(n, _)| n == k).map(|(_, v)| v.into_owned());
    let server = normalize(&get("u")?)?;
    let ticket = get("t").unwrap_or_default();
    if !ticket.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return None;
    }
    Some(Pair { server, ticket })
}

fn check_remote(origin: &Url) -> Result<(), String> {
    let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(15)).build();
    match agent.get(origin.join("/manifest.json").unwrap().as_str()).call() {
        Ok(_) => Ok(()),
        Err(e) => Err(format!("{}{e}", tr("连不上这个地址：", "Can't reach this address: "))),
    }
}

// ---------- 打开服务器 ----------

/// 记下服务器的地址（只在它里面跳转），给它的网页界面授权，主界面跳过去。first：先打开的地址（比如兑换登录票）
fn open_server(app: &AppHandle, origin: Url, first: Option<Url>) {
    let st = state(app);
    *st.origin.lock().unwrap() = Some(origin.clone());
    st.error.lock().unwrap().clear();
    grant(app, &origin);
    if let Some(w) = main_window(app) {
        let _ = w.navigate(first.unwrap_or(origin));
    }
}

fn grant(app: &AppHandle, origin: &Url) {
    let key = origin.origin().ascii_serialization();
    let st = state(app);
    let mut granted = st.granted.lock().unwrap();
    if granted.contains(&key) {
        return;
    }
    let mut cap = CapabilityBuilder::new(format!("server-{}", granted.len())).remote(server_pattern(origin)).local(false).window("main");
    for cmd in ["set-badge", "open-external", "log", "push-current", "push-subscribe", "push-unsubscribe", "open-sheet", "switch-server", "take-pending-open"] {
        cap = cap.permission(format!("allow-mobile-{cmd}"));
    }
    if app.add_capability(cap).is_ok() {
        granted.push(key);
    }
}

/// 连上一台服务器并记住它；有登录票就先用票登录
fn connect(app: &AppHandle, origin: Url, ticket: &str) -> Result<(), String> {
    check_remote(&origin)?;
    let st = state(app);
    {
        let mut cfg = st.cfg.lock().unwrap();
        cfg.url = Some(origin.origin().ascii_serialization());
        store::save(&st.dir, &cfg);
    }
    let first = (!ticket.is_empty()).then(|| {
        let mut u = origin.join("/login/ticket").unwrap();
        u.query_pairs_mut().append_pair("t", ticket).append_pair("next", "/");
        u
    });
    open_server(app, origin, first);
    Ok(())
}

/// 扫了二维码（系统相机打开了 opensuperagent://pair?...）
fn on_pair(app: &AppHandle, pair: Pair) {
    let configured = state(app).cfg.lock().unwrap().url.clone().and_then(|u| normalize(&u));
    // 已经连着另一台服务器：先问一下，免得一个恶意的二维码把 App 悄悄连到别人的服务器上
    if configured.as_ref().is_some_and(|c| c.origin() != pair.server.origin()) {
        *state(app).pair.lock().unwrap() = Some(pair);
        show_setup(app, "?pair=1");
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        if let Err(e) = connect(&app, pair.server, &pair.ticket) {
            *state(&app).error.lock().unwrap() = e;
            show_setup(&app, "");
        }
    });
}

fn on_open_urls(app: &AppHandle, urls: Vec<Url>) {
    for u in urls {
        trace(format_args!("opened {}", u.as_str().split('?').next().unwrap_or("")));
        if let Some(p) = parse_pair(&u) {
            on_pair(app, p);
        } else if cfg!(debug_assertions) && u.scheme() == "opensuperagent" && u.host_str() == Some("debug") {
            debug_hook(app, &u);
        }
    }
}

/// 只在调试版里：自动测试的通道（模拟器上没法点屏幕，`simctl openurl` 打开自定义链接还会弹确认框）
/// - 启动参数 `--osa-open <链接>`：当成打开了这个链接（比如扫码配对）
/// - App 的 Documents/autotest.js 出现时，在页面里执行它再删掉；结果用 mobile_log 打到控制台
#[cfg(debug_assertions)]
fn start_autotest(app: &AppHandle) {
    let args: Vec<String> = std::env::args().collect();
    if let Some(u) = args.iter().position(|a| a == "--osa-open").and_then(|i| args.get(i + 1)).and_then(|s| Url::parse(s).ok()) {
        let app = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(1));
            on_open_urls(&app, vec![u]);
        });
    }
    let Ok(dir) = app.path().document_dir() else { return };
    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(500));
        let f = dir.join("autotest.js");
        if let Ok(js) = std::fs::read_to_string(&f) {
            let _ = std::fs::remove_file(&f);
            if let Some(w) = main_window(&app) {
                let _ = w.eval(js);
            }
        }
    });
}

/// 只在调试版里：opensuperagent://debug/eval?js= 往页面里执行 JS、/open?path= 模拟点通知
fn debug_hook(app: &AppHandle, u: &Url) {
    let get = |k: &str| u.query_pairs().find(|(n, _)| n == k).map(|(_, v)| v.into_owned());
    let Some(w) = main_window(app) else { return };
    match u.path() {
        "/eval" => {
            if let Some(js) = get("js") {
                let _ = w.eval(js);
            }
        }
        "/open" => {
            let path = get("path").unwrap_or_else(|| "/".into());
            let _ = w.eval(format!("window.dispatchEvent(new CustomEvent('osa-open-url', {{ detail: {} }}))", json!(path)));
        }
        _ => {}
    }
}

// ---------- 连接页调用的命令 ----------

#[tauri::command]
fn setup_state(app: AppHandle) -> Value {
    let st = state(&app);
    let url = st.cfg.lock().unwrap().url.clone();
    let pair = st.pair.lock().unwrap().clone();
    json!({
        "url": url,
        "zh": zh(),
        "error": st.error.lock().unwrap().clone(),
        "pair": pair.map(|p| p.server.origin().ascii_serialization()),
    })
}

#[tauri::command(async)]
fn choose_remote(app: AppHandle, url: String) -> Result<(), String> {
    let origin = normalize(&url).ok_or_else(|| tr("地址不对，比如 https://osa.example.com", "That address doesn't look right, e.g. https://osa.example.com").to_string())?;
    connect(&app, origin, "")
}

#[tauri::command(async)]
fn confirm_pair(app: AppHandle) -> Result<(), String> {
    let pair = state(&app).pair.lock().unwrap().take().ok_or("no pending pair")?;
    connect(&app, pair.server, &pair.ticket)
}

#[tauri::command]
fn cancel_pair(app: AppHandle) {
    state(&app).pair.lock().unwrap().take();
    let configured = state(&app).cfg.lock().unwrap().url.clone().and_then(|u| normalize(&u));
    if let Some(origin) = configured {
        open_server(&app, origin, None);
    }
}

// ---------- 网页通过 window.osaDesktop 调用的命令 ----------

fn current_origin(app: &AppHandle) -> Result<String, String> {
    state(app).origin.lock().unwrap().as_ref().map(|o| o.origin().ascii_serialization()).ok_or_else(|| "no server".into())
}

#[tauri::command(async)]
async fn mobile_set_badge(app: AppHandle, count: i64) -> Result<(), String> {
    app.osa_ios().call::<Value>("setBadge", json!({ "count": count.max(0) })).await.map(|_| ())
}

#[tauri::command]
fn mobile_open_external(app: AppHandle, url: String) {
    if let Ok(u) = Url::parse(&url) {
        if matches!(u.scheme(), "http" | "https" | "mailto" | "tel") {
            let _ = app.opener().open_url(u.as_str(), None::<&str>);
        }
    }
}

#[tauri::command]
fn mobile_log(msg: String) {
    trace(format_args!("{msg}"));
}

#[tauri::command(async)]
async fn mobile_push_current(app: AppHandle, key: String) -> Result<Option<Value>, String> {
    let origin = current_origin(&app)?;
    let r: Value = app.osa_ios().call("pushCurrent", json!({ "origin": origin, "vapid": key })).await?;
    Ok(r.get("subscription").filter(|s| !s.is_null()).cloned())
}

#[tauri::command(async)]
async fn mobile_push_subscribe(app: AppHandle, key: String) -> Result<Value, String> {
    let origin = current_origin(&app)?;
    let r: Value = app.osa_ios().call("pushSubscribe", json!({ "origin": origin, "vapid": key })).await?;
    r.get("subscription").filter(|s| !s.is_null()).cloned().ok_or_else(|| "no subscription".into())
}

#[tauri::command(async)]
async fn mobile_push_unsubscribe(app: AppHandle, key: String) -> Result<(), String> {
    let _ = key;
    let origin = current_origin(&app)?;
    app.osa_ios().call::<Value>("pushUnsubscribe", json!({ "origin": origin })).await.map(|_| ())
}

/// 在弹出页里打开同一台服务器上的地址，等它关掉。别的网站用系统浏览器
#[tauri::command(async)]
async fn mobile_open_sheet(app: AppHandle, url: String) -> Result<(), String> {
    let origin = state(&app).origin.lock().unwrap().clone().ok_or("no server")?;
    let u = origin.join(&url).map_err(|e| e.to_string())?;
    if u.origin() != origin.origin() {
        let _ = app.opener().open_url(u.as_str(), None::<&str>);
        return Ok(());
    }
    app.osa_ios().call::<Value>("openSheet", json!({ "url": u.as_str() })).await.map(|_| ())
}

#[tauri::command]
fn mobile_switch_server(app: AppHandle) {
    show_setup(&app, "?switch=1");
}

#[tauri::command(async)]
async fn mobile_take_pending_open(app: AppHandle) -> Result<Option<String>, String> {
    let origin = current_origin(&app)?;
    let r: Value = app.osa_ios().call("takePendingOpen", json!({ "origin": origin })).await?;
    Ok(r.get("url").and_then(|u| u.as_str()).map(String::from))
}

// ---------- 主界面 ----------

/// 主界面开始加载一个地址时：别的网站交给系统浏览器；同一台服务器上不该在主界面里的页面（报告页、文件）改在弹出页打开。
/// 主界面都回到服务器。只看顶层页面（这个回调不管 iframe）
fn on_main_load(app: &AppHandle, w: &WebviewWindow, event: PageLoadEvent, u: &Url) {
    if event != PageLoadEvent::Started || !matches!(u.scheme(), "http" | "https") || is_setup(u) {
        return;
    }
    let Some(origin) = state(app).origin.lock().unwrap().clone() else { return };
    if origin.origin() != u.origin() {
        let _ = app.opener().open_url(u.as_str(), None::<&str>);
        let _ = w.navigate(origin);
    } else if !main_path_ok(u.path()) {
        let _ = w.navigate(origin);
        let (app, target) = (app.clone(), u.to_string());
        tauri::async_runtime::spawn(async move {
            let _ = app.osa_ios().call::<Value>("openSheet", json!({ "url": target })).await;
        });
    }
}

/// 下载：存到 App 的临时目录，下完弹分享面板（存到“文件”、发给别的 App）
fn on_download(app: &AppHandle, event: DownloadEvent<'_>) -> bool {
    match event {
        DownloadEvent::Requested { url, destination } => {
            let name = destination
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .filter(|n| !n.is_empty())
                .or_else(|| url.path_segments().and_then(|mut s| s.next_back().map(String::from)))
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| "download".into());
            let dir = app.path().temp_dir().unwrap_or_else(|_| std::env::temp_dir()).join("downloads");
            let _ = std::fs::create_dir_all(&dir);
            let path = dir.join(name.replace(['/', '\\'], "_"));
            trace(format_args!("download -> {}", path.display()));
            *destination = path.clone();
            state(app).downloads.lock().unwrap().insert(url.to_string(), path);
            true
        }
        DownloadEvent::Finished { url, path, success } => {
            let saved = path.or_else(|| state(app).downloads.lock().unwrap().remove(url.as_str()));
            trace(format_args!("download finished ok={success} {:?}", saved));
            if let (true, Some(p)) = (success, saved) {
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let _ = app.osa_ios().call::<Value>("share", json!({ "path": p })).await;
                });
            }
            true
        }
        _ => true,
    }
}

fn build_main(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    let (load, dl) = (app.clone(), app.clone());
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .initialization_script(BRIDGE)
        .on_page_load(move |w, p| on_main_load(&load, &w, p.event(), p.url()))
        .on_download(move |_, event| on_download(&dl, event))
        .build()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_osa_ios::init())
        .invoke_handler(tauri::generate_handler![
            setup_state, choose_remote, confirm_pair, cancel_pair,
            mobile_set_badge, mobile_open_external, mobile_log, mobile_push_current, mobile_push_subscribe,
            mobile_push_unsubscribe, mobile_open_sheet, mobile_switch_server, mobile_take_pending_open
        ])
        .setup(|app| {
            let dir = app.path().app_data_dir().unwrap_or_else(|_| std::env::temp_dir());
            let cfg = store::load(&dir);
            app.manage(State {
                dir,
                cfg: Mutex::new(cfg.clone()),
                origin: Mutex::new(None),
                granted: Mutex::new(vec![]),
                pair: Mutex::new(None),
                error: Mutex::new(String::new()),
                downloads: Mutex::new(HashMap::new()),
            });
            let handle = app.handle().clone();
            build_main(&handle)?;
            #[cfg(debug_assertions)]
            start_autotest(&handle);
            // 上次连过的服务器：直接打开（登录状态在 WKWebView 的 cookie 里）
            if let Some(origin) = cfg.url.as_deref().and_then(normalize) {
                open_server(&handle, origin, None);
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("启动 Open Super Agent 失败");

    app.run(|app, event| {
        #[cfg(any(target_os = "macos", target_os = "ios"))]
        if let RunEvent::Opened { urls } = event {
            on_open_urls(app, urls);
        }
        #[cfg(not(any(target_os = "macos", target_os = "ios")))]
        let _ = (app, event);
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
        for u in ["/", "/?chat=3&thread=4", "/apps", "/library", "/library/pages/12"] {
            assert!(ok(&format!("https://h.example:8443{u}")), "{u} should be allowed");
        }
        for u in ["/api/pages/1/raw", "/api/shared/x/raw", "/s/abc", "/login"] {
            assert!(!ok(&format!("https://h.example:8443{u}")), "{u} should be denied");
        }
        assert!(!ok("https://other.example/"));
    }

    #[test]
    fn main_view_keeps_only_the_ui() {
        for p in ["/", "/apps", "/library/", "/library/pages/3", "/login", "/login/ticket", "/oauth/start/notion"] {
            assert!(main_path_ok(p), "{p}");
        }
        for p in ["/api/pages/1/raw", "/s/abc", "/api/files/download", "/library/pages/x", "/library/pages/"] {
            assert!(!main_path_ok(p), "{p}");
        }
    }

    #[test]
    fn pairing_links() {
        let p = parse_pair(&Url::parse("opensuperagent://pair?u=https%3A%2F%2Fosa.example.com%2Fx&t=abc_-9").unwrap()).unwrap();
        assert_eq!(p.server.as_str(), "https://osa.example.com/");
        assert_eq!(p.ticket, "abc_-9");
        let lan = parse_pair(&Url::parse("opensuperagent://pair?u=http%3A%2F%2F192.168.1.5%3A8765").unwrap()).unwrap();
        assert_eq!((lan.server.as_str(), lan.ticket.as_str()), ("http://192.168.1.5:8765/", ""));
        assert!(parse_pair(&Url::parse("opensuperagent://pair?t=x").unwrap()).is_none());
        assert!(parse_pair(&Url::parse("opensuperagent://pair?u=javascript%3Aalert(1)").unwrap()).is_none());
        assert!(parse_pair(&Url::parse("opensuperagent://pair?u=https%3A%2F%2Fa.example&t=a%26next%3D%2F%2Fevil").unwrap()).is_none());
        assert!(parse_pair(&Url::parse("https://pair/?u=x").unwrap()).is_none());
    }

    #[test]
    fn addresses() {
        assert_eq!(normalize("osa.example.com/abc").unwrap().as_str(), "https://osa.example.com/");
        assert_eq!(normalize(" http://10.0.0.2:8765 ").unwrap().as_str(), "http://10.0.0.2:8765/");
        assert!(normalize("ftp://x").is_none());
    }
}
