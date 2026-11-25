"""QQ group role checks and passive file replies using botpy's authenticated client."""
import asyncio
import base64
import hashlib
from urllib.parse import quote, urlsplit

import aiohttp
from astrbot.api import logger

from .menu import identifiers


def field(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def retain_event_roles():
    """Keep QQ's author role when older botpy models discard it during parsing."""
    try:
        from botpy.message import GroupMessage
        original = GroupMessage._User
        if not isinstance(original, type):
            return None
        if "_groupdraw_users" in vars(original):
            original._groupdraw_users += 1
            return original
        probe = {"id": "probe", "member_openid": "probe", "member_role": "owner"}
        if field(original(probe), "member_role") == "owner":
            return None
    except (ImportError, AttributeError, TypeError, KeyError):
        return None

    class GroupUserWithRole(original):
        _groupdraw_users = 1

        def __init__(self, data, *args, **kwargs):
            super().__init__(data, *args, **kwargs)
            self.member_role = field(data, "member_role")

    GroupMessage._User = GroupUserWithRole
    return GroupUserWithRole


def release_event_roles(model):
    if model is None:
        return
    from botpy.message import GroupMessage
    model._groupdraw_users -= 1
    if not model._groupdraw_users and GroupMessage._User is model:
        GroupMessage._User = model.__bases__[0]


async def qq_request(api, method, path, **kwargs):
    from botpy.http import Route
    return await api._http.request(Route(method, path), **kwargs)


async def require_group_admin(event, group, user, group_entries, global_admins=None):
    raw = getattr(event.message_obj, "raw_message", None)
    payload = field(raw, "d") or raw
    author = field(payload, "author")
    api = getattr(getattr(event, "bot", None), "api", None)
    member_id = field(author, "member_openid") or field(author, "id")
    if (not api or field(payload, "group_openid") != group
            or not member_id or user not in (member_id, field(author, "id"))):
        raise ValueError("无法核实你在本群的 QQ 身份，操作未执行。")
    if user in identifiers(global_admins):
        return api, "你是插件总管理员"
    role = field(author, "member_role")
    lookup_note = "QQ 未返回可识别的群角色"
    if role not in ("owner", "admin", "member"):
        path = f"/v2/groups/{quote(group, safe='')}/members/{quote(member_id, safe='')}"
        try:
            member = await qq_request(api, "GET", path)
        except Exception as error:
            code = str(getattr(error, "code", ""))
            lookup_note = "QQ 群成员查询失败"
            if code.isascii() and code.isdecimal() and len(code) <= 10:
                lookup_note += f"（错误码 {code}）"
            logger.info(f"{lookup_note} ({type(error).__name__})")
        else:
            if field(member, "member_openid") not in (None, member_id):
                raise ValueError("QQ 返回的成员身份与发送者不符，操作未执行。")
            role = field(member, "member_role")
    if role in ("owner", "admin"):
        return api, "QQ 已识别你为本群" + ("群主" if role == "owner" else "管理员")
    if role == "member":
        raise ValueError("只有插件总管理员、本群群主或管理员可以执行此操作。")
    if len(group_entries) == 1 and user in identifiers(group_entries[0].get("gallery_admins")):
        return api, "你已获得本群手动授权的图库管理权限"
    raise ValueError(f"{lookup_note}，操作未执行。可私聊机器人发送 /标识，再请机器人管理员将你的用户标识填入本群的「图库管理员」并保存。")


def file_hashes(file):
    md5, sha1, prefix = hashlib.md5(), hashlib.sha1(), hashlib.md5()
    remaining = 10002432
    while block := file.read(1024 * 1024):
        md5.update(block)
        sha1.update(block)
        if remaining:
            part = block[:remaining]
            prefix.update(part)
            remaining -= len(part)
    file.seek(0)
    return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "md5_10m": prefix.hexdigest()}


async def chunk_upload(api, path, file, size, name):
    hashes = await asyncio.to_thread(file_hashes, file)
    plan = await qq_request(api, "POST", path + "/upload_prepare", json={
        "file_type": 4, "file_name": name, "file_size": str(size), **hashes})
    if plan.get("file_info"):
        return plan
    upload = plan["upload_id"]
    parts = sorted(plan.get("parts") or [], key=lambda p: int(p.get("index", p.get("part_index"))))
    if not parts:
        raise ValueError("QQ 未返回文件上传分片。")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for part in parts:
            url = part.get("presigned_url") or part.get("upload_url")
            if not url or urlsplit(url).scheme != "https":
                raise ValueError("QQ 未返回有效的 HTTPS 上传地址。")
            index = int(part.get("index", part.get("part_index")))
            length = min(int(part.get("block_size") or plan["block_size"]), size - file.tell())
            if length <= 0:
                raise ValueError("QQ 返回的分片大小无效。")
            # Stream this part without allocating the whole ZIP or even a
            # whole platform-sized part in the VPS process.
            checksum = hashlib.md5()
            async def blocks():
                remaining = length
                while remaining:
                    block = await asyncio.to_thread(file.read, min(remaining, 256 * 1024))
                    if not block:
                        raise OSError("ZIP 提前结束")
                    checksum.update(block)
                    remaining -= len(block)
                    yield block
            async with session.put(url, data=blocks(), headers={"Content-Length": str(length)},
                                   allow_redirects=False) as response:
                response.raise_for_status()
                if response.status // 100 != 2:
                    raise ValueError("QQ 文件分片上传失败。")
            await qq_request(api, "POST", path + "/upload_part_finish", json={
                "upload_id": upload, "part_index": index, "block_size": str(length),
                "md5": checksum.hexdigest()})
    if file.tell() != size:
        raise ValueError("QQ 返回的分片未覆盖整个 ZIP。")
    return await qq_request(api, "POST", path + "/files", json={
        "upload_id": upload, "file_type": 4, "file_name": name, "srv_send_msg": False})


async def send_archive(api, group, mid, file, size, name, *, dm=None, authorize=None):
    path = f"/v2/users/{quote(dm, safe='')}" if dm else f"/v2/groups/{quote(group, safe='')}"
    if size <= 5 * 1024 * 1024:
        encoded = base64.b64encode(file.read()).decode("ascii")
        result = await qq_request(api, "POST", path + "/files", json={
            "file_type": 4, "file_name": name, "file_data": encoded, "srv_send_msg": False})
    else:
        result = await chunk_upload(api, path, file, size, name)
    info = result.get("file_info")
    if not info:
        raise ValueError("QQ 未返回文件凭据。")
    if dm:
        authorize()
        sent = await qq_request(api, "POST", path + "/messages", json={
            "msg_id": mid, "msg_seq": 1, "msg_type": 7, "content": " ", "media": {"file_info": info}})
    else:
        sent = await api.post_group_message(group_openid=group, msg_id=mid, msg_seq=1,
                                            msg_type=7, media={"file_info": info})
    if not sent or not field(sent, "id"):
        raise ValueError("QQ 未确认文件消息。")
