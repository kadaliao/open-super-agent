//! 在这台电脑上运行时，App 自己起停服务：跑 App 里带的 osa-server（开发版用仓库里编译好的），崩了自动重启。
//! 服务器借用这台 Mac 时，同样的办法起停 Mac 上的小程序（osa-server agent）。
//! 服务和它启动的应用（MCP 子进程）放在一个进程组里，退出 App 时一起停掉；
//! App 异常退出来不及停时，服务看 OSA_PARENT_PID 自己退出。

use std::fs::OpenOptions;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering::SeqCst};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::store;

pub struct Launch {
    program: PathBuf,
    args: Vec<String>,
    cwd: PathBuf,
    env: Vec<(String, String)>,
    log: PathBuf,
}

#[derive(Default)]
pub struct Server {
    pid: AtomicI32, // 正在跑的服务进程，0 表示没有
    stopping: AtomicBool,
    running: AtomicBool, // 守护线程还在
}

/// 停服务时最多等多久：比服务自己等任务的时限（30 秒）多一点
const STOP_WAIT: Duration = Duration::from_secs(35);

impl Server {
    /// 起服务并一直看着：退出了就按 2、4、8、16 秒退避重启，2 分钟内退出 5 次就放弃，调用 on_fail。
    pub fn start(self: &Arc<Self>, launch: Launch, on_fail: impl Fn(String) + Send + 'static) {
        if self.running.swap(true, SeqCst) {
            return;
        }
        self.stopping.store(false, SeqCst);
        let me = Arc::clone(self);
        thread::spawn(move || {
            let mut exits: Vec<Instant> = vec![];
            loop {
                let mut child = match spawn(&launch) {
                    Ok(c) => c,
                    Err(e) => {
                        on_fail(format!("{}: {e}", launch.program.display()));
                        break;
                    }
                };
                me.pid.store(child.id() as i32, SeqCst);
                let status = child.wait();
                me.pid.store(0, SeqCst);
                if me.stopping.load(SeqCst) {
                    break;
                }
                exits.retain(|t| t.elapsed() < Duration::from_secs(120));
                exits.push(Instant::now());
                if exits.len() >= 5 {
                    on_fail(format!("{status:?}"));
                    break;
                }
                thread::sleep(Duration::from_secs(1 << exits.len().min(4)));
                if me.stopping.load(SeqCst) {
                    break;
                }
            }
            me.running.store(false, SeqCst);
        });
    }

    pub fn alive(&self) -> bool {
        self.running.load(SeqCst)
    }

    /// 先只给服务自己发 SIGTERM：它会等任务把正在做的那一步做完、存好（最多 limits.shutdown_wait_s，默认 30 秒），
    /// 再自己停掉聊天软件和应用。所以这里等 35 秒；不给整个进程组发，免得把那一步正用着的应用、正在跑的代码先杀了。
    /// 服务退出以后（或者到时间还没退），再收拾进程组里剩下的。没有任务在做时服务马上就退，不会让你等。
    pub fn stop(&self) {
        self.stopping.store(true, SeqCst);
        let pid = self.pid.load(SeqCst);
        if pid > 0 {
            #[cfg(unix)]
            unsafe {
                libc::kill(pid, libc::SIGTERM);
                let t = Instant::now();
                while t.elapsed() < STOP_WAIT && libc::kill(pid, 0) == 0 {
                    thread::sleep(Duration::from_millis(100));
                }
                // 服务已经正常退出时进程组里一般没人了（kill 返回 ESRCH）；还有残留的子进程就一起结束
                libc::kill(-pid, if libc::kill(pid, 0) == 0 { libc::SIGKILL } else { libc::SIGTERM });
            }
        }
        // 等守护线程收尾，免得马上再 start 时被当成还在跑
        let t = Instant::now();
        while self.running.load(SeqCst) && t.elapsed() < Duration::from_secs(3) {
            thread::sleep(Duration::from_millis(50));
        }
    }
}

fn spawn(l: &Launch) -> std::io::Result<Child> {
    let log = OpenOptions::new().create(true).append(true).open(&l.log)?;
    let mut cmd = Command::new(&l.program);
    cmd.args(&l.args)
        .current_dir(&l.cwd)
        .envs(l.env.iter().map(|(k, v)| (k, v)))
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
    // 开发时从终端启动会带上 shell 里的 INSTABOX_API_KEY，电脑就会用线上的 Instabox；本机开发一律用本地电脑
    if cfg!(debug_assertions) {
        cmd.env_remove("INSTABOX_API_KEY");
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    cmd.spawn()
}

/// 怎么起服务：跑 App 里带的 osa-server（Rust 版服务，一个文件；开发版用仓库里编译好的）。
pub fn launch(resources: Option<PathBuf>, port: u16, secret: &str) -> Result<Launch, String> {
    let port = port.to_string();
    rust_launch(resources, &["--host", "127.0.0.1", "--port", &port], "server.log", vec![
        ("OSA_PASSWORD".into(), secret.into()),
        ("OSA_COOKIE".into(), "osa_desktop".into()), // 和本机别的服务（比如开发用的 8765）分开，免得互相顶掉登录
        ("OSA_DESKTOP".into(), "1".into()), // 电脑默认用这台电脑上的 Chrome
    ])
}

/// 服务器借用这台 Mac 时在后台跑的小程序（osa-server agent）：连到 ws_url，替服务器上的 Bot 操作这台 Mac。
/// 数据目录和“在这台电脑上运行”共用：Bot 的 Chrome 里登录过的网站、工作文件夹里的文件两边都能用
pub fn agent_launch(resources: Option<PathBuf>, ws_url: &str, token: &str) -> Result<Launch, String> {
    rust_launch(resources, &["agent"], "agent.log", vec![
        ("OSA_LINK_URL".into(), ws_url.into()),
        ("OSA_LINK_TOKEN".into(), token.into()),
    ])
}

/// 第一次打开时装 uv 和 Python：跑 `osa-server setup`，标准输出是一行一行的 JSON 进度（见 onboard.rs），出错信息进 setup.log
pub fn setup_child(resources: Option<PathBuf>) -> Result<Child, String> {
    let l = rust_launch(resources, &["setup"], "setup.log", vec![])?;
    let log = OpenOptions::new().create(true).append(true).open(&l.log).map_err(|e| e.to_string())?;
    let mut cmd = Command::new(&l.program);
    cmd.args(&l.args).current_dir(&l.cwd).envs(l.env.iter().map(|(k, v)| (k, v))).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(log);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    cmd.spawn().map_err(|e| e.to_string())
}

/// osa-server 在哪、它的根目录（config.yaml、osa/static 在下面）在哪。
/// 打包的 App：osa-server 和主程序一起在 Contents/MacOS（Tauri 的 externalBin），根目录是 Resources/app；
/// 开发版：仓库里 server/target 下编译好的（先 cd server && cargo build），根目录是仓库
fn server_exe(resources: Option<PathBuf>) -> Option<(PathBuf, PathBuf)> {
    let bundled = std::env::current_exe().ok().and_then(|e| e.parent().map(|d| d.join("osa-server")));
    if let (Some(exe), Some(res)) = (bundled.filter(|p| p.is_file()), resources.clone()) {
        if res.join("app/config.yaml").is_file() {
            return Some((exe, res.join("app")));
        }
    }
    if cfg!(debug_assertions) {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        for p in ["server/target/release/osa-server", "server/target/debug/osa-server"] {
            if repo.join(p).is_file() {
                return Some((repo.join(p), repo));
            }
        }
    }
    None
}

fn rust_launch(resources: Option<PathBuf>, args: &[&str], log_name: &str, extra: Vec<(String, String)>) -> Result<Launch, String> {
    let (program, root) = server_exe(resources).ok_or("App 里没有找到 osa-server")?;
    let data = store::data_dir();
    std::fs::create_dir_all(&data).map_err(|e| e.to_string())?;
    let log = store::log_dir().join(log_name);
    if std::fs::metadata(&log).map(|m| m.len() > 5 << 20).unwrap_or(false) {
        let _ = std::fs::rename(&log, log.with_extension("log.1"));
    }
    // uv（uvx 应用、没有 Python 时装一份）第一次用到时才下载到这里，App 不再带 Python
    let runtime = store::home().join("runtime");
    let mut env: Vec<(String, String)> = vec![
        ("OSA_DATA_DIR".into(), data.display().to_string()),
        ("OSA_PARENT_PID".into(), std::process::id().to_string()),
        ("OSA_ROOT".into(), root.display().to_string()),
        ("OSA_RUNTIME_DIR".into(), runtime.display().to_string()),
        ("PATH".into(), search_path(Some(&runtime.join("bin")))),
        // 只用 uv 自己下载的 Python：macOS 的 /usr/bin/python3 没装命令行工具时一跑就弹安装框
        ("UV_PYTHON_PREFERENCE".into(), "only-managed".into()),
        // uv 下载的 Python 放在 App 自己的目录里：不和你自己的 uv 混在一起，删掉 App 的数据就干净了
        ("UV_PYTHON_INSTALL_DIR".into(), runtime.join("python").display().to_string()),
        // 国内网络：uv、Python、npm 都从国内镜像下载（见 server/crates/osa/src/runtime.rs）
        ("OSA_MIRROR".into(), store::effective_mirror(&store::load())),
    ];
    env.extend(extra);
    let args: Vec<String> = args.iter().map(|s| s.to_string()).collect();
    Ok(Launch { program, args, cwd: root, env, log })
}

/// 从访达打开的 App 拿到的 PATH 很短，找不到 npx、uvx、docker；补上常见的位置
/// （Docker Desktop 没装命令行工具到 /usr/local/bin 时在 ~/.docker/bin，OrbStack 在 ~/.orbstack/bin）
fn search_path(bundled: Option<&Path>) -> String {
    let home = dirs::home_dir().unwrap_or_default();
    let mut dirs: Vec<String> = bundled.map(|p| p.display().to_string()).into_iter().collect();
    dirs.extend(["/opt/homebrew/bin".into(), "/usr/local/bin".into(), home.join(".local/bin").display().to_string()]);
    dirs.extend([".docker/bin", ".orbstack/bin"].map(|d| home.join(d).display().to_string()));
    dirs.extend(std::env::var("PATH").unwrap_or_default().split(':').filter(|s| !s.is_empty()).map(String::from));
    dirs.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"].map(String::from));
    let mut seen = std::collections::HashSet::new();
    dirs.retain(|d| seen.insert(d.clone()));
    dirs.join(":")
}

pub fn port_free(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

pub fn any_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0").and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(store::DEFAULT_PORT + 1)
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new().timeout(Duration::from_secs(5)).build()
}

/// 等服务起来：公开的 /manifest.json 能打开就算好了（第一次启动要十来秒）
pub fn wait_ready(port: u16, timeout: Duration, alive: impl Fn() -> bool) -> bool {
    let url = format!("http://127.0.0.1:{port}/manifest.json");
    let t = Instant::now();
    loop {
        if agent().get(&url).call().is_ok() {
            return true;
        }
        if t.elapsed() > timeout || !alive() {
            return false;
        }
        thread::sleep(Duration::from_millis(300));
    }
}

/// 用密码换一张一次性的登录票（POST /login/ticket），窗口带着票打开页面就登录了，密码不经过网页
pub fn ticket(port: u16, secret: &str) -> Result<String, String> {
    let resp = agent()
        .post(&format!("http://127.0.0.1:{port}/login/ticket"))
        .set("Authorization", &format!("Bearer {secret}"))
        .call()
        .map_err(|e| e.to_string())?;
    let body: serde_json::Value = resp.into_json().map_err(|e| e.to_string())?;
    body["ticket"].as_str().map(String::from).ok_or_else(|| "no ticket".into())
}

/// 连服务器前先看看地址对不对：能打开它的 /manifest.json
pub fn check_remote(origin: &str) -> Result<(), String> {
    agent().get(&format!("{origin}/manifest.json")).call().map(|_| ()).map_err(|e| e.to_string())
}

/// 日志最后几行，启动失败时给人看
pub fn log_tail() -> String {
    let text = std::fs::read_to_string(store::log_dir().join("server.log")).unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(12)..].join("\n")
}
