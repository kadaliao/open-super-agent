//! App 自己的设置（用哪种方式、服务器地址、端口）和本机服务的登录密码。
//! 都放在 ~/Library/Application Support/open-super-agent：desktop.json、logs/、data/（服务的数据目录）。

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

pub const DEFAULT_PORT: u16 = 18765;
const KEYCHAIN_SERVICE: &str = "Open Super Agent (desktop)";
const KEYCHAIN_USER: &str = "local-server";

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Config {
    /// "local"：在这台电脑上运行；"remote"：连接到服务器；None：还没选
    pub mode: Option<String>,
    /// 连接的服务器，只留到域名和端口，比如 https://osa.example.com
    pub url: Option<String>,
    /// 本机服务的端口。定下来就别再变：应用的登录授权回调地址里有它
    pub port: Option<u16>,
}

pub fn home() -> PathBuf {
    // OSA_DESKTOP_HOME：测试时换一个目录，不碰真正的数据
    let dir = std::env::var_os("OSA_DESKTOP_HOME").map(PathBuf::from)
        .unwrap_or_else(|| dirs::data_dir().unwrap_or_else(std::env::temp_dir).join("open-super-agent"));
    let _ = std::fs::create_dir_all(&dir);
    dir
}

pub fn data_dir() -> PathBuf {
    home().join("data")
}

pub fn log_dir() -> PathBuf {
    let dir = home().join("logs");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

pub fn load() -> Config {
    std::fs::read(home().join("desktop.json"))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}

pub fn save(cfg: &Config) {
    if let Ok(b) = serde_json::to_vec_pretty(cfg) {
        let _ = std::fs::write(home().join("desktop.json"), b);
    }
}

fn random_secret() -> String {
    let mut buf = [0u8; 24];
    getrandom::getrandom(&mut buf).expect("系统随机数不可用");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// 本机服务的登录密码：第一次用时随机生成。发布版存在钥匙串里；
/// 开发版每次编译签名都会变，钥匙串会反复弹窗问，所以存在数据目录的文件里（权限 600）。测试目录（OSA_DESKTOP_HOME）也一样
pub fn secret() -> String {
    if !cfg!(debug_assertions) && std::env::var_os("OSA_DESKTOP_HOME").is_none() {
        if let Ok(entry) = keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_USER) {
            if let Ok(s) = entry.get_password() {
                return s;
            }
            let s = random_secret();
            if entry.set_password(&s).is_ok() {
                return s;
            }
        }
    }
    let file = home().join("local-server.secret");
    if let Ok(s) = std::fs::read_to_string(&file) {
        if !s.trim().is_empty() {
            return s.trim().to_string();
        }
    }
    let s = random_secret();
    let _ = std::fs::write(&file, &s);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600));
    }
    s
}

// ---------- 服务器借用这台 Mac（link.rs） ----------

/// 借用令牌：服务器发的，按服务器地址（https://osa.example.com）分开存，值是 {"token", "id"}。
/// 发布版在钥匙串里；开发版和测试目录在数据目录的 link-tokens.json 里（权限 600），理由同 secret()
fn link_in_keychain() -> bool {
    !cfg!(debug_assertions) && std::env::var_os("OSA_DESKTOP_HOME").is_none()
}

fn link_file() -> PathBuf {
    home().join("link-tokens.json")
}

fn read_link_file() -> serde_json::Map<String, serde_json::Value> {
    std::fs::read(link_file()).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

fn write_private(path: &std::path::Path, data: &[u8]) {
    let _ = std::fs::write(path, data);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
}

pub fn link_get(origin: &str) -> Option<(String, i64)> {
    let v: serde_json::Value = if link_in_keychain() {
        let s = keyring::Entry::new(KEYCHAIN_SERVICE, &format!("link {origin}")).ok()?.get_password().ok()?;
        serde_json::from_str(&s).ok()?
    } else {
        read_link_file().get(origin)?.clone()
    };
    Some((v["token"].as_str()?.to_string(), v["id"].as_i64().unwrap_or(0)))
}

pub fn link_set(origin: &str, token: &str, id: i64) -> bool {
    let v = serde_json::json!({ "token": token, "id": id });
    if link_in_keychain() {
        return keyring::Entry::new(KEYCHAIN_SERVICE, &format!("link {origin}")).and_then(|e| e.set_password(&v.to_string())).is_ok();
    }
    let mut all = read_link_file();
    all.insert(origin.into(), v);
    write_private(&link_file(), &serde_json::to_vec_pretty(&all).unwrap_or_default());
    true
}

pub fn link_clear(origin: &str) {
    if link_in_keychain() {
        if let Ok(e) = keyring::Entry::new(KEYCHAIN_SERVICE, &format!("link {origin}")) {
            let _ = e.delete_credential();
        }
        return;
    }
    let mut all = read_link_file();
    if all.remove(origin).is_some() {
        write_private(&link_file(), &serde_json::to_vec_pretty(&all).unwrap_or_default());
    }
}
