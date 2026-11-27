//! osa-server：和 `uvicorn osa.server:app --host … --port …` 一样的用法。
//! `osa-server setup`：装好 uv 和 Python（桌面 App 第一次打开时调，见 runtime.rs）。
//! `osa-server agent`：你的 Mac 上替服务器上的 Bot 干活的小程序（Python 版的 `python -m osa.computer.agent`，见 computer/agent.rs）。

use std::net::SocketAddr;

fn main() -> anyhow::Result<()> {
    osa::runtime::init_env(); // 改 PATH、镜像要在起线程之前
    tokio::runtime::Builder::new_multi_thread().enable_all().build()?.block_on(run())
}

async fn run() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_env("OSA_LOG").unwrap_or_else(|_| "info".into()))
        .with_target(false)
        .init();
    if std::env::args().nth(1).as_deref() == Some("agent") {
        return osa::computer::agent::main().await;
    }
    if std::env::args().nth(1).as_deref() == Some("backup") {
        return osa::backup::cli();
    }
    if std::env::args().nth(1).as_deref() == Some("setup") {
        return osa::runtime::setup_main().await;
    }
    if let Some(other) = std::env::args().nth(1).filter(|a| !a.starts_with('-')) {
        anyhow::bail!("不认识的子命令：{other}（可以用 agent、setup、backup，或者 --host / --port 起服务）");
    }
    let mut host = "127.0.0.1".to_string();
    let mut port: u16 = 8765;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--host" => host = args.next().unwrap_or(host),
            "--port" => port = args.next().and_then(|p| p.parse().ok()).unwrap_or(port),
            _ => {}
        }
    }
    osa::server::migrate();
    osa::server::startup().await;
    let addr: SocketAddr = format!("{host}:{port}").parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("osa-server 在 http://{addr} 上运行");
    let mut stop = osa::server::SHUTDOWN.1.clone();
    axum::serve(listener, osa::server::app().into_make_service_with_connect_info::<SocketAddr>())
        .with_graceful_shutdown(async move {
            #[cfg(unix)]
            {
                let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {},
                    _ = term.recv() => {},
                    _ = stop.changed() => {},
                }
            }
            #[cfg(not(unix))]
            {
                let _ = tokio::signal::ctrl_c().await;
            }
            osa::taskstore::begin_stop(); // 任务不再开始新的一步（shutdown() 里等正在执行的做完）
        })
        .await?;
    osa::server::shutdown().await;
    osa::backup::restart_if_requested(); // 网页上确认了恢复：用同样的命令行重新启动，启动时换上备份
    Ok(())
}
