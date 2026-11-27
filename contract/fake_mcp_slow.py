"""接口测试用的 stdio 应用（test_tasks.py 用）：会卡住一会儿的只读工具和写工具，做了什么记进 log 文件，测重启时正在执行的那一步。"""
import time

from mcp.server.mcpserver import MCPServer

app = MCPServer("slow")


def _log(path: str, line: str):
    if path:
        with open(path, "a") as f:
            f.write(line + "\n")


@app.tool()
def slow_lookup(seconds: float = 1.0, log: str = "") -> str:
    """慢慢查一下（只读）"""
    _log(log, "lookup-start")
    time.sleep(seconds)
    _log(log, "lookup-done")
    return "查到了：一切正常"


@app.tool()
def slow_append(text: str = "", seconds: float = 1.0, log: str = "") -> str:
    """慢慢往记录里追加一行（写操作）"""
    _log(log, f"append-start {text}")
    time.sleep(seconds)
    _log(log, f"append-done {text}")
    return f"已追加：{text}"


if __name__ == "__main__":
    app.run("stdio")
