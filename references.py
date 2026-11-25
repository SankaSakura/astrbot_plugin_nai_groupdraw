"""Prepare image attachments from the current QQ message for NovelAI."""
import asyncio
import base64
import io
import math
import re
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps

from .qq_gallery import field


MODES = {"角色": "character", "画风": "style", "风格": "style", "服装": "outfit",
         "角色+画风": "character&style"}
MAX_BYTES = 16 * 1024 * 1024


def image_urls(event):
    raw = getattr(event.message_obj, "raw_message", None)
    attachments = field(field(raw, "d") or raw, "attachments") or []
    urls = []
    for attachment in attachments:
        mime = str(field(attachment, "content_type") or "").lower()
        name = str(field(attachment, "filename") or "").lower()
        if mime == "image" or mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            urls.append(field(attachment, "url"))
    if not urls:
        from astrbot.api.message_components import Image as QQImage
        urls = [getattr(part, "url", None) or getattr(part, "file", None)
                for part in getattr(event.message_obj, "message", []) or []
                if isinstance(part, QQImage)]
    return urls


def reference_mode(body):
    value = re.sub(r"^参考模式\s*[:：]?\s*", "", body.strip()) or "角色"
    if value not in MODES:
        raise ValueError("参考模式可填：角色、画风、服装、角色+画风。例如 /出图 参考模式：服装")
    return MODES[value]


def encode_image(data):
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 24_000_000:
                raise ValueError("参考图超过 2400 万像素，请缩小后重发。")
            image = ImageOps.exif_transpose(source).convert("RGBA")
        sizes = ((1024, 1536), (1472, 1472), (1536, 1024))
        ratio = image.width / image.height
        size = min(sizes, key=lambda wh: abs(math.log(ratio * wh[1] / wh[0])))
        fitted = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size)
        canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2), fitted)
        output = io.BytesIO()
        canvas.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")
    except (OSError, Image.DecompressionBombError, SyntaxError) as error:
        raise ValueError("参考图读取失败，请重新附上可打开的图片。") from error


async def fetch_image(session, url, remaining):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("QQ 未提供参考图地址，请重新附图。")
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("QQ 参考图地址不可用，请重新附图。")
    url = parts._replace(scheme="https").geturl()
    try:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError("QQ 参考图下载失败，请重新附图。")
            data = bytearray()
            async for block in response.content.iter_chunked(65536):
                if len(data) + len(block) > remaining:
                    raise ValueError("参考图合计超过 16 MiB，请缩小图片后重发。")
                data.extend(block)
            return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ValueError("QQ 参考图下载失败，请重新附图。") from error


async def reference_parameters(event, body):
    urls = image_urls(event)
    if not urls:
        return {}
    mode = reference_mode(body)
    images = []
    remaining = MAX_BYTES
    encoded_size = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        for url in urls:
            data = await fetch_image(session, url, remaining)
            remaining -= len(data)
            encoded = await asyncio.to_thread(encode_image, data)
            encoded_size += len(encoded)
            if encoded_size > 2 * MAX_BYTES:
                raise ValueError("参考图编码后过大，请减少本次附图。")
            images.append(encoded)
    return {
        "director_reference_images": images,
        "director_reference_descriptions": [
            {"caption": {"base_caption": mode, "char_captions": []}} for _ in images],
        "director_reference_strength_values": [1.0] * len(images),
        "director_reference_secondary_strength_values": [0.0] * len(images),
        "director_reference_information_extracted": [1.0] * len(images),
    }
