import asyncio
import io
import os
import hashlib
import json
import sqlite3
import uuid
import re
import secrets
import shutil
import time
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools

from .drawing import DIRECTIONS, build_request
from .deepseek import DeepSeek
from .menu import Draft, ENTRY, FIELD, HELP, HELP_MARKDOWN, MULTILINE_EDIT, button_payload, field_key, identifiers, markdown_text, parse_draft

from .quota import Budget, group_limit, matching_groups, migrate_groups
from .drafts import Drafts, GroupMember
from .presets import Presets
from .references import MODES, image_urls, inspect_quote, raw_message, reference_parameters
from .qq_image import send_image_card
from .bindings import Bindings
from .gallery import Gallery
from .qq_gallery import field as message_field, qq_request, release_event_roles, require_group_admin, retain_event_roles, send_archive

class Rejected(ValueError):
    """Upstream confirmed it did not accept the request."""

ENDPOINT = "https://image.novelai.net/ai/generate-image"
DS_ENTRY = r"^[/／](?i:DS)(整理|出图)(?=\s|$)"
DS_CANDIDATE = r"^[/／]?(?i:DS)(?:整理|出图)(?=\s|$)"
MAX_BYTES = 16 * 1024 * 1024

def content_rejected(error):
    # botpy versions differ: some expose the QQ code, others only its message.
    return (str(getattr(error, "code", "")) == "40034006"
            or "40034006" in str(error) or "消息内容违规" in str(error))

async def generate(token, payload):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/zip"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=150)) as session:
        async with session.post(ENDPOINT, headers=headers, json=payload, allow_redirects=False) as response:
            if response.status not in (200, 201):
                messages = {401: "请管理员检查 NovelAI Token。", 402: "请检查账户额度。",
                            403: "请检查账户权限及订阅。", 429: "NovelAI 正忙，请稍后再试。"}
                error = Rejected if response.status in (400, 401, 402, 403, 404, 413, 422, 429) else ValueError
                raise error(f"NovelAI 返回 {response.status}。"
                                 + messages.get(response.status, "请检查提示词或稍后查看服务状态。"))
            archive = io.BytesIO()
            async for chunk in response.content.iter_chunked(65536):
                if archive.tell() + len(chunk) > MAX_BYTES:
                    raise ValueError("收到的图片数据过大。")
                archive.write(chunk)
    return unpack_images(archive)

def unpack_images(archive):
    images = []
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if entry.is_dir() or not entry.filename.lower().endswith(".png"):
                continue
            total += entry.file_size
            if total > MAX_BYTES:
                raise ValueError("NovelAI 返回的 PNG 总大小超过接收上限。")
            image = bundle.read(entry)
            if image.startswith(b"\x89PNG\r\n\x1a\n"):
                images.append(image)
    if images:
        return images
    raise ValueError("NovelAI 返回的数据中没有可发送的 PNG。")

class NovelAIGroupDraw(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        migrate_groups(config)
        self.lock = asyncio.Lock()
        self.seen = {}
        data = StarTools.get_data_dir("astrbot_plugin_nai_groupdraw")
        self.gallery_root = data / "gallery"
        self.gallery_root.mkdir(exist_ok=True, parents=True)
        self.exporting = None
        self.budget = Budget(data / "budget.sqlite3")
        self.drafts = Drafts(data / "drafts.sqlite3")
        self.bindings = Bindings(self.drafts.transaction)
        self.clear_requests = {}
        self.presets = Presets(self.drafts.transaction)
        self.deepseek = DeepSeek(self.drafts)
        self.group_user_model = retain_event_roles()

    async def terminate(self):
        release_event_roles(self.group_user_model)
        self.group_user_model = None

    async def show_draft(self, event, owner, draft, **options):
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            author = raw.get("d", raw).get("author") or {}
        else:
            author = getattr(raw, "author", None)
        def author_field(key):
            return author.get(key) if isinstance(author, dict) else getattr(author, key, None)

        user = str(event.get_sender_id())
        member = author_field("member_openid") or user
        get_name = getattr(event, "get_sender_name", None)
        name = None
        for candidate in (author_field("nickname"), author_field("username"), get_name() if get_name else None):
            if isinstance(candidate, str):
                candidate = " ".join(candidate.split())
                if (candidate and candidate not in (user, member, author_field("id"), author_field("union_openid"))
                        and candidate.lower() not in ("unknown", "unknown_user")
                        and not re.fullmatch(r"[a-fA-F0-9]{32}", candidate)):
                    name = candidate
                    break
        text, markdown, rows = draft.panel(name, mention_id=None if owner.private else member, **options)
        enabled = self.presets.snapshot(owner)
        if enabled:
            notice = "出图预设：" + " → ".join(p["name"] for p in enabled)
            text += "\n" + notice
            markdown += "\n\n" + markdown_text(notice)
        options = "、".join(f"`{mode}`" for mode in MODES)
        hint = f"附图时，在 `参考模式：` 后填一项：{options}。无图直接发送即可。"
        plain_hint = "参考模式可填：" + "、".join(MODES) + "。例如 /出图 参考模式：服装"
        await self.menu(event, text + "\n" + plain_hint, markdown + "\n\n" + hint, rows)

    async def menu(self, event, message, paragraphs, rows=None, *, draft_menu=True, all_commands=False):
        api = getattr(getattr(event, "bot", None), "api", None)
        group = event.get_group_id()
        dm = None if group else self.dm_id(event)
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        if api and mid and (group or dm) and (rows is None or self.config.get("buttons", True)):
            markdown = {"content": paragraphs}
            if self.config.get("markdown_id"):
                markdown = {"custom_template_id": self.config["markdown_id"], "params": [
                    {"key": self.config.get("markdown_key") or "text", "values": [paragraphs]}]}
            extra = {}
            if rows:
                template = self.config.get("keyboard_id") if draft_menu else None
                extra["keyboard"] = {"id": template} if template else button_payload(rows)
            try:
                payload = dict(msg_type=2, msg_id=mid, msg_seq=1, markdown=markdown, **extra)
                if dm:
                    await qq_request(api, "POST", f"/v2/users/{quote(dm, safe='')}/messages", json=payload)
                else:
                    await api.post_group_message(group_openid=group, **payload)
                return
            except Exception as error:
                logger.info(f"QQ menu fallback ({type(error).__name__})")
        if all_commands:
            message += "\n\n" + "\n".join(command for row in rows for _, command in row)
        elif not draft_menu:
            message += "\n\n" + rows[0][0][1]
        await event.send(event.plain_result(message))

    @filter.regex(r"(?i)^[/／]?help\s*$", priority=100)
    async def drawing_help(self, event: AstrMessageEvent):
        group = str(event.get_group_id() or "")
        if not group and not self.dm_id(event):
            return
        if event.message_str.strip().lower() == "help" and not event.is_at_or_wake_command:
            return
        event.should_call_llm(False)
        try:
            if str(event.get_sender_id()) not in identifiers(self.config.get("blocked_users")):
                await self.menu(event, HELP, HELP_MARKDOWN)
        finally:
            event.stop_event()

    @filter.regex(ENTRY + "|" + DS_CANDIDATE + r"|^\s*\[引用消息[\s\S]*(?:测试引用|出图|(?i:DS)整理)")
    async def draw(self, event: AstrMessageEvent):
        group, user = str(event.get_group_id() or ""), str(event.get_sender_id() or "")
        text = event.message_str.strip()
        quoted = text.startswith("[引用消息")
        if quoted or re.match(DS_CANDIDATE, text):
            # Only the sender's current command can trigger a generation. The rendered
            # outline includes quoted text, which can itself contain bot commands.
            current = message_field(raw_message(event), "content")
            if isinstance(current, str):
                text = re.sub(r"^(?:<@!?[^>]+>|<qqbot-at-user\b[^>]*?/>)\s*", "", current.strip())
            elif quoted:
                text = ""
            if quoted and not text:
                from astrbot.api.message_components import Plain
                text = "".join(p.text for p in event.message_obj.message if isinstance(p, Plain)).strip()
                if re.match(DS_ENTRY, text):
                    return
            if not re.match(DS_ENTRY, text) and not (quoted and re.match(r"^[/／]?(?:测试引用|出图)(?=\s|$)", text)):
                return
        ds = re.match(DS_ENTRY, text)
        match = ds or re.match(ENTRY, text)
        if not user or not match:
            return
        if not group and match.group(1) != "标识" and not self.dm_id(event):
            return
        event.should_call_llm(False)
        try:
            action = ("DS" if ds else "") + match.group(1)
            try:
                await self.handle(event, group, user, action, text[match.end():].strip())
            except ValueError as error:
                await event.send(event.plain_result(str(error)))
            except sqlite3.Error:
                await event.send(event.plain_result("绘图数据暂不可用，请稍后重试。"))
        finally:
            event.stop_event()

    @staticmethod
    def dm_id(event):
        if not getattr(event, "message_obj", None):
            return None
        value = message_field(message_field(raw_message(event), "author"), "user_openid")
        return value if isinstance(value, str) and value else None

    def group_name(self, group):
        rules = matching_groups(self.config, group)
        return (rules[0].get("nickname") if rules else None) or group

    @staticmethod
    def expiry(timestamp):
        return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M（北京时间）")

    def check_binding(self, binding, dm):
        owner = binding.owner
        blocked = identifiers(self.config.get("blocked_users"))
        if (not matching_groups(self.config, owner.group) or dm in blocked or owner.user in blocked or
                self.bindings.get(owner.platform, owner.bot, dm) != binding):
            raise ValueError("绑定已变化、到期或权限已撤销，请在目标群重新发送 /绑定群。")

    async def handle(self, event, group, user, action, body):
        say = lambda message: event.send(event.plain_result(message))
        dm = None if group else self.dm_id(event)
        blocked = identifiers(self.config.get("blocked_users"))
        if user in blocked or dm in blocked:
            return
        if action == "标识":
            if body:
                await event.send(event.plain_result("请单独发送 /标识。"))
                return
            if group:
                message = f"当前群标识 {group}"
            else:
                message = f"你的用户标识 {dm}" if dm else "未能读取你的 QQ 私聊标识。"
            await event.send(event.plain_result(message))
            return
        if action == "画图" and body == "身份":
            await event.send(event.plain_result("该指令已更名，请发送 /标识。"))
            return
        if action == "画图" and body in ("帮助", "help"):
            await self.menu(event, HELP, HELP_MARKDOWN)
            return
        origin = str(event.unified_msg_origin).split(":", 1)[0]
        bot = str(event.get_self_id() or "")
        mid = getattr(event.message_obj, "message_id", None)
        owner = GroupMember(origin, bot, group, user)
        binding = None
        if not group and not dm:
            raise ValueError("未能核实 QQ 私聊身份。")
        if action in ("绑定群", "绑定码", "解绑群", "当前绑定"):
            if not bot or not mid:
                raise ValueError("缺少机器人或消息标识，请重新发送。")
            command_id = json.dumps([origin,bot,group,dm or user,str(mid)])
            if action == "绑定群":
                if not group or not matching_groups(self.config, group):
                    raise ValueError("请在白名单群里 @机器人 发送 /绑定群。")
                raw = raw_message(event)
                author = message_field(raw,"author")
                member = message_field(author,"member_openid")
                if not member or message_field(raw,"group_openid") != group or user not in (member,message_field(author,"id")):
                    raise ValueError("未能核实本条 QQ 群消息的发送者。")
                if member in blocked:
                    return
                expires = self.bindings.bind(owner,member,body,command_id,blocked)
                await say(f"已绑定到「{self.group_name(group)}」，有效至 {self.expiry(expires)}。私聊出图计入该群额度。")
            elif group or body:
                raise ValueError(f"请私聊单独发送 /{action}。")
            elif action == "解绑群":
                self.bindings.unbind(origin,bot,dm,command_id)
                await say("已解除当前绑定。")
            elif action == "绑定码":
                code = self.bindings.pairing_code(origin,bot,dm,command_id)
                await say(f"请本人在目标群 @机器人发送 /绑定群 {code}，5 分钟内有效。")
            else:
                binding = self.bindings.get(origin,bot,dm)
                if binding:
                    self.check_binding(binding,dm)
                await say(f"当前绑定「{self.group_name(binding.owner.group)}」，有效至 {self.expiry(binding.expires)}。"
                          if binding else "当前没有有效绑定。请在目标群发送 /绑定群。")
            return
        if dm:
            binding = self.bindings.get(origin,bot,dm)
            if not binding:
                raise ValueError("请在目标群发送 /绑定群，有效期 48 小时。已绑定但仍提示时，私聊发送 /绑定码。")
            self.check_binding(binding,dm)
            owner = binding.owner
            group, user = owner.group, owner.user
            if action in ("图库权限","清空图库","导出图库","清空全群提示词记录"):
                raise ValueError("群管理操作请在绑定群中执行。")
        group_entries = matching_groups(self.config, group)
        if not group_entries:
            await event.send(event.plain_result("这个群尚未加入绘图白名单。"))
            return
        receipt = (owner.history_owner, group, str(mid or uuid.uuid4()))
        if dm:
            receipt = (origin, bot, "dm", dm, receipt[-1])
        now = time.monotonic()
        self.seen = {key: until for key, until in self.seen.items() if until > now}
        if receipt in self.seen:
            return
        self.seen[receipt] = now + 600
        try:
            if action in ("导出我的私聊图库","清空我的私聊图库","清空我的私聊提示词记录"):
                if not dm:
                    raise ValueError("请在与机器人的私聊中操作个人私聊记录。")
                if action != "清空我的私聊图库" and body:
                    raise ValueError(f"请单独发送 /{action}。")
                gallery = Gallery(self.gallery_root, owner.group_scope, owner.user)
                if action == "导出我的私聊图库":
                    api = getattr(getattr(event, "bot", None), "api", None)
                    if not api or not mid:
                        raise ValueError("未取得 QQ 私聊文件发送接口或消息标识，请重新发送。")
                    await self.export_gallery(event, api, owner, mid, gallery, binding=binding, dm=dm)
                elif action == "清空我的私聊提示词记录":
                    await say(f"已清除 {self.drafts.clear_private_history(owner)} 条个人私聊提示词和重画记录。")
                else:
                    await self.clear_private_gallery(event, binding, dm, gallery, body)
                return
            if action == "测试引用":
                quote = inspect_quote(event)
                if quote.problem:
                    result = f"已定位原消息，识别到 {len(quote.urls)} 个图片附件。" if quote.located else ""
                    await say(f"引用测试：{result}{quote.problem.rstrip('。')}。")
                else:
                    if self.lock.locked():
                        raise ValueError("机器人正在处理图片，请稍后再测试。")
                    async with self.lock:
                        await reference_parameters(quote.urls, "")
                    await say(f"引用测试通过：已读取原消息中的 {len(quote.urls)} 张图片。请再次回复原图发送 /出图 参考模式：服装。")
                return
            if action == "预设":
                await self.menu(event, *self.presets.command(owner, body), draft_menu=False, all_commands=True)
                return
            if action in ("图库权限", "导出图库", "清空图库", "清空全群提示词记录"):
                if body:
                    await say(f"请单独发送 /{action}。")
                    return
                api, permission = await require_group_admin(
                    event, group, user, group_entries, self.config.get("global_admins"))
                if action == "图库权限":
                    await say(permission + "，可以导出图库、清空图库和清空全群提示词记录。")
                    return
                if action == "清空全群提示词记录":
                    count = self.drafts.clear_group_history(owner)
                    await say(f"已清除本群 {count} 条提示词和重画记录。原图与个人草稿已保留。")
                    return
                gallery = Gallery(self.gallery_root, owner.group_scope)
                if action == "清空图库":
                    if self.exporting == owner.group_scope:
                        await say("本群图库正在导出，文件发送结束后可以清空。")
                        return
                    count = await asyncio.to_thread(gallery.clear)
                    await say(f"已清空本群图库，共删除 {count} 张原图。")
                else:
                    await self.export_gallery(event, api, owner, mid, gallery)
                return
            if action == "删除角色":
                if not re.fullmatch(r"[1-6]", body):
                    await say("请指定角色编号 1 到 6，例如 /删除角色 2。")
                    return
                draft, _ = self.drafts.edit(owner, updates={f"角色{body}正向": "", f"角色{body}负向": ""})
                page = 2 if sum(i < int(body) for i in draft.roles()) >= 3 else 1
                await self.show_draft(event, owner, draft, page=page)
                return
            if action in ("清空草稿", "清空", "取消画图"):
                draft, _ = self.drafts.edit(owner, replacement=Draft())
                await self.show_draft(event, owner, draft)
                return
            if action in ("查看提示词", "覆盖草稿"):
                parts = body.split()
                code = parts[0] if parts else ""
                snapshot = self.drafts.recipe(owner, code, shared=True)
                if action == "查看提示词":
                    panel = self.presets.history_panel(owner, code, snapshot, parts[1:])
                    await self.menu(event, *panel, draft_menu=False, all_commands=True)
                else:
                    if len(parts) != 1:
                        raise ValueError("请点击这张图的「覆盖草稿」。")
                    draft, _ = self.drafts.edit(owner, replace_fields=snapshot.fields)
                    await self.show_draft(event, owner, draft, preview=False)
                return
            original_settings = action in ("原设置重画", "再出一张", "再来一张")
            reference_body = body if action == "出图" else None
            key = hashlib.sha256(json.dumps(receipt).encode()).hexdigest()
            if action in ("DS整理", "DS出图"):
                if not self.config.get("deepseek_enabled", False):
                    raise ValueError("本插件尚未启用 DeepSeek。")
                if not mid:
                    raise ValueError("无法取得本条消息标识，请重新发送 DS 指令。")
                editing = action == "DS整理"
                prepared = await self.deepseek.prepare(self.config, owner, body, editing, key)
                if dm:
                    self.check_binding(binding,dm)
                if (not self.config.get("deepseek_enabled", False) or not matching_groups(self.config, group)
                        or user in identifiers(self.config.get("blocked_users"))):
                    raise ValueError("当前群或账号没有 DS 使用权限。")
                result = self.deepseek.execute(owner, body, editing, prepared)
                if editing:
                    await say(result)
                    return
                draft, epoch, reference_body = result
            elif original_settings or action == "我的预设重画":
                _, epoch = self.drafts.edit(owner)
                from_card = bool(body) and action in ("原设置重画", "我的预设重画")
                draft = self.drafts.recipe(owner, body, shared=from_card)
            elif action == "出图":
                draft, epoch = self.drafts.edit(owner)
            else:
                direction = DIRECTIONS.get(action) or (DIRECTIONS.get(body) if action == "画图" else None)
                field = FIELD.fullmatch(body) if action == "画图" and ("\n" not in body or MULTILINE_EDIT.match(body)) else None
                if action in ("横图","竖图","草稿") or direction or field or not body:
                    updates = {}
                    edited_role = None
                    if field:
                        label, number, polarity, value = field.groups()
                        if number:
                            edited_role = int(number)
                        name = field_key(label, number, polarity)
                        updates[name] = value.strip()
                    elif action in DIRECTIONS and body:
                        updates["画面正向"] = body
                    draft, _ = self.drafts.edit(owner, direction, updates)
                    page = 2 if action == "草稿" and body == "2" else 1
                    await self.show_draft(event, owner, draft, page=page, edited_role=edited_role)
                    return
                draft, epoch = self.drafts.edit(owner, replacement=parse_draft(body))
            if not original_settings and action != "DS出图":
                draft.presets = self.presets.snapshot(owner)
                draft.original_request = None
            images = await self.request_image(event, draft, key, owner, acknowledge=True,
                                              reference_body=reference_body, binding=binding, dm=dm)
            image = images[0]
            try:
                await asyncio.to_thread(Gallery(self.gallery_root, owner.group_scope, owner.user if dm else None).save, key, images)
            except (OSError, sqlite3.Error) as error:
                logger.error(f"groupdraw {key[:12]}: gallery save failed ({type(error).__name__})")
                try:
                    await say("原图未写入本群图库，请管理员检查 VPS 可用空间和目录权限。")
                except Exception:
                    logger.warning(f"groupdraw {key[:12]}: could not notify of gallery error")
            try:
                code = self.drafts.remember(owner, draft, epoch)
            except sqlite3.Error:
                logger.warning(f"groupdraw {key[:12]}: history write failed")
                code = None
            api = getattr(getattr(event, "bot", None), "api", None)
            raw = getattr(event.message_obj, "raw_message", None)
            if isinstance(raw, dict):
                payload = raw.get("d", raw)
                member = (payload.get("author") or {}).get("member_openid")
            else:
                member = getattr(getattr(raw, "author", None), "member_openid", None)
            if code and mid and api and self.config.get("buttons", True) and len(image) <= 10_000_000:
                try:
                    await send_image_card(api, group, member or user,
                                          mid, image, (draft.original_request["parameters"]["width"],
                                                       draft.original_request["parameters"]["height"]), code, **({"dm":dm} if dm else {}))
                    return
                except Exception as error:
                    if content_rejected(error):
                        await self.delivery_notice(event, api, group, mid, key, error)
                        return
                    logger.warning(f"groupdraw {key[:12]}: QQ card fallback ({type(error).__name__})")
            try:
                await event.send(event.chain_result([Image.fromBytes(image)]))
                if dm and code:
                    await say(f"查看提示词：/查看提示词 {code}\n重画：/原设置重画 {code}")
            except Exception as error:
                await self.delivery_notice(event, api, group, mid, key, error)
        except ValueError as error:
            await say(str(error))
        except (OSError, sqlite3.Error):
            await say("读写绘图数据失败，请检查 VPS 数据目录和磁盘空间。")



    async def clear_private_gallery(self, event, binding, dm, gallery, body):
        key = (binding.revision, dm)
        now = time.monotonic()
        self.clear_requests = {k:v for k,v in self.clear_requests.items() if v[0] > now}
        if not body:
            snapshot = await asyncio.to_thread(gallery.snapshot)
            self.check_binding(binding,dm)
            if not snapshot[0]:
                raise ValueError("你的私聊图库是空的。")
            token = secrets.token_hex(4)
            self.clear_requests[key] = (now+120,token,snapshot)
            text = (f"将删除你在「{self.group_name(binding.owner.group)}」的 {snapshot[0]} 张私聊原图。"
                    f"两分钟内发送 /清空我的私聊图库 确认 {token}")
        else:
            pending = self.clear_requests.get(key)
            if not pending or body != "确认 " + pending[1]:
                raise ValueError("确认无效，请重新发送 /清空我的私聊图库。")
            del self.clear_requests[key]
            count = await asyncio.to_thread(gallery.clear, pending[2], lambda: self.check_binding(binding,dm))
            text = f"已清空你的 {count} 张私聊原图。"
        await event.send(event.plain_result(text))

    async def export_gallery(self, event, api, owner, mid, gallery, *, binding=None, dm=None):
        if self.exporting is not None:
            raise ValueError("机器人正在导出图库，请稍后再试。")
        if not mid:
            raise ValueError("未取得群消息标识，请重新发送 /导出图库。")
        self.exporting = owner.group_scope
        try:
            output, count = await asyncio.to_thread(gallery.archive)
            with output:
                size = os.fstat(output.fileno()).st_size
                try:
                    if dm:
                        self.check_binding(binding,dm)
                        await send_archive(api, owner.group, mid, output, size, f"我的私聊图库-{count}张.zip",
                                           dm=dm, authorize=lambda: self.check_binding(binding,dm))
                    else:
                        await send_archive(api, owner.group, mid, output, size, f"本群图库-{count}张.zip")
                except Exception as error:
                    logger.warning(f"QQ gallery delivery failed ({type(error).__name__}; {size} bytes)")
                    if dm:
                        self.check_binding(binding,dm)
                        raise ValueError("QQ 未确认私聊文件送达，原图仍在你的私聊图库。") from error
                    raise ValueError(f"本群 {count} 张原图已打包（{size / 1048576:.1f} MiB），"
                                     "但 QQ 未确认文件送达。请检查群文件权限和单文件大小限制。原图仍在图库中。") from error
        finally:
            self.exporting = None

    async def delivery_notice(self, event, api, group, mid, key, error):
        rejected = content_rejected(error)
        reason = "QQ 内容审核未通过。" if rejected else "群消息发送失败。"
        text = "图片已生成，但未能送达：" + reason
        detail = "QQ content rejected" if rejected else type(error).__name__
        logger.warning(f"groupdraw {key[:12]}: delivery failed ({detail})")
        try:
            if event.get_group_id() and api and mid and hasattr(api, "post_group_message"):
                result = await api.post_group_message(group_openid=group, msg_id=mid, msg_seq=4,
                                                      msg_type=0, content=text)
                if result is None:
                    logger.warning(f"groupdraw {key[:12]}: failure notice unconfirmed")
            else:
                await event.send(event.plain_result(text))
        except Exception as notice_error:
            logger.warning(f"groupdraw {key[:12]}: failure notice failed ({type(notice_error).__name__})")

    async def request_image(self, event, draft, key, owner, acknowledge, reference_body=None, binding=None, dm=None):
        user, group, group_scope = owner.user, owner.group, owner.group_scope
        if dm:
            self.check_binding(binding,dm)
        if (not user or user in identifiers(self.config.get("blocked_users"))
                or not matching_groups(self.config, group)):
            raise ValueError("当前群或账号没有绘图权限。")
        bindings = str(self.config.get("member_tokens") or "").strip()
        if bindings:
            # Mappings contain environment variable names, not members' credentials.
            names = {}
            for item in bindings.splitlines():
                member, eq, name = item.partition("=")
                if eq and re.fullmatch(r"NOVELAI_TOKEN_[A-Za-z0-9_]+", name.strip()):
                    names[member.strip()] = name.strip()
            token = os.getenv(names[user], "").strip() if user in names else ""
            if not token:
                raise ValueError("请管理员在后台绑定你自己的 NovelAI Token。")
        else:
            token = (os.getenv("NOVELAI_TOKEN") or self.config.get("api_token") or "").strip()
        if not token:
            raise ValueError("请管理员先在插件后台设置 NovelAI Token。")
        if self.lock.locked():
            raise ValueError("上一张还在生成，请稍后点击提交。")
        async with self.lock:
            if shutil.disk_usage(self.gallery_root).free < MAX_BYTES + 128 * 1024 * 1024:
                raise ValueError("VPS 剩余空间不足，请先释放空间再出图。本次未请求 NovelAI。")
            try:
                limit = group_limit(self.config, group)
                self.budget.reserve(key, limit, group_scope=group_scope)
            except sqlite3.Error as error:
                raise ValueError("额度记录暂时不可用，请稍后再试。") from error
            try:
                payload = build_request(draft.effective().drawing(), draft.original_request)
                if reference_body is not None:
                    urls = image_urls(event)
                    source = "本条附图"
                    if not urls:
                        quote = inspect_quote(event)
                        if quote.explicit:
                            if quote.problem:
                                raise ValueError("引用图片不可用：" + quote.problem + "。本次未出图。")
                            urls = quote.urls
                            source = "引用图片"
                    payload["parameters"].update(await reference_parameters(urls, reference_body))
                draft.original_request = payload
                if acknowledge:
                    params = payload["parameters"]
                    notice = f"正在画{draft.direction} · V5 · {params['width']}×{params['height']} · {params['steps']} 步"
                    if reference_body is not None:
                        descriptions = params.get("director_reference_descriptions", [])
                        if descriptions:
                            mode = descriptions[0]["caption"]["base_caption"]
                            label = next(name for name, code in MODES.items() if code == mode)
                            notice += f" · {label}参考 {len(descriptions)} 张（{source}）"
                        else:
                            notice += " · 本条消息未收到参考图"
                    await event.send(event.plain_result(notice))
                if dm:
                    self.check_binding(binding,dm)
            except BaseException:
                self.budget.release(key)
                raise
            try:
                logger.info(f"groupdraw {key[:12]}: submitting to NovelAI")
                image = await generate(token, payload)
            except Rejected:
                self.budget.release(key)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, zipfile.BadZipFile) as error:
                raise ValueError("请求结果未知。已保留一个名额，请先检查账户再重发。") from error
            try:
                self.budget.complete(key)
            except sqlite3.Error:
                # The committed reservation still counts from the submission time.
                logger.warning(f"groupdraw {key[:12]}: quota timestamp update failed")
            logger.info(f"groupdraw {key[:12]}: received {len(image)} PNG(s), {sum(map(len, image))} bytes")
            return image
