"""Bot 模板：把一个 Bot 的人设、头像、技能、要用的应用、定时任务打包成一个文件，一键装出一个现成的助手。

文件是 YAML（.osabot.yaml）：
    osa_template: 1
    name: 每日资讯助手
    description: 一句话说明
    avatar: {shape: star, color: "#f6c453"}
    instructions: 人设和工作方式
    apps: [exa, fetch]                      只写应用名，不带配置和密钥；装的时候列出来，没连的去应用页连
    skills: [{name, description, body}]     可选 files（{相对路径: 文字}，技能带的脚本、参考资料）和 apps，见 docs/skills.md
    routines: [{name, prompt, skill, trigger}]   trigger 和定时任务、事件的一样（once / daily / monthly / every / event）
    en: {name, description, instructions, apps, skills: [{name, description, body}], routines: [{name, prompt}]}
                                            可选：英文界面装的版本，只写要换的字段；skills、routines 按顺序和上面一一对应

- 内置模板在 osa/templates/*.yaml；也可以从文件、链接装（别人分享的 /template/<token>、GitHub 上的文件）。
- 导出、分享的是 Bot 现在的样子：它自己的技能，加上定时任务用到的共用技能。对话、记忆、资料库不带。
- 装之前先完整检查一遍（parse），通过了才建 Bot、技能、定时任务，不会装到一半。定时任务可以选先暂停。
- 模板里的人设、技能、定时任务都是给 Bot 的指令，来路不明的装之前要看一遍（预览里全列出来了）。
- 用哪种语言在解析时定（parse 按当前界面语言）：英文界面把 en: 盖上去，预览和装的都是英文的；
  定时任务的 skill 跟着换成同一位置的英文技能名。导出、分享的模板没有 en:，原样装。
"""
import re
import secrets
from pathlib import Path
import yaml
from . import bots, catalog, events, i18n, mcp_hub, routines, skills
from .memory import conn

BUILTIN = Path(__file__).parent / "templates"
FORMAT = 1
MAX_SKILLS, MAX_ROUTINES, MAX_APPS = 30, 20, 20
APP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
EN_KEYS = ("name", "description", "instructions", "apps")
EN_LISTS = {"skills": (("name", "description", "body"),
                       i18n.N_("en.skills 要和 skills 一一对应：个数、顺序都一样，每一项只能写 name、description、body")),
            "routines": (("name", "prompt"),
                         i18n.N_("en.routines 要和 routines 一一对应：个数、顺序都一样，每一项只能写 name、prompt"))}

if "template_token" not in {r[1] for r in conn.execute("pragma table_info(bots)")}:
    conn.execute("alter table bots add column template_token text")
conn.execute("create unique index if not exists bots_template_token on bots(template_token)")
conn.commit()


# ---------- 检查 ----------
def _check_en(t: dict):
    """en: 的结构：只能写认识的字段，skills、routines 和原来的一样多。内容在盖上去以后和中文的一样检查。"""
    en = t["en"]
    if not isinstance(en, dict) or set(en) - set(EN_KEYS) - set(EN_LISTS):
        raise ValueError(i18n.t("en 里只能写 name、description、instructions、apps、skills、routines"))
    for key, (fields, msg) in EN_LISTS.items():
        xs, base = en.get(key), t.get(key) or []
        if xs is not None and not (isinstance(xs, list) and isinstance(base, list) and len(xs) == len(base) and
                                   all(x is None or isinstance(x, dict) and set(x) <= set(fields) for x in xs)):
            raise ValueError(i18n.t(msg))


def _english(t: dict) -> dict:
    """把 en: 里写了的字段盖到原来的上面（没写、写空的保持原样）。定时任务的 skill 换成同一位置的英文技能名。"""
    en = t["en"]

    def over(base, e):
        return base | {k: v for k, v in (e or {}).items() if v} if isinstance(base, dict) else base
    out = t | {k: en[k] for k in EN_KEYS if en.get(k)}
    rename = {}
    if isinstance(t.get("skills"), list) and en.get("skills"):
        out["skills"] = [over(s, e) for s, e in zip(t["skills"], en["skills"])]
        rename = {skills._clean_name(s.get("name") or ""): skills._clean_name(e["name"])
                  for s, e in zip(t["skills"], en["skills"]) if isinstance(s, dict) and e and e.get("name")}
    if isinstance(t.get("routines"), list):
        rs = [over(r, e) for r, e in zip(t["routines"], en.get("routines") or [None] * len(t["routines"]))]
        out["routines"] = [r | {"skill": rename.get(str(r["skill"]).strip().lstrip("/"), r["skill"])}
                           if isinstance(r, dict) and r.get("skill") else r for r in rs]
    return out


def parse(text: str, lang: str | None = None) -> dict:
    """解析并检查一份模板，返回干净的字典；有问题抛 ValueError（一次说一个）。
    lang 是装哪种语言的版本，不给就按现在的界面语言：英文的用 en: 里的字段（有的话）。返回的字典里没有 en。"""
    if len(text or "") > 400_000:
        raise ValueError(i18n.t("文件太大了，不像是一个 Bot 模板"))
    try:
        t = yaml.safe_load(text or "")
    except yaml.YAMLError as e:
        raise ValueError(i18n.t("YAML 写错了：{err}", err=str(e).splitlines()[0]))
    if not isinstance(t, dict) or "osa_template" not in t:
        raise ValueError(i18n.t("这不是 open-super-agent 的 Bot 模板（开头要有 osa_template: 1）"))
    if t["osa_template"] != FORMAT:
        raise ValueError(i18n.t("这个模板是更新版本的 open-super-agent 做的，先升级再装"))
    if t.get("en") is not None:
        _check_en(t)
        if (lang or i18n.lang()) == "en":
            t = _english(t)
    name = str(t.get("name") or "").strip()[:40]
    if not name:
        raise ValueError(i18n.t("模板缺少 name（Bot 的名字）"))
    av = t.get("avatar") if isinstance(t.get("avatar"), dict) else {}
    avatar = {"shape": av.get("shape") if av.get("shape") in bots.SHAPES else bots.SHAPES[0],
              "color": av.get("color") if isinstance(av.get("color"), str) and COLOR.match(av["color"]) else bots.COLORS[0]}
    instructions = str(t.get("instructions") or "").strip()
    if len(instructions) > 4000:
        raise ValueError(i18n.t("人设（instructions）最多 4000 个字"))
    apps = t.get("apps") or []
    if not isinstance(apps, list) or len(apps) > MAX_APPS or not all(isinstance(a, str) and APP_ID.match(a) for a in apps):
        raise ValueError(i18n.t("apps 应该是应用名的列表（小写字母开头，比如 notion、gmail）"))
    out_skills, seen = [], set()
    raw_skills = t.get("skills") or []
    if not isinstance(raw_skills, list) or len(raw_skills) > MAX_SKILLS:
        raise ValueError(i18n.t("skills 应该是一个列表，最多 {n} 个", n=MAX_SKILLS))
    for i, s in enumerate(raw_skills, 1):
        if not isinstance(s, dict):
            raise ValueError(i18n.t("第 {i} 个技能应该写 name、description、body", i=i))
        sname = skills._clean_name(s.get("name") or "")
        try:
            body = str(s.get("body") or "").strip()
            skills._check(sname, body)
        except ValueError as e:
            raise ValueError(i18n.t("技能 {name}：{err}", name=s.get("name") or i, err=e))
        if sname in seen:
            raise ValueError(i18n.t("技能 /{name} 写了两次", name=sname))
        seen.add(sname)
        try:   # 技能带的文件（scripts/、references/）和声明的应用，和技能包里的一样
            files = skills.check_files(s.get("files"))
        except ValueError as e:
            raise ValueError(i18n.t("技能 {name}：{err}", name=sname, err=e))
        out_skills.append({"name": sname, "description": " ".join(str(s.get("description") or "").split())[:200], "body": body,
                           "files": files, "apps": skills.clean_apps(s["apps"]) if s.get("apps") is not None else None})
    out_routines = []
    raw_routines = t.get("routines") or []
    if not isinstance(raw_routines, list) or len(raw_routines) > MAX_ROUTINES:
        raise ValueError(i18n.t("routines 应该是一个列表，最多 {n} 个", n=MAX_ROUTINES))
    for i, r in enumerate(raw_routines, 1):
        if not isinstance(r, dict):
            raise ValueError(i18n.t("第 {i} 个定时任务应该写 name、prompt 或 skill、trigger", i=i))
        rname = str(r.get("name") or "").strip()[:40]
        prompt = str(r.get("prompt") or "").strip()[:4000]
        skill = str(r.get("skill") or "").strip().lstrip("/")
        if not rname:
            raise ValueError(i18n.t("第 {i} 个定时任务缺少 name", i=i))
        if not prompt and not skill:
            raise ValueError(i18n.t("定时任务 {name}：要写 prompt（做什么），或者 skill（用哪个技能）", name=rname))
        if skill and skill not in seen:
            raise ValueError(i18n.t("定时任务 {name}：模板里没有技能 /{skill}", name=rname, skill=skill))
        try:
            trig = routines._clean_trigger(r.get("trigger") or {})
        except (ValueError, TypeError) as e:
            raise ValueError(i18n.t("定时任务 {name}：{err}", name=rname, err=e))
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
        raise ValueError(i18n.t("没有这个模板"))
    return (BUILTIN / f"{tid}.yaml").read_text()


# ---------- 导出 ----------
def from_bot(bot_id: int) -> dict:
    b = bots.get(bot_id)
    if not b:
        raise ValueError(i18n.t("没有这个 Bot"))
    rs = routines.list_for(bot_id)
    used = {r["skill"] for r in rs if r.get("skill")}
    ss = [s for s in skills.list_for(bot_id) if s["bot_id"] == bot_id or s["name"] in used]
    body = " ".join([b["instructions"]] + [s["body"] for s in ss] + [r["prompt"] for r in rs])
    first = next((x.strip(" -#*") for x in b["instructions"].splitlines() if x.strip(" -#*")), "")
    t = {"osa_template": FORMAT, "name": b["name"], "description": first[:120],
         "avatar": {k: b["avatar"][k] for k in ("shape", "color") if k in b["avatar"]},   # 头像图片只在这台上有，不带
         "instructions": b["instructions"], "apps": sorted(set(skills.apps_in(body)) | {a for s in ss for a in s["apps"] or []}),
         "skills": [{"name": s["name"], "description": s["description"], "body": s["body"],
                     **({"apps": s["apps"]} if s["apps"] is not None else {}), **({"files": s["files"]} if s["files"] else {})}
                    for s in ss],
         "routines": [{"name": r["name"], **({"prompt": r["prompt"]} if r["prompt"] else {}),
                       **({"skill": r["skill"]} if r["skill"] else {}), "trigger": events.portable(r["trigger"])} for r in rs]}
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
    head = "# " + i18n.t("open-super-agent 的 Bot 模板：新建 Bot → 从模板创建 → 导入这个文件") + "\n"
    return head + yaml.dump(walk(t), Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=1000)


# ---------- 分享 ----------
def share(bot_id: int, on: bool) -> str | None:
    b = conn.execute("select template_token from bots where id=?", (bot_id,)).fetchone()
    if not b:
        raise ValueError(i18n.t("没有这个 Bot"))
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
        raise ValueError(i18n.t("链接要以 http:// 或 https:// 开头"))
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
        raise ValueError(i18n.t("取不到这个链接（{err}）", err=type(e).__name__))
    if r.status_code == 404:
        raise ValueError(i18n.t("链接打不开（404）：分享可能已经取消了，或者地址不对"))
    if r.status_code >= 400:
        raise ValueError(i18n.t("链接打不开（{code}）", code=r.status_code))
    if "text/html" in r.headers.get("content-type", ""):
        raise ValueError(i18n.t("这个链接打开是一个网页，不是模板文件"))
    return parse(r.text)


# ---------- 安装 ----------
def install(t: dict, enable_routines: bool = True, name: str = "") -> dict:
    """按检查过的模板建 Bot、技能、定时任务。出错时把建了一半的删掉。"""
    b = bots.create(name.strip()[:40] or t["name"], t["avatar"], t["instructions"])
    try:
        for s in t["skills"]:
            skills.save(b["id"], s["name"], s["body"], s["description"], "import", files=s.get("files") or {}, apps=s.get("apps"))
        for r in t["routines"]:
            # 事件的 webhook 密钥每装一次重新生成（parse 时生成的那个不用）
            trig = {k: v for k, v in r["trigger"].items() if k not in ("key", "path")}
            routines.save(b["id"], r | {"trigger": trig, "enabled": enable_routines})
    except Exception:
        routines.delete_for_bot(b["id"])
        conn.execute("delete from skills where bot_id=?", (b["id"],))
        conn.commit()
        bots.delete(b["id"])
        raise
    return bots.get(b["id"])


if __name__ == "__main__":   # python -m osa.templates：检查内置模板（CI 里也跑），中英文两个版本都检查
    import sys
    bad = 0
    market = catalog.items()
    for p in sorted(BUILTIN.glob("*.yaml")):
        for lang in i18n.LANGS:
            try:
                t = parse(p.read_text(), lang)
            except ValueError as e:
                print("✗", p.name, lang, e)
                bad += 1
                continue
            if missing := [a for a in t["apps"] if a not in market]:
                print("✗", p.name, lang, "应用市场里没有：", "、".join(missing))
                bad += 1
                continue
            text = " ".join([t["instructions"]] + [s["body"] for s in t["skills"]] + [r["prompt"] for r in t["routines"]])
            extra = sorted(set(skills._APP_REF.findall(text)) & set(market) - set(t["apps"]))
            notes = [f"提到了 @{'、@'.join(extra)}，但没写进 apps"] if extra else []
            if lang == "en" and i18n.CJK.search(" ".join([t["name"], t["description"], text]
                                                          + [s["name"] + s["description"] for s in t["skills"]]
                                                          + [r["name"] for r in t["routines"]])):
                notes.append("英文版里还有中文：没写 en:，或者 en: 里漏了字段")
            print("✓", p.name, lang, t["name"], f"（{'；'.join(notes)}）" if notes else "")
    sys.exit(1 if bad else 0)
