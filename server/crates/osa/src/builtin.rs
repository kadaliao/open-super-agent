//! 内置工具的注册。顺序就是给模型的顺序，别随手调换：换了模型服务的前缀缓存就不命中了。对应 osa/builtin.py。
//!
//! schema、where、gated、parallel、describe 都取自 Python 版导出的 assets/schemas.json（逐字一样）；
//! 每个工具怎么跑写在各自的模块里（tool_xxx），这里只把它们按顺序登记上。

use crate::registry::{Call, Context, Out, ROUTER, Tool};
use crate::{assets, py};
use futures::future::BoxFuture;
use serde_json::Value;
use std::sync::{Arc, LazyLock, Once};

pub static CTX: LazyLock<Context> = LazyLock::new(|| Context::new("builtin"));
static LOADED: Once = Once::new();

/// 对话里随时能用；后台任务只有正式的（不是 Heavy 的草稿）才能存页面、发文件
fn not_draft(env: &Value) -> bool {
    env["scope"] == ROUTER || py::truthy(&env["pages"])
}

fn sandbox_any(_: &Value) -> bool {
    !crate::sandbox::place(false).is_empty()
}

fn sandbox_quick(_: &Value) -> bool {
    !crate::sandbox::place(true).is_empty()
}

/// 参数里的文字（没给是空的），和 Python 的 str(a.get(k) or "") 一样
pub fn arg_s(a: &Value, k: &str) -> String {
    if py::truthy(&a[k]) { py::str_of(&a[k]) } else { String::new() }
}

type F = fn(Call) -> BoxFuture<'static, Out>;

fn reg(name: &str, run: F, progress: Option<fn(&Value) -> String>, when: Option<fn(&Value) -> bool>) {
    let info = assets::tool_info(name);
    let mut tool = Tool::new(name, info["schema"].clone(), Arc::new(run));
    tool.where_ = info["where"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|w| match w.as_str() {
                    Some("router") => Some(crate::registry::ROUTER),
                    Some("worker") => Some(crate::registry::WORKER),
                    Some("applet") => Some(crate::registry::APPLET),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default();
    tool.gated = info["gated"] == true;
    tool.parallel = info["parallel"].as_u64().unwrap_or(0) as usize;
    tool.describe = info["describe"].as_str().unwrap_or("").to_string();
    tool.when = when;
    if let Some(p) = progress {
        tool.progress = Some(Arc::new(p));
    }
    if let Err(e) = CTX.tool(tool) {
        tracing::error!("{e}");
    }
}

pub fn load() {
    LOADED.call_once(|| {
        reg(
            "web_search",
            |c| Box::pin(crate::tools::tool_web_search(c)),
            Some(|a| t!("搜索：{what}", what = py::head(&arg_s(a, "query"), 60))),
            None,
        );
        reg(
            "search_posts",
            |c| Box::pin(crate::tools::tool_search_posts(c)),
            Some(|a| t!("搜 {platform}：{what}", platform = arg_s(a, "platform"), what = py::head(&arg_s(a, "query"), 60))),
            None,
        );
        reg(
            "fetch_url",
            |c| Box::pin(crate::tools::tool_fetch_url(c)),
            Some(|a| t!("读取网页：{what}", what = py::head(&arg_s(a, "url"), 60))),
            None,
        );
        reg(
            "save_page",
            |c| Box::pin(crate::library::tool_save_page(c)),
            Some(|a| t!("保存页面：{title}", title = py::head(&arg_s(a, "title"), 40))),
            Some(not_draft),
        );
        reg("read_page", |c| Box::pin(crate::library::tool_read_page(c)), Some(|a| t!("读页面 #{id}", id = a["page_id"].clone())), None);
        reg(
            "page_data",
            |c| Box::pin(crate::library::tool_page_data(c)),
            Some(|a| t!("小应用 #{id} 的数据", id = a["page_id"].clone())),
            None,
        );
        reg(
            "send_file",
            |c| Box::pin(crate::media::tool_send_file(c)),
            Some(|a| t!("发文件：{path}", path = py::head(&arg_s(a, "path"), 60))),
            Some(not_draft),
        );
        reg("generate_image", |c| Box::pin(crate::images::tool_generate_image(c)), None, None);
        reg("generate_video", |c| Box::pin(crate::videos::tool_generate_video(c)), None, None);
        reg(
            "search_memory",
            |c| Box::pin(crate::search::tool_search_memory(c)),
            Some(|a| t!("查记忆和资料库：{query}", query = py::head(&arg_s(a, "query"), 40))),
            None,
        );
        reg(
            "use_skill",
            |c| Box::pin(crate::skills::tool_use_skill(c)),
            Some(|a| t!("看技能 /{name}", name = py::head(arg_s(a, "name").trim_start_matches('/'), 40))),
            None,
        );
        reg("create_routine", |c| Box::pin(crate::routines::tool_create(c)), None, None);
        reg("create_event", |c| Box::pin(crate::routines::tool_create_event(c)), None, None);
        reg("list_routines", |c| Box::pin(crate::routines::tool_list(c)), None, None);
        reg("update_routine", |c| Box::pin(crate::routines::tool_update(c)), None, None);
        reg("delete_routine", |c| Box::pin(crate::routines::tool_delete(c)), None, None);
        reg("run_code", |c| Box::pin(crate::sandbox::tool_run(c)), Some(crate::sandbox::progress), Some(sandbox_any));
        reg("quick_code", |c| Box::pin(crate::sandbox::tool_quick(c)), None, Some(sandbox_quick));
        crate::taskstore::register(&CTX); // ask_user：任务问用户一个问题（schema 在 taskstore.rs）
        crate::sources::register(&CTX);
    });
}

/// 还没迁过来的工具的回答
pub fn todo(name: &str) -> Out {
    Out::Text(format!("工具出错：Rust 版还没有 {name}"))
}
