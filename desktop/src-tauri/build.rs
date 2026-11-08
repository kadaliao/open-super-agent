fn main() {
    // 列出 App 自己的命令，给每个生成 allow-/deny- 授权：服务器的网页只能用授权给它的那几个
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
        "setup_state",
        "choose_local",
        "choose_remote",
        "open_logs",
        "desktop_notify",
        "desktop_set_badge",
        "desktop_open_external",
        "desktop_log",
        "desktop_link_state",
        "desktop_link",
        "desktop_unlink",
    ])))
    .expect("tauri build");
}
