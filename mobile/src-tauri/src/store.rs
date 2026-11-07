//! App 自己的设置：连的是哪台服务器。放在 App 的数据目录（Library/Application Support/<bundle id>/mobile.json）。
//! 登录状态是服务器种的 cookie，在 WKWebView 自己的存储里；推送的钥匙在钥匙串里（Swift 那边管）。

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Config {
    /// 连的服务器，只留到域名和端口，比如 https://osa.example.com
    pub url: Option<String>,
}

fn file(dir: &PathBuf) -> PathBuf {
    let _ = std::fs::create_dir_all(dir);
    dir.join("mobile.json")
}

pub fn load(dir: &PathBuf) -> Config {
    std::fs::read(file(dir)).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

pub fn save(dir: &PathBuf, cfg: &Config) {
    if let Ok(b) = serde_json::to_vec_pretty(cfg) {
        let _ = std::fs::write(file(dir), b);
    }
}
