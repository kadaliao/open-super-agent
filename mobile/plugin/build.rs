// 插件没有给网页用的命令：Swift 部分只由 App 的 Rust 代码调用
const COMMANDS: &[&str] = &[];

fn main() {
    tauri_plugin::Builder::new(COMMANDS).ios_path("ios").build();
}
