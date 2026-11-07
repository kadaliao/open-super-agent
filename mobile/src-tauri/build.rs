fn main() {
    // 列出 App 自己的命令，给每个生成 allow-/deny- 授权：服务器的网页只能用授权给它的那几个
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
        "setup_state",
        "choose_remote",
        "confirm_pair",
        "cancel_pair",
        "mobile_set_badge",
        "mobile_open_external",
        "mobile_log",
        "mobile_push_current",
        "mobile_push_subscribe",
        "mobile_push_unsubscribe",
        "mobile_open_sheet",
        "mobile_switch_server",
        "mobile_take_pending_open",
    ])))
    .expect("tauri build");
}
