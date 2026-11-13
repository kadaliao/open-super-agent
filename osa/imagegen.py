"""生图服务的接口：各家的参数不一样，都在这里按服务分开写，外面只用 draw(prompt, provider, model, refs) 和 spec()。

- 哪一家按接口地址认：硅基流动（siliconflow）、火山方舟（volces.com / bytepluses.com），其余的当兼容 OpenAI 的。
- 参考图（refs）：照着改、照着画的图片，统一成 Ref（内容 + mime + 文件名），每家各自拼进请求：
  - 硅基流动：都走 /images/generations，JSON 里放 base64 data URL（2026-11 的接口文档）。
    Kolors 可以带一张（图生图）；Qwen-Image-Edit 必须带一张；Qwen-Image-Edit-2509 最多三张（image、image2、image3）；
    这两个改图的不收 image_size。FLUX.1-Kontext-pro / max 必须带一张，字段叫 input_image；Kontext-dev 也必须带，字段叫 image。
  - 火山方舟 Seedream 4 以后：/images/generations 的 image，一张写字符串、几张写数组，收 data URL，最多 10 张。
  - OpenAI 的 gpt-image（别家转发的同名模型也算）：/images/edits，multipart 的 image[]，最多 16 张，不写尺寸（跟着原图）。
  - 别的不收参考图（spec().refs == 0）。名字里带 edit、kontext 的只能改图（needs_ref），没有参考图时不拿它们画。
- 以后接视频生成也照这个办法：每家写一个 spec（能收几张图、几段视频）和请求怎么拼。
  注意视频参考大多只收公网网址、不收 base64，要先有能给外面临时访问的地址。
"""
import base64
import re
from dataclasses import dataclass
import httpx
from . import i18n, prices, providers
from .config import CFG

MAX_GENERATED = 20 << 20        # 生图服务给回来的原图
_TRANSPORT = None               # 测试时换成假的

# 能画图的模型，按名字认
IMAGE_MODEL = re.compile(r"kolors|qwen-image|z-image|flux|stable-diffusion|sdxl|dall-e|gpt-image|cogview|seedream|imagen-|wanx|hidream|hunyuan-image", re.I)
EDIT_ONLY = re.compile(r"edit|kontext", re.I)   # 只能改图：必须带参考图
# 硅基流动每个模型要的尺寸（文档里推荐的正方形尺寸）
SF_SIZE = {"qwen-image": "1328x1328", "z-image": "512x512"}


@dataclass(frozen=True)
class Spec:
    refs: int = 0             # 最多收几张参考图，0 是不收
    needs_ref: bool = False   # 只能改图


@dataclass(frozen=True)
class Ref:
    data: bytes
    mime: str
    name: str = ""

    def data_url(self) -> str:
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode()


def vendor(provider: str) -> str:
    base = ((CFG["providers"].get(provider) or {}).get("base_url") or "").lower()
    if "siliconflow" in base:
        return "siliconflow"
    if "volces.com" in base or "bytepluses.com" in base:
        return "ark"
    return "openai"


def spec(provider: str, model: str) -> Spec:
    """这个模型能不能照着图片画、最多几张、是不是只能改图。"""
    x = model.lower()
    needs = bool(EDIT_ONLY.search(x))
    v = vendor(provider)
    if v == "siliconflow":
        if re.search(r"qwen-image-edit-\d", x):
            return Spec(3, True)
        if "qwen-image-edit" in x or "kontext" in x:
            return Spec(1, True)
        if "kolors" in x:
            return Spec(1)
    elif v == "ark":
        if re.search(r"seedream-[4-9]", x):
            return Spec(10)
    elif "gpt-image" in x:
        return Spec(16)
    return Spec(0, needs)


def _sf_body(model: str, prompt: str, refs: list[Ref]) -> dict:
    x = model.lower()
    body = {"model": model, "prompt": prompt, "batch_size": 1}
    if "kontext" in x and "dev" not in x:   # FLUX.1-Kontext-pro / max
        body["input_image"] = refs[0].data_url()
    else:
        for i, r in enumerate(refs):
            body["image" if i == 0 else f"image{i + 1}"] = r.data_url()
    if "edit" not in x and "kontext" not in x:
        body["image_size"] = next((v for k, v in SF_SIZE.items() if k in x), "1024x1024")
    return body


def _error(r: httpx.Response) -> str:
    try:
        d = r.json()
        msg = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else d.get("error")
        msg = msg or d.get("message") or d.get("detail")
    except ValueError:
        msg = r.text[:200]
    return f"HTTP {r.status_code}" + (f"：{msg}" if msg else "")


def record(provider: str, model: str):
    """画了一张：费用统计里记一行（0 token，费用按每张的价格，不知道的按 0）。"""
    from . import memory
    memory.add_usage("image", model, provider=provider, cost=prices.image_price(provider, model) or 0.0)


def sniff(b: bytes) -> str | None:
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


async def draw(prompt: str, provider: str, model: str, refs: list[Ref] | None = None) -> tuple[bytes, str]:
    """让生图模型画一张，返回 (原图, mime)。refs：参考图，模型收不了这么多、或者只能改图却没给时报错。"""
    prompt = (prompt or "").strip()[:1000]
    refs = refs or []
    if not prompt:
        raise ValueError(i18n.t("写一句想要画什么"))
    if provider not in CFG["providers"] or not providers.usable(provider):
        raise ValueError(i18n.t("没有这个模型服务：{name}", name=provider))
    if not model or not IMAGE_MODEL.search(model):
        raise ValueError(i18n.t("{model} 不是画图的模型", model=model))
    s = spec(provider, model)
    if len(refs) > s.refs:
        raise ValueError(i18n.t("{model} 不能照着图片画", model=model) if not s.refs else
                         i18n.t("{model} 最多参考 {n} 张图", model=model, n=s.refs))
    if s.needs_ref and not refs:
        raise ValueError(i18n.t("{model} 只能改图，要给它一张原图", model=model))
    base = CFG["providers"][provider]["base_url"].rstrip("/")
    key = providers.api_key(provider)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    v = vendor(provider)
    async with httpx.AsyncClient(timeout=180, follow_redirects=True, transport=_TRANSPORT) as c:
        try:
            if v == "siliconflow":
                r = await c.post(f"{base}/images/generations", json=_sf_body(model, prompt, refs), headers=headers)
            elif refs and v == "openai":   # gpt-image 改图
                # 文件名只用来让对方认格式；不用原名，免得中文文件名在有的网关上出问题
                files = [("image[]", (f"image{i + 1}.{x.mime.split('/')[1].replace('jpeg', 'jpg')}", x.data, x.mime))
                         for i, x in enumerate(refs)]
                r = await c.post(f"{base}/images/edits", data={"model": model, "prompt": prompt, "n": "1"}, files=files, headers=headers)
            else:
                body = {"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"}
                if refs:   # 火山方舟
                    body["image"] = refs[0].data_url() if len(refs) == 1 else [x.data_url() for x in refs]
                r = await c.post(f"{base}/images/generations", json=body, headers=headers)
        except httpx.TimeoutException:
            raise ValueError(i18n.t("画图失败：{e}", e=i18n.t("连接超时：{url}", url=base)))
        except httpx.HTTPError:
            raise ValueError(i18n.t("画图失败：{e}", e=i18n.t("连不上 {url}", url=base)))
        if r.status_code >= 400:
            raise ValueError(i18n.t("画图失败：{e}", e=_error(r)))
        d = r.json()
        item = ((d.get("data") or d.get("images") or [None])[0]) or {}
        if item.get("b64_json"):
            b = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            got = await c.get(item["url"])   # 硅基流动的图片地址一小时后失效，当场取回来
            if got.status_code >= 400:
                raise ValueError(i18n.t("画图失败：{e}", e=f"HTTP {got.status_code}"))
            b = got.content
        else:
            raise ValueError(i18n.t("画图服务没有返回图片"))
    mime = sniff(b)
    if not mime or len(b) > MAX_GENERATED:
        raise ValueError(i18n.t("画图服务没有返回图片"))
    record(provider, model)
    return b, mime
