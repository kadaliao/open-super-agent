"""对话里生成视频：Router 的 generate_video 工具。各家接口怎么调按配方（generation.yaml），这里管流程。

- 用设置 → 模型 → 生成视频 里选的模型（settings 表的 video_model）。带了首帧、参考图、参考视频时，选的那个收不了就换一个能收的
  （排序按配方的 prefer.video）。一个都没有就照实说。
- 要花钱、要几分钟：和画图一样默认先弹确认卡片（approvals.ASK）。同意后在对话里放一条“正在生成”（role=task、kind=video_job），
  后台提交、隔一会儿查一次；好了把视频存进 media，发一条 kind=video 的消息（播放器；推送、聊天软件也收到），“正在生成”那条标成 done。
  失败的标成 failed，再发一条说明（kind=note）。“正在生成”那条的变化用 message_meta 事件推给网页。
- 提交后的任务记在“正在生成”那条的 meta.job 里，服务重启后 resume() 接着查；还没提交上就重启的算失败（不重复提交，免得花两份钱）。
- 参考素材：images 写图片编号或路径，image_use 决定当首帧（frames，默认）还是参考图（reference）；videos 写视频编号或路径。
  参考视频、要网址的配方给的是临时公网地址（generation.public_link），服务器的网页地址外面打不开时用不了。
"""
import asyncio
import json
import re
import time
from types import SimpleNamespace
from . import approvals, generation, i18n, images, library, media, memory, prices
from .memory import conn

TOOL = "generate_video"
WAIT_S = 40 * 60          # 最多等多久
POLL_S = (5, 10, 20)      # 头一分钟 5 秒查一次，十分钟内 10 秒，之后 20 秒
MAX_POLL_ERRORS = 20      # 连续查不到（网络问题）这么多次算失败
MAX_REFS = 10
GENERATE_VIDEO = {"type": "function", "function": {
    "name": TOOL,
    "description": "按描述生成一段几秒到十几秒的视频，好了直接显示在对话里。要等几分钟；默认先弹卡片请用户确认（写着模型、时长和价格）。"
                   "描述写清楚：主体、动作、镜头怎么动、风格、光线。用户要让一张图动起来，或者照着图里的人物、商品、风格拍，把图放进 images；"
                   "照着某段视频的动作、运镜拍，把视频放进 videos。",
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "视频描述，最多 1000 字"},
        "images": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_REFS,
                   "description": "图片（可选）：对话里图片的编号（“[图片]”后面那串）或电脑里图片的路径（“[附件]”后面那个）"},
        "image_use": {"type": "string", "enum": ["frames", "reference"],
                      "description": "frames：第 1 张当开头画面（有第 2 张就当结尾画面），视频从这张图动起来；"
                                     "reference：参考图里的人物、商品、风格，画面重新拍。不写时能当开头画面就当开头画面"},
        "videos": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                   "description": "参考视频（可选）：对话里视频的编号（“[视频]”后面那串）或电脑里视频的路径"},
        "seconds": {"type": "integer", "description": "想要几秒（可选，模型不支持就取最接近的）"},
        "ratio": {"type": "string", "description": "画面比例（可选）：16:9、9:16、1:1 这类；不写时有开头画面就跟着它"}},
        "required": ["prompt"]}}}

NO_MODEL = ("没生成：还没有能生成视频的模型。照实告诉用户：去 设置 → 模型 添加有视频模型的服务（硅基流动的 Wan、阿里云百炼的万相、"
            "智谱的 CogVideoX、火山方舟的 Seedance），再在“生成视频”那一行选模型。不要假装生成了，也不要派任务去做。")

notify = None        # 由 server 设置：async (event) -> None
_running: dict[int, asyncio.Task] = {}   # “正在生成”那条消息的 id → 后台任务


def _setting() -> dict:
    r = conn.execute("select value from settings where key='video_model'").fetchone()
    return json.loads(r[0]) if r else {}


def choice() -> dict:
    c = _setting()
    return {"provider": c.get("provider") or "", "model": c.get("model") or ""}


def choose(provider: str, model: str):
    conn.execute("insert or replace into settings values('video_model', ?)", (json.dumps({"provider": provider, "model": model}),))
    conn.commit()


async def models() -> dict:
    """能生成视频的模型（带价格、能收什么），排序按配方的 prefer.video；default 是现在用的（只收文字时）。"""
    out = sorted(await generation.catalog("video"), key=lambda x: (generation.rank("video", x["model"]), x["provider"], x["model"]))
    c = choice()
    plain = [x for x in out if not x["needs_ref"]]
    chosen = next((x for x in out if x["provider"] == c["provider"] and x["model"] == c["model"]), None)
    return {"models": out, "default": chosen or (plain[0] if plain else out[0] if out else None)}


def _fits(m: dict, refs: list[generation.Ref]) -> bool:
    return not generation.check(generation.caps("video", m["provider"], m["model"]), refs, m["model"])


async def pick(refs: list[generation.Ref]) -> dict | None:
    """用哪个模型：设置里选的收得下这些参考素材就用它，否则用收得下的里排最前的。"""
    d = await models()
    ok = [x for x in d["models"] if _fits(x, refs)]
    c = choice()
    return next((x for x in ok if x["provider"] == c["provider"] and x["model"] == c["model"]), None) or (ok[0] if ok else None)


def _find_video(x: str) -> dict | None:
    """视频编号，或者消息附件里存过的这段视频（路径）。"""
    if re.fullmatch(r"[0-9a-f]{24}", x):
        got = media.info(x)
        if not got:
            raise ValueError(f"对话里没有编号是 {x} 的视频")
        return got
    try:
        cands = {x, library.norm(x)}
    except ValueError:
        cands = {x}
    for p in cands:
        r = conn.execute("""select json_extract(a.value, '$.media') from messages m, json_each(m.meta, '$.attachments') a
            where m.meta like '%"media"%' and json_extract(a.value, '$.path') = ? and json_extract(a.value, '$.media') is not null
            order by m.id desc limit 1""", (p,)).fetchone()
        if r and (got := media.info(r[0])) and got["mime"].startswith("video/"):
            return got
    return None


async def find_videos(items) -> list[dict]:
    out: list[dict] = []
    for x in (items if isinstance(items, list) else [items] if items else [])[:3]:
        x = str(x or "").strip().removeprefix("[视频]").removeprefix("[附件]").strip()   # i18n-ignore：上下文里的标记
        if not x:
            continue
        got = _find_video(x)
        if not got:
            att = await media.from_computer(x, video=True)
            if not att.get("media") or not str(att.get("mime") or "").startswith("video/"):
                raise ValueError(f"{att['name']} 不是视频（只认 MP4、MOV、WebM，最大 {media.MAX_VIDEO_REF >> 20} MB）")
            got = media.info(att["media"])
        if not got["mime"].startswith("video/"):
            raise ValueError(f"{got['name']} 不是视频")
        if all(r["id"] != got["id"] for r in out):
            out.append(got)
    return out


def _refs(items: list[dict], need_url: bool) -> list[generation.Ref]:
    """存好的素材 → Ref。参考视频和要网址的配方，给临时公网地址（这时候才生成，免得等确认的时候过期）。"""
    out = []
    for it in items:
        got = media.get(it["media"]) if it["role"] != "video" else None
        url = generation.public_link(it["media"]) if (it["role"] == "video" or need_url) else ""
        if (it["role"] == "video" or need_url) and not url:
            raise ValueError(i18n.t("参考视频要给视频服务一个能从外网打开的地址，这台服务器的网页地址外面打不开"))
        if it["role"] != "video" and not got:
            raise ValueError(i18n.t("参考图已经被清理掉了：{name}", name=it["name"]))
        out.append(generation.Ref(got[1] if got else b"", got[0] if got else it.get("mime") or "", it["name"], url, it["role"],
                                  it.get("w") or 0, it.get("h") or 0))
    return out


def _shape(items: list[dict]) -> list[generation.Ref]:
    """只看个数和角色的 Ref（挑模型、检查用，不读内容）。"""
    return [generation.Ref(name=x["name"], role=x["role"], w=x.get("w") or 0, h=x.get("h") or 0) for x in items]


def _seconds(m: dict, want) -> int | None:
    secs = tuple(m.get("seconds") or ())
    if not secs:
        return None
    return generation._nearest(int(want) if want else 5, secs)


async def generate(bot: dict, chat_id: int, private_chat_id: int, args: dict, post) -> str:
    """generate_video 工具。和画图一样：要问就弹卡片、在后台等，Router 这一轮照常说完。"""
    prompt = str(args.get("prompt") or "").strip()[:1000]
    if not prompt:
        return "没生成：描述是空的"
    try:
        pics = await images.find_refs(args.get("images"))
        vids = await find_videos(args.get("videos"))
    except Exception as e:
        return f"没生成：参考素材有问题：{e}"
    use = args.get("image_use") if args.get("image_use") in ("frames", "reference") else ""
    vid_items = [{"media": v["id"], "name": v["name"], "mime": v["mime"], "role": "video"} for v in vids]

    def items(role):
        return [{"media": p["id"], "name": p["name"], "mime": p["mime"], "w": p["w"], "h": p["h"], "role": role} for p in pics] + vid_items
    tries = [use] if use else (["frames", "reference"] if pics and not vids else ["reference"] if pics else ["frames"])
    m = chosen = None
    for t in tries:
        chosen = items("frame" if t == "frames" else "image")
        if m := await pick(_shape(chosen)):
            break
    if not m:
        d = await models()
        if not d["models"]:
            return NO_MODEL
        what = "、".join(f"{x['model']}（首尾帧 {x['frames']}、参考图 {x['refs']}、参考视频 {x['videos']}）" for x in d["models"][:8])
        return f"没生成：现在的视频模型都收不了这些参考素材。能用的模型和它们收什么：{what}。照实告诉用户，或者少给几个、换一种用法再试。"
    if any(x["role"] == "video" for x in chosen) and not generation.public_base():
        return ("没生成：参考视频要给视频服务一个能从外网打开的地址，这台服务器的网页地址外面打不开（比如在自己电脑上用的桌面 App），"
                "或者还没填（设置 → 聊天软件 → 网页地址）。照实告诉用户：可以不带参考视频，或者填好外面打得开的网页地址再试。")
    seconds = _seconds(m, args.get("seconds"))
    price = prices.video_price(m["provider"], m["model"], seconds)
    plan = {"prompt": prompt, "provider": m["provider"], "model": m["model"], "title": m["title"], "seconds": seconds,
            "ratio": str(args.get("ratio") or "")[:10], "refs": chosen, "price": price}
    if not approvals.decide(TOOL)[0]:
        mid = await start(bot["id"], chat_id, plan, post)
        return f"开始生成了（模型 {m['model']}{f'，{seconds} 秒' if seconds else ''}），要几分钟，好了会自动显示在对话里，你不用再回来。简短告诉用户等一会儿。" \
            if mid else "没生成：出错了"

    fake = SimpleNamespace(id=f"对话·{bot['name']}", bot_id=bot["id"], chat_id=chat_id, approval_chat_id=private_chat_id,
                           goal=i18n.t("在对话里生成视频"), background=False, waiting_approval=None)
    names = lambda role: "、".join(x["name"] for x in chosen if x["role"] == role)   # noqa: E731
    frames = [x for x in chosen if x["role"] == "frame"]
    shown = {"描述": prompt, **({("开头画面" if len(frames) == 1 else "开头和结尾画面"): names("frame")} if frames else {}),
             **({"参考图": names("image")} if names("image") else {}), **({"参考视频": names("video")} if names("video") else {}),
             "模型": f"{m['model']} · {m['title']}", **({"时长": f"{seconds} 秒"} if seconds else {}), "价格": price_text(price)}

    async def wait_and_start():
        ok, _ = await approvals.request(fake, TOOL, shown)
        if ok:
            await start(bot["id"], chat_id, plan, post)

    t = asyncio.create_task(wait_and_start())
    images._running.add(t)
    t.add_done_callback(images._running.discard)
    return (f"已弹出确认卡片（模型 {m['model']}，{price_text(price)}），用户同意后会开始生成，要几分钟，好了自动显示在对话里，你不用再回来。"
            "简短告诉用户确认一下就行，不要说已经生成好了。")


def price_text(price: float | None) -> str:
    from .config import CFG
    if price is None:
        return i18n.t("价格未知")
    if price == 0:
        return i18n.t("免费")
    return i18n.t("{price} / 段", price=f"{CFG['pricing']['currency']}{price:g}")


async def start(bot_id: int, chat_id: int, plan: dict, post) -> int:
    """在对话里放一条“正在生成”，后台提交、等结果。返回那条消息的 id。"""
    meta = {k: plan[k] for k in ("prompt", "provider", "model", "seconds", "ratio", "price")} | \
        {"refs": [{k: x.get(k) for k in ("media", "name", "role", "mime", "w", "h")} for x in plan["refs"]],
         "status": "running", "started": time.time()}
    mid = await post("task", f"[生成视频] {plan['prompt']}", chat_id, kind="video_job", **meta)
    _spawn(mid, chat_id, bot_id, meta)
    return mid


def _spawn(mid: int, chat_id: int, bot_id: int | None, meta: dict):
    t = asyncio.create_task(_run(mid, chat_id, bot_id, meta))
    _running[mid] = t
    t.add_done_callback(lambda _: _running.pop(mid, None))


async def _update(mid: int, chat_id: int, meta: dict, **fields):
    meta.update(fields)
    memory.update_meta(mid, **fields)
    if notify:
        await notify({"type": "message_meta", "chat_id": chat_id, "id": mid, "meta": meta})


async def _post(bot_id: int | None, role: str, content: str, chat_id: int, **meta):
    from . import bots, router
    if bot_id and bots.get(bot_id):
        return await router.runtime(bot_id).add(role, content, chat_id, **meta)
    return memory.add(role, content, chat_id, bot_id, **meta)


async def _run(mid: int, chat_id: int, bot_id: int | None, meta: dict):
    try:
        job = meta.get("job")
        if not job:
            r = generation.recipe("video", meta["provider"], meta["model"]) or {}
            refs = _refs(meta.get("refs") or [], r.get("image_url") == "url")
            job = await generation.submit(meta["provider"], meta["model"], meta["prompt"], refs, meta.get("seconds"), meta.get("ratio"))
            await _update(mid, chat_id, meta, job=job, seconds=job.get("seconds") or meta.get("seconds"))
        errors = 0
        while True:
            waited = time.time() - meta["started"]
            if waited > WAIT_S:
                raise ValueError(i18n.t("等了 {n} 分钟还没好", n=WAIT_S // 60))
            await asyncio.sleep(POLL_S[0] if waited < 60 else POLL_S[1] if waited < 600 else POLL_S[2])
            status, got = await generation.poll(job)
            if status == "running":
                errors = errors + 1 if got else 0
                if errors >= MAX_POLL_ERRORS:
                    raise ValueError(i18n.t("查不到进度：{e}", e=got))
                continue
            if status == "failed":
                raise ValueError(got or i18n.t("服务说失败了"))
            item = media.save(got, _name(meta["prompt"], got), limit=generation.MAX_VIDEO, video=True)
            if not item["mime"].startswith("video/"):
                raise ValueError(i18n.t("服务没有返回视频"))
            price = prices.video_price(meta["provider"], meta["model"], meta.get("seconds"))
            generation.record("video", meta["provider"], meta["model"], price)
            att = {"name": item["name"], "size": item["size"], "media": item["id"], "mime": item["mime"]}
            await _post(bot_id, "task", f"[生成视频] {meta['prompt']}", chat_id, kind="video", prompt=meta["prompt"],
                        model=meta["model"], provider=meta["provider"], price=price, seconds=meta.get("seconds"),
                        attachments=[att], **({"refs": meta["refs"]} if meta.get("refs") else {}))
            await _update(mid, chat_id, meta, status="done", media=item["id"], elapsed=round(time.time() - meta["started"]))
            return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await _update(mid, chat_id, meta, status="failed", error=str(e)[:300], elapsed=round(time.time() - meta["started"]))
        await _post(bot_id, "task", i18n.t("[没生成视频：{error}]", error=e), chat_id, kind="note")


def _name(prompt: str, data: bytes) -> str:
    ext = {"video/webm": "webm", "video/quicktime": "mov"}.get(media.sniff(data) or "", "mp4")
    words = re.sub(r"[\\/:*?\"<>|\s]+", "-", prompt.strip())[:24].strip("-")
    return f"{words or 'video'}.{ext}"


def resume():
    """服务重启：没做完的接着查；还没提交上的算失败（不重新提交，免得花两份钱）。"""
    rows = conn.execute("""select id, chat_id, bot_id, meta from messages
        where json_extract(meta, '$.kind') = 'video_job' and json_extract(meta, '$.status') = 'running'""").fetchall()
    for mid, cid, bid, raw in rows:
        meta = json.loads(raw)
        if meta.get("job"):
            _spawn(mid, cid, bid, meta)
        else:
            memory.update_meta(mid, status="failed", error=i18n.t("服务重启了，还没提交上；可以再说一次"))
