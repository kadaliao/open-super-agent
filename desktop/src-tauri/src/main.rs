// 发布版在 Windows 上不弹命令行窗口
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    osa_desktop_lib::run()
}
