//! Open Super Agent 的服务端（Rust 版）。模块和 osa/*.py 一一对应，名字不变；对外的接口、WebSocket 消息、
//! 数据库表结构和 Python 版一样（见 docs/plans/rust-rewrite.md）。

pub mod config;
pub mod db;
pub mod py;
pub mod runtime;
pub mod util;
#[macro_use]
pub mod i18n;
pub mod assets;
pub mod web;

pub mod applets;
pub mod approvals;
pub mod apps;
pub mod auth;
pub mod avatars;
pub mod backup;
pub mod board;
pub mod botadmin;
pub mod bots;
pub mod builtin;
pub mod catalog;
pub mod channels;
pub mod computer;
pub mod consolidate;
pub mod dm;
pub mod docs;
pub mod dsml;
pub mod events;
pub mod generation;
pub mod groups;
pub mod images;
pub mod library;
pub mod mcp_client;
pub mod mcp_hub;
pub mod mcp_oauth;
pub mod mcpapps;
pub mod media;
pub mod memories;
pub mod memory;
pub mod observe;
pub mod outputs;
pub mod prices;
pub mod providers;
pub mod push;
pub mod registry;
pub mod relay;
pub mod router;
pub mod routes;
pub mod routines;
pub mod sandbox;
pub mod search;
pub mod security;
pub mod server;
pub mod settings;
pub mod skills;
pub mod sources;
pub mod task;
pub mod tasklog;
pub mod taskstore;
pub mod teach;
pub mod templates;
pub mod tools;
pub mod unfurl;
pub mod vault;
pub mod videos;
pub mod watchdog;
pub mod websearch;
pub mod worker;

/// 各模块自己的表（Python 版在 import 时建）
pub fn modules_migrate() {
    computer::migrate();
    events::migrate();
    routines::migrate();
    avatars::migrate();
    library::migrate();
    docs::migrate();
    search::migrate();
    unfurl::migrate();
    mcp_hub::migrate();
    skills::migrate();
    templates::migrate();
    channels::migrate();
    push::migrate();
    media::migrate();
    consolidate::migrate();
    board::migrate();
    taskstore::migrate();
}

/// 后台循环（清理、刷新价格……）
pub fn background() {
    tokio::spawn(computer::idle_pauser());
    tokio::spawn(media::gc_loop());
    tokio::spawn(prices::price_loop());
    tokio::spawn(search::startup());
    tokio::spawn(consolidate::schedule_loop());
    videos::resume();
    tokio::spawn(board::watcher());
    tokio::spawn(taskstore::resume()); // 重启前在做的任务接着做
}
