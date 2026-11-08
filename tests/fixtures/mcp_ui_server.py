"""测试用的 MCP 应用：带界面（MCP Apps，ui:// 资源）的天气工具。

- show_weather：模型能调，结果用界面显示（_meta.ui.resourceUri）
- refresh_weather：只给界面调（visibility: ["app"]），模型看不到
- secret_tool：只给模型调（visibility: ["model"]），界面不能调
界面用官方的 App SDK（从 jsDelivr 加载，所以资源声明了 resourceDomains），也能用 OSA_UI_HTML 换成别的页面。
"""
import os
from typing import TypedDict
from mcp.server.mcpserver import MCPServer

URI = "ui://weather/view.html"
MIME = "text/html;profile=mcp-app"
SDK = "https://cdn.jsdelivr.net/npm/@modelcontextprotocol/ext-apps@2.0.4/dist/src/app-with-deps.js"

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>weather</title>
<style>body{font-family:system-ui;margin:0;padding:14px}button{padding:6px 10px}#log{color:#666;font-size:12px}</style></head>
<body><h3 id="city">…</h3><p id="temp">…</p>
<button id="refresh">刷新</button> <button id="tell">告诉 Bot</button> <button id="secret">调只给模型的工具</button>
<p id="out"></p><p id="log"></p>
<script type="module">
import { App } from "%SDK%";
const $ = id => document.getElementById(id);
const app = new App({ name: "weather-view", version: "1.0.0" });
const show = r => { const d = r.structuredContent || {}; $("city").textContent = d.city || "?"; $("temp").textContent = d.temp + "°C " + (d.sky || ""); };
app.ontoolinput = p => { $("log").textContent = "input:" + JSON.stringify(p.arguments); };
app.ontoolresult = r => show(r);
$("refresh").onclick = async () => {
  try { const r = await app.callServerTool({ name: "refresh_weather", arguments: { city: $("city").textContent } }); show(r); $("out").textContent = "refreshed"; }
  catch (e) { $("out").textContent = "ERR " + e.message; }
};
$("secret").onclick = async () => {
  try { await app.callServerTool({ name: "secret_tool", arguments: {} }); $("out").textContent = "secret ok"; }
  catch (e) { $("out").textContent = "DENIED " + e.message; }
};
$("tell").onclick = async () => {
  await app.sendMessage({ role: "user", content: [{ type: "text", text: "界面说：" + $("city").textContent + " 已刷新" }] });
  $("out").textContent = "sent";
};
await app.connect();
const ctx = app.getHostContext() || {};
document.body.dataset.theme = ctx.theme || "";
document.body.dataset.host = (app.getHostVersion() || {}).name || "";
</script></body></html>""".replace("%SDK%", SDK)

mcp = MCPServer("weather")


class Weather(TypedDict):
    city: str
    temp: int
    sky: str

count = {"n": 0}


@mcp.tool(meta={"ui": {"resourceUri": URI}}, structured_output=True)
def show_weather(city: str) -> Weather:
    """查一个城市的天气，结果用界面显示"""
    return {"city": city, "temp": 21, "sky": "晴"}


@mcp.tool(meta={"ui": {"resourceUri": URI, "visibility": ["app"]}}, structured_output=True)
def refresh_weather(city: str) -> Weather:
    """界面里的刷新按钮"""
    count["n"] += 1
    return {"city": city, "temp": 21 + count["n"], "sky": "多云"}


@mcp.tool(meta={"ui": {"visibility": ["model"]}})
def secret_tool() -> str:
    """只给模型用"""
    return "secret"


@mcp.resource(URI, mime_type=MIME, meta={"ui": {"csp": {"resourceDomains": ["https://cdn.jsdelivr.net"]}}})
def view() -> str:
    return os.environ.get("OSA_UI_HTML") or HTML


if __name__ == "__main__":
    mcp.run("stdio")
