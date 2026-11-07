//! 手机 App 的原生部分（Swift，在 ios/ 里）：推送注册和钥匙、弹出页、分享面板、角标、点通知。
//! 只给 App 的 Rust 代码调用；服务器的网页拿不到这个插件的任何权限。
use serde::de::DeserializeOwned;
use serde_json::Value;
use tauri::{
    plugin::{Builder, TauriPlugin},
    Manager, Runtime,
};

#[cfg(target_os = "ios")]
tauri::ios_plugin_binding!(init_plugin_osa_ios);

pub struct OsaIos<R: Runtime> {
    #[cfg(target_os = "ios")]
    handle: tauri::plugin::PluginHandle<R>,
    #[cfg(not(target_os = "ios"))]
    _r: std::marker::PhantomData<fn() -> R>,
}

impl<R: Runtime> OsaIos<R> {
    /// 调 Swift 里的一个命令（OsaPlugin 的同名方法），等它完成
    pub async fn call<T: DeserializeOwned>(&self, cmd: &str, args: Value) -> Result<T, String> {
        #[cfg(target_os = "ios")]
        {
            self.handle.run_mobile_plugin_async(cmd, args).await.map_err(|e| e.to_string())
        }
        #[cfg(not(target_os = "ios"))]
        {
            let _ = (cmd, args);
            Err("only available on iOS".into())
        }
    }
}

pub trait OsaIosExt<R: Runtime> {
    fn osa_ios(&self) -> &OsaIos<R>;
}

impl<R: Runtime, T: Manager<R>> OsaIosExt<R> for T {
    fn osa_ios(&self) -> &OsaIos<R> {
        self.state::<OsaIos<R>>().inner()
    }
}

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("osa-ios")
        .setup(|app, api| {
            #[cfg(target_os = "ios")]
            let s = OsaIos::<R> { handle: api.register_ios_plugin(init_plugin_osa_ios)? };
            #[cfg(not(target_os = "ios"))]
            let s = {
                let _ = api;
                OsaIos::<R> { _r: std::marker::PhantomData }
            };
            app.manage(s);
            Ok(())
        })
        .build()
}
