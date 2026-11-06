"""Bot 模板：把一个 Bot 的人设、头像、技能、要用的应用、定时任务打包成一个文件，一键装出一个现成的助手。

文件是 YAML（.osabot.yaml）：
    osa_template: 1
    name: 每日资讯助手
    description: 一句话说明
    avatar: {shape: star, color: "#f6c453"}
    instructions: 人设和工作方式
    apps: [exa, fetch]                      只写应用名，不带配置和密钥；装的时候列出来，没连的去应用页连
    skills: [{name, description, body}]
    routines: [{name, prompt, skill, trigger}]   trigger 和定时任务的一样（daily / every / webhook）

- 内置模板在 osa/templates/*.yaml；也可以从文件、链接装（别人分享的 /template/<token>、GitHub 上的文件）。
- 导出、分享的是 Bot 现在的样子：它自己的技能，加上定时任务用到的共用技能。对话、记忆、资料库不带。
- 装之前先完整检查一遍（parse），通过了才建 Bot、技能、定时任务，不会装到一半。定时任务可以选先暂停。
- 模板里的人设、技能、定时任务都是给 Bot 的指令，来路不明的装之前要看一遍（预览里全列出来了）。
"""
import re
import secrets
from pathlib import Path
import yaml
from . import bots, catalog, mcp_hub, routines, skills
from .memory import conn

BUILTIN = Path(__file__).parent / "templates"
FORMAT = 1
MAX_SKILLS, MAX_ROUTINES, MAX_APPS = 30, 20, 20
APP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

if "template_token" not in {r[1] for r in conn.execute("pragma table_info(bots)")}:
    conn.execute("alter table bots add column template_token text")
conn.execute("create unique index if not exists bots_template_token on bots(template_token)")
conn.commit()


# ---------- 检查 ----------
def parse(text: str) -> dict:
    """解析并检查一份模板，返回干净的字典；有问题抛 ValueError（一次说一个）。"""
    if len(text or "") > 400_000:
        raise ValueError("文件太大了，不像是一个 Bot 模板")
    try:
        t = yaml.safe_load(text or "")
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 写错了：{str(e).splitlines()[0]}")
    if not isinstance(t, dict) or "osa_template" not in t:
        raise ValueError("这不是 open-super-agent 的 Bot 模板（开头要有 osa_template: 1）")
    if t["osa_template"] != FORMAT:
        raise ValueError("这个模板是更新版本的 open-super-agent 做的，先升级再装")
    name = str(t.get("name") or "").strip()[:40]
    if not name:
        raise ValueError("模板缺少 name（Bot 的名字）")
    av = t.get("avatar") if isinstance(t.get("avatar"), dict) else {}
    avatar = {"shape": av.get("shape") if av.get("shape") in bots.SHAPES else bots.SHAPES[0],
              "color": av.get("color") if isinstance(av.get("color"), str) and COLOR.match(av["color"]) else bots.COLORS[0]}
    instructions = str(t.get("instructions") or "").strip()
    if len(instructions) > 4000:
        raise ValueError("人设（instructions）最多 4000 个字")
    apps = t.get("apps") or []
    if not isinstance(apps, list) or len(apps) > MAX_APPS or not all(isinstance(a, str) and APP_ID.match(a) for a in apps):
        raise ValueError("apps 应该是应用名的列表（小写字母开头，比如 notion、gmail）")
    out_skills, seen = [], set()
    raw_skills = t.get("skills") or []
    if not isinstance(raw_skills, list) or len(raw_skills) > MAX_SKILLS:
        raise ValueError(f"skills 应该是一个列表，最多 {MAX_SKILLS} 个")
    for i, s in enumerate(raw_skills, 1):
        if not isinstance(s, dict):
            raise ValueError(f"第 {i} 个技能应该写 name、description、body")
        sname = skills._clean_name(s.get("name") or "")
        try:
            body = str(s.get("body") or "").strip()
            skills._check(sname, body)
        except ValueError as e:
            raise ValueError(f"技能 {s.get('name') or i}：{e}")
        if sname in seen:
            raise ValueError(f"技能 /{sname} 写了两次")
        seen.add(sname)
        out_skills.append({"name": sname, "description": " ".join(str(s.get("description") or "").split())[:200], "body": body})
    out_routines = []
    raw_routines = t.get("routines") or []
    if not isinstance(raw_routines, list) or len(raw_routines) > MAX_ROUTINES:
        raise ValueError(f"routines 应该是一个列表，最多 {MAX_ROUTINES} 个")
    for i, r in enumerate(raw_routines, 1):
        if not isinstance(r, dict):
            raise ValueError(f"第 {i} 个定时任务应该写 name、prompt 或 skill、trigger")
        rname = str(r.get("name") or "").strip()[:40]
        prompt = str(r.get("prompt") or "").strip()[:4000]
        skill = str(r.get("skill") or "").strip().lstrip("/")
        where = f"定时任务 {rname or i}"
        if not rname:
            raise ValueError(f"第 {i} 个定时任务缺少 name")
        if not prompt and not skill:
            raise ValueError(f"{where}：要写 prompt（做什么），或者 skill（用哪个技能）")
        if skill and skill not in seen:
            raise ValueError(f"{where}：模板里没有技能 /{skill}")
        try:
            trig = routines._clean_trigger(r.get("trigger") or {})
        except (ValueError, TypeError) as e:
            raise ValueError(f"{where}：{e}")
        if trig["kind"] == "webhook" and not re.fullmatch(r"[\w-]{1,40}", rname):
            raise ValueError(f"{where}：webhook 类的名字会出现在网址里，只能用字母、数字、- 和 _")
        out_routines.append({"name": rname, "prompt": prompt, "skill": skill, "trigger": trig})
    return {"name": name, "description": " ".join(str(t.get("description") or "").split())[:300], "avatar": avatar,
            "instructions": instructions, "apps": list(dict.fromkeys(apps)), "skills": out_skills, "routines": out_routines}


def preview(t: dict) -> dict:
    """给安装前的预览用：应用连没连、定时任务什么时候跑。"""
    have = mcp_hub.all_apps()
    known = catalog.items()
    return t | {"apps": [{"name": a, "title": catalog.title(a) if a in known or a in have else "@" + a,
                          "connected": a in have, "in_market": a in known} for a in t["apps"]],
                "routines": [r | {"when": routines.describe(r["trigger"])} for r in t["routines"]]}


# ---------- 内置 ----------
def builtin() -> list[dict]:
    out = []
    for p in sorted(BUILTIN.glob("*.yaml")):
        try:
            t = parse(p.read_text())
        except ValueError as e:
            print(f"[模板] {p.name} 有问题：{e}", flush=True)
            continue
        out.append({"id": p.stem, "name": t["name"], "description": t["description"], "avatar": t["avatar"],
                    "apps": t["apps"], "skills": len(t["skills"]), "routines": len(t["routines"])})
    return out


def builtin_text(tid: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]{1,40}", tid or "") or not (BUILTIN / f"{tid}.yaml").exists():
        raise ValueError("没有这个模板")
    return (BUILTIN / f"{tid}.yaml").read_text()


# ---------- 导出 ----------
def from_bot(bot_id: int) -> dict:
    b = bots.get(bot_id)
    if not b:
        raise ValueError("没有这个 Bot")
    rs = routines.list_for(bot_id)
    used = {r["skill"] for r in rs if r.get("skill")}
    ss = [s for s in skills.list_for(bot_id) if s["bot_id"] == bot_id or s["name"] in used]
    body = " ".join([b["instructions"]] + [s["body"] for s in ss] + [r["prompt"] for r in rs])
    first = next((x.strip(" -#*") for x in b["instructions"].splitlines() if x.strip(" -#*")), "")
    t = {"osa_template": FORMAT, "name": b["name"], "description": first[:120], "avatar": b["avatar"],
         "instructions": b["instructions"], "apps": skills.apps_in(body),
         "skills": [{"name": s["name"], "description": s["description"], "body": s["body"]} for s in ss],
         "routines": [{"name": r["name"], **({"prompt": r["prompt"]} if r["prompt"] else {}),
                       **({"skill": r["skill"]} if r["skill"] else {}), "trigger": r["trigger"]} for r in rs]}
    return t


class _Block(str):
    pass


def _block(d, s):   # 多行文字用 | 块，导出的文件好读好改
    return d.represent_scalar("tag:yaml.org,2002:str", s, style="|")


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Block, _block)


def to_text(t: dict) -> str:
    def blk(v):
        return _Block(v) if isinstance(v, str) and "\n" in v else v

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return blk(x)
    head = "# open-super-agent 的 Bot 模板：新建 Bot → 从模板创建 → 导入这个文件\n"
    return head + yaml.dump(walk(t), Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=1000)


# ---------- 分享 ----------
def share(bot_id: int, on: bool) -> str | None:
    b = conn.execute("select template_token from bots where id=?", (bot_id,)).fetchone()
    if not b:
        raise ValueError("没有这个 Bot")
    token = (b[0] or secrets.token_urlsafe(16)) if on else None
    conn.execute("update bots set template_token=? where id=?", (token, bot_id))
    conn.commit()
    return token


def token_of(bot_id: int) -> str | None:
    r = conn.execute("select template_token from bots where id=?", (bot_id,)).fetchone()
    return r[0] if r else None


def by_token(token: str) -> int | None:
    if not token:
        return None
    r = conn.execute("select id from bots where template_token=?", (token,)).fetchone()
    return r[0] if r else None


def raw_url(url: str) -> str:
    """别人给的链接 → 能直接取到文件的地址。"""
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("链接要以 http:// 或 https:// 开头")
    parts = [x for x in u.path.split("/") if x]
    if len(parts) == 2 and parts[0] == "template":
        return f"{u.scheme}://{u.netloc}/api/shared/templates/{parts[1]}/raw"
    if u.netloc == "github.com" and len(parts) >= 5 and parts[2] == "blob":
        return f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{parts[3]}/{'/'.join(parts[4:])}"
    return url.strip()


async def fetch(url: str) -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(raw_url(url))
    except httpx.HTTPError as e:
        raise ValueError(f"取不到这个链接（{type(e).__name__}）")
    if r.status_code == 404:
        raise ValueError("链接打不开（404）：分享可能已经取消了，或者地址不对")
    if r.status_code >= 400:
        raise ValueError(f"链接打不开（{r.status_code}）")
    if "text/html" in r.headers.get("content-type", ""):
        raise ValueError("这个链接打开是一个网页，不是模板文件")
    return parse(r.text)


# ---------- 安装 ----------
def install(t: dict, enable_routines: bool = True, name: str = "") -> dict:
    """按检查过的模板建 Bot、技能、定时任务。出错时把建了一半的删掉。"""
    b = bots.create(name.strip()[:40] or t["name"], t["avatar"], t["instructions"])
    try:
        for s in t["skills"]:
            skills.save(b["id"], s["name"], s["body"], s["description"], "import")
        for r in t["routines"]:
            rname = r["name"]
            if r["trigger"]["kind"] == "webhook":   # webhook 的名字全局唯一：重名就加个后缀
                n = 2
                while routines.by_hook(rname):
                    rname, n = f"{r['name'][:36]}-{n}", n + 1
            routines.save(b["id"], r | {"name": rname, "enabled": enable_routines})
    except Exception:
        routines.delete_for_bot(b["id"])
        conn.execute("delete from skills where bot_id=?", (b["id"],))
        conn.commit()
        bots.delete(b["id"])
        raise
    return bots.get(b["id"])


if __name__ == "__main__":   # python -m osa.templates：检查内置模板（CI 里也跑）
    import sys
    bad = 0
    market = catalog.items()
    for p in sorted(BUILTIN.glob("*.yaml")):
        try:
            t = parse(p.read_text())
        except ValueError as e:
            print("✗", p.name, e)
            bad += 1
            continue
        if missing := [a for a in t["apps"] if a not in market]:
            print("✗", p.name, "应用市场里没有：", "、".join(missing))
            bad += 1
            continue
        text = " ".join([t["instructions"]] + [s["body"] for s in t["skills"]] + [r["prompt"] for r in t["routines"]])
        extra = sorted(set(skills._APP_REF.findall(text)) & set(market) - set(t["apps"]))
        print("✓", p.name, t["name"], f"（提到了 @{'、@'.join(extra)}，但没写进 apps）" if extra else "")
    sys.exit(1 if bad else 0)
