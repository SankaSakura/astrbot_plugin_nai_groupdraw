"""Prepare image attachments from the current QQ message for NovelAI."""
import asyncio
import base64
import io
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from time import time
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps

from .qq_gallery import field


MODES = {"角色": "character", "画风": "style", "服装": "costume", "角色+画风": "character&style"}
MAX_BYTES = 16 * 1024 * 1024


def attachment_urls(attachments):
    urls = []
    for attachment in attachments or []:
        mime = str(field(attachment, "content_type") or "").lower()
        name = str(field(attachment, "filename") or "").lower()
        if mime == "image" or mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            urls.append(field(attachment, "url"))
    return urls


def raw_message(event):
    raw = getattr(event.message_obj, "raw_message", None)
    return field(raw, "d") or raw


def image_urls(event):
    raw = raw_message(event)
    urls = attachment_urls(field(raw, "attachments"))
    # A flattened chain can contain a quoted image indistinguishable from a
    # current attachment. Prefer its actual envelope on reply events.
    if urls or field(raw, "msg_elements") or field(raw, "message_reference") or str(field(raw, "message_type")) == "103":
        return urls
    return component_images(getattr(event.message_obj, "message", []))


def component_images(parts):
    from astrbot.api.message_components import Image as QQImage
    return [getattr(part, "url", None) or getattr(part, "file", None)
            for part in parts or [] if isinstance(part, QQImage)]


def sent_at(value):
    try:
        if isinstance(value, str) and not value.isdecimal():
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo else None
        stamp = float(value)
        return stamp / 1000 if stamp > 10**12 else stamp
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@dataclass
class QuotedImage:
    urls: list
    problem: str
    explicit: bool


def inspect_quote(event):
    raw = raw_message(event)
    ref_id = field(field(raw, "message_reference"), "message_id")
    ext = field(field(raw, "message_scene"), "ext") or []
    if isinstance(ext, dict):
        ref_idx = ext.get("ref_msg_idx")
    else:
        ref_idx = next((s.partition("=")[2] for s in ext
                        if isinstance(s, str) and s.startswith("ref_msg_idx=")), None)
    elements = field(raw, "msg_elements") or []
    if not (ref_id or ref_idx):
        detail = "仅收到上下文，无法确认你回复了哪条消息。" if elements else "未收到可定位的引用，请长按图片选「回复」后再 @机器人。"
        return QuotedImage([], detail, False)
    targets = [e for e in elements if
               (ref_idx and field(e, "msg_idx") == ref_idx) or
               (ref_id and ref_id in (field(e, "message_id"), field(e, "id"), field(e, "msg_idx")))]
    if (not targets and ref_id and str(field(raw, "message_type")) == "103" and len(elements) == 1
            and not any(field(elements[0], key) for key in ("msg_idx", "message_id", "id"))):
        targets = elements
    from astrbot.api import message_components as components
    replies = [part for part in getattr(event.message_obj, "message", []) or []
               if isinstance(part, getattr(components, "Reply", ()))
               and field(part, "id") and field(part, "id") in (ref_id, ref_idx)]
    reply = replies[0] if len(replies) == 1 else None
    if len(targets) > 1 or (not targets and reply is None):
        return QuotedImage([], "QQ 提供了引用索引，但未返回可唯一匹配的原消息。", bool(ref_id))
    target = targets[0] if targets else None
    urls = attachment_urls(field(target, "attachments")) or component_images(field(reply, "chain"))
    author = field(target, "author")
    owner = field(author, "member_openid") or field(author, "id") or field(target, "sender_id") or field(reply, "sender_id")
    stamp = sent_at(field(target, "timestamp") or field(target, "time") or field(reply, "time") or None)
    current_author = field(raw, "author")
    sender_ids = (str(event.get_sender_id()), field(current_author, "member_openid"), field(current_author, "id"))
    issues = []
    if not urls:
        issues.append("引用内容没有结构化图片附件")
    elif not all(urls):
        issues.append("QQ 未提供完整的图片下载地址")
    original_group = field(target, "group_openid")
    if original_group and original_group != event.get_group_id():
        issues.append("原图来自其他群")
    if not owner or owner == "0":
        issues.append("缺少原作者")
    elif owner not in sender_ids:
        issues.append("原图并非本人发送")
    if stamp is None:
        issues.append("缺少可核对的原消息时间")
    elif not 0 <= time() - stamp <= 120:
        issues.append("原消息时间不在最近两分钟内")
    return QuotedImage(urls, "，".join(issues), bool(ref_id))


def reference_mode(body):
    normalized = unicodedata.normalize("NFKC", body).strip()
    value = re.sub(r"^参考模式\s*:?\s*", "", normalized) or "角色"
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    value = re.sub(r"\s*\+\s*", "+", value)
    if value == "风格":
        value = "画风"
    if value not in MODES:
        raise ValueError("参考模式可填：" + "、".join(MODES) + "。例如 /出图 参考模式：服装")
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


async def reference_parameters(urls, body):
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
