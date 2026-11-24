import hashlib
import logging
from html import escape
from urllib.parse import quote

import aiohttp
from yarl import URL

from .menu import button_payload


async def put_png(url, image):
    target = URL(url, encoded=True)
    if target.scheme != "https" or not target.host:
        raise ValueError("QQ 图片上传地址无效")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
        async with session.put(target, data=image, headers={"Content-Type": "image/png"},
                               allow_redirects=False) as response:
            if not 200 <= response.status < 300:
                raise ValueError(f"QQ 素材上传返回 HTTP {response.status}")
            return response.headers["ETag"].strip('"')


async def send_image_card(api, group, member, message_id, image, size, code):
    from botpy.http import Route

    request = api._http.request
    base = "/v2/media-images"
    task = await request(Route("POST", base + "/prepare"), json={
        "file_name": "drawing.png", "file_size": len(image), "content_type": "image/png",
        "md5": hashlib.md5(image).hexdigest(), "sha1": hashlib.sha1(image).hexdigest(),
    })
    etag = await put_png(task["presigned_url"], image)
    material = await request(Route("POST", base + "/complete"), json={
        "upload_id": task["upload_id"], "md5": etag,
    })
    image_id = material["image_id"]
    try:
        width, height = size
        text = (f'<qqbot-at-user id="{escape(str(member), quote=True)}" />\n\n'
                f'![绘图 #{width}px #{height}px]({material["url"]})')
        keyboard = button_payload([
            [("按原设置重画", "/原设置重画 " + code), ("用我的预设重画", "/我的预设重画 " + code)],
            [("查看提示词", "/查看提示词 " + code)],
        ])
        result = await api.post_group_message(
            group_openid=group, msg_type=2, msg_id=message_id, msg_seq=2,
            markdown={"content": text}, keyboard=keyboard,
        )
        if result is None:
            raise ValueError("QQ 未确认图片卡片送达")
    finally:
        # QQ copies the image into the sent message. Release only this upload.
        try:
            await request(Route("DELETE", f"{base}/{quote(image_id, safe='')}/delete"))
        except Exception as exc:
            logging.getLogger(__name__).warning("QQ material cleanup failed (%s)", type(exc).__name__)
