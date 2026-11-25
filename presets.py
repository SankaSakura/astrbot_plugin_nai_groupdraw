"""Group preset catalog, member selections, and QQ commands."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace

from .menu import markdown_text, tag_markdown


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    positive: str
    negative: str


class Catalog(dict):
    """Display each preset once; resolve historical global numbers in this group only."""

    def __init__(self):
        super().__init__(OFFICIAL)
        self.aliases = {}

    def canonical(self, key):
        key = key.upper()
        return self.aliases.get(key, key)


HEAVY = ("lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, "
         "very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, "
         "too many watermarks, negative space, blank page")
OFFICIAL = {p.key: p for p in (
    Preset("N1", "正向轻量", "very aesthetic, amazing quality, no text,", ""),
    Preset("N2", "正向标准", "very aesthetic, masterpiece, no text,", ""),
    Preset("N3", "反向轻量", "", "lowres, bad hands, bad anatomy, artistic error, sepia, white haze, "
           "worst quality, very displeasing, jpeg artifacts, 0::ai-generated::,"),
    Preset("N4", "反向重度", "", HEAVY + ","),
    Preset("N5", "人物向", "", HEAVY + ", @_@, mismatched pupils, glowing eyes, bad anatomy,"),
    Preset("N6", "兽人向", "", "{worst quality}, distracting watermark, unfinished, bad quality, {widescreen}, "
           "upscale, {sequence}, {{grandfathered content}}, blurred foreground, chromatic aberration, sketch, "
           "everyone, [sketch background], simple, [flat colors], ych (character), outline, multiple scenes, "
           "[[horror (theme)]], comic,"),
)}


def parse_fields(lines):
    fields = {}
    current = None
    for line in lines:
        match = re.match(r"^\s*(昵称|名称|(?:画面)?正向|(?:画面)?[负反]向)\s*[:：](.*)$", line)
        if match:
            label, value = match.groups()
            current = "name" if label in ("昵称", "名称") else ("positive" if "正" in label else "negative")
            if current in fields:
                raise ValueError("同一栏出现了两次，请合并后再发送。")
            fields[current] = value
        elif current in ("positive", "negative"):
            fields[current] += "\n" + line
        elif line.strip():
            raise ValueError("请另起一行填写「正向：…」或「反向：…」。")
    return {k: v.strip() for k, v in fields.items()}


class Presets:
    def __init__(self, transaction):
        self.transaction = transaction
        with transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS group_preset (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, "
                       "name TEXT NOT NULL, positive TEXT NOT NULL, negative TEXT NOT NULL, UNIQUE(scope,name))")
            db.execute("CREATE TABLE IF NOT EXISTS preset_selection (owner TEXT PRIMARY KEY, keys TEXT NOT NULL)")
            if "local_no" not in {row[1] for row in db.execute("PRAGMA table_info(group_preset)")}:
                db.execute("ALTER TABLE group_preset ADD COLUMN local_no INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE group_preset ADD COLUMN legacy_no INTEGER")
                numbers = {}
                for row_id, scope in db.execute("SELECT id,scope FROM group_preset ORDER BY id").fetchall():
                    numbers[scope] = numbers.get(scope, 0) + 1
                    db.execute("UPDATE group_preset SET local_no=?,legacy_no=id WHERE id=?", (numbers[scope], row_id))
                db.execute("CREATE UNIQUE INDEX group_preset_local_no ON group_preset(scope,local_no)")

    @staticmethod
    def catalog(db, member):
        presets = Catalog()
        for number, old, name, pos, neg in db.execute(
                "SELECT local_no,legacy_no,name,positive,negative FROM group_preset WHERE scope=? ORDER BY local_no",
                (member.group_scope,)):
            key = f"P{number}"
            presets[key] = Preset(key, name, pos, neg)
            if old is not None:
                presets.aliases[f"G{old}"] = key
        return presets

    @staticmethod
    def selection(db, member, catalog=None):
        row = db.execute("SELECT keys FROM preset_selection WHERE owner=?", (member.draft_key,)).fetchone()
        if row is None:
            return []
        if catalog is None:
            catalog = Presets.catalog(db, member)
        return [catalog.canonical(key) for key in json.loads(row[0])]

    def snapshot(self, member):
        with self.transaction() as db:
            catalog = self.catalog(db, member)
            keys = self.selection(db, member, catalog)
            return [asdict(catalog[key]) for key in keys]

    def history_panel(self, member, code, snapshot, options):
        # Cards are visible to the group. A different reader's first tap opens
        # their own controls before they make a choice.
        viewer = hashlib.sha256(member.draft_key.encode()).hexdigest()[:16]
        if options and (len(options) not in (1, 4) or not re.fullmatch(r"[1-9][0-9]{0,8}", options[0])):
            raise ValueError("请重新点击原图的「查看提示词」。")
        page = int(options[0]) if options else 1
        per_page = 4
        pages = max(1, (len(snapshot.presets) + per_page - 1) // per_page)
        if page > pages:
            raise ValueError("这张图没有这一页预设。")
        presets = snapshot.presets[(page-1)*per_page:page*per_page]
        command = "/查看提示词 " + code
        nav = [("覆盖草稿", "/覆盖草稿 " + code)]
        if page > 1:
            nav.append(("上一页", f"{command} {page-1}"))
        if page < pages:
            nav.append(("下一页", f"{command} {page+1}"))
        switches = []
        with self.transaction() as db:
            catalog = self.catalog(db, member)
            selected = self.selection(db, member, catalog)
            presets = [dict(p, key=catalog.canonical(p["key"])) for p in presets]
            text, md = snapshot.history_text(presets, page, pages)
            if len(options) == 4:
                _, action, key, card_viewer = options
                key = catalog.canonical(key)
                if action not in ("开启", "关闭") or key not in {p["key"] for p in presets} or key not in catalog:
                    raise ValueError("这项预设不可用，请重新查看提示词。")
                if card_viewer == viewer:
                    verb = "启用" if action == "开启" else "停用"
                    selected = self.update_selection(db, member, selected, verb, [key])
            for preset in presets:
                key = preset["key"]
                if key in catalog:
                    action = "关闭" if key in selected else "开启"
                    switches.append((action + " " + key, f"{command} {page} {action} {key} {viewer}"))
                    current = catalog[key]
                    if asdict(current) != preset:
                        note = f"{key} 已更新，开启时使用本群当前版本「{current.name}」。"
                        text += "\n" + note
                        md += "\n\n" + markdown_text(note)
        rows = [switches[i:i + 3] for i in range(0, len(switches), 3)]
        return text, md, rows + [nav]

    def command(self, member, body):
        lines = body.splitlines() or [""]
        verb, *rest = lines[0].split(maxsplit=1) or [""]
        arg = rest[0].strip() if rest else ""
        with self.transaction() as db:
            catalog = self.catalog(db, member)
            selected = self.selection(db, member, catalog)
            page = 1
            if verb in ("新建", "修改"):
                changes = parse_fields(lines[1:])
                preset = self.save_group_preset(db, member, catalog, arg if verb == "修改" else None, arg, changes)
                return self.detail(preset, selected)
            if len(lines) > 1:
                raise ValueError("请每次发送一条预设指令。")
            if verb == "详情":
                return self.detail(self.lookup(catalog, arg), selected)
            if verb in ("启用", "停用", "全关"):
                keys = [self.lookup(catalog, k).key for k in re.split(r"[\s,，]+", arg) if k]
                if (verb == "全关" and keys) or (verb != "全关" and not keys):
                    raise ValueError("用法：/预设 启用 N2 N5、/预设 停用 N5 或 /预设 全关。")
                selected = self.update_selection(db, member, selected, verb, keys)
                if keys:
                    page = list(catalog).index(keys[-1]) // 4 + 1
            elif verb:
                if arg or not re.fullmatch(r"[1-9][0-9]{0,8}", verb):
                    raise ValueError("发送 /预设 打开列表，或用 /预设 详情 编号 查看内容。")
                page = int(verb)
            return self.panel(catalog, selected, page)

    @staticmethod
    def save_group_preset(db, member, catalog, key, name, changes):
        if key is None:
            preset = Preset("", name, "", "")
        else:
            preset = Presets.lookup(catalog, key)
            if preset.key in OFFICIAL:
                raise ValueError("官方预设只读。可用 /预设 新建 另存为本群预设。")
        preset = replace(preset, **changes)
        if not preset.name or not (preset.positive or preset.negative):
            raise ValueError("请填写预设昵称，以及至少一栏正向或反向提示词。")
        if any(p.name == preset.name and p.key != preset.key for p in catalog.values()):
            raise ValueError("本群已有同名预设，请换一个昵称。")
        if preset.key:
            db.execute("UPDATE group_preset SET name=?,positive=?,negative=? WHERE scope=? AND local_no=?",
                       (preset.name, preset.positive, preset.negative, member.group_scope, preset.key[1:]))
        else:
            number, = db.execute("SELECT COALESCE(MAX(local_no),0)+1 FROM group_preset WHERE scope=?",
                                 (member.group_scope,)).fetchone()
            db.execute("INSERT INTO group_preset(scope,local_no,name,positive,negative) VALUES (?,?,?,?,?)",
                       (member.group_scope, number, preset.name, preset.positive, preset.negative))
            preset = replace(preset, key=f"P{number}")
        return preset

    @staticmethod
    def update_selection(db, member, selected, verb, keys):
        if verb == "启用":
            selected = list(dict.fromkeys(selected + keys))
        elif verb == "停用":
            selected = [key for key in selected if key not in keys]
        elif verb == "全关":
            selected = []
        else:
            raise ValueError("未知预设操作。")
        db.execute("INSERT OR REPLACE INTO preset_selection VALUES (?,?)",
                   (member.draft_key, json.dumps(selected)))
        return selected

    @staticmethod
    def lookup(catalog, key):
        preset = catalog.get(catalog.canonical(key))
        if not preset:
            raise ValueError("本群没有这个预设编号。发送 /预设 查看列表。")
        return preset

    @staticmethod
    def detail(preset, selected):
        enabled = preset.key in selected
        action = "停用" if enabled else "启用"
        title = f"{preset.key} · {preset.name}"
        status = f"你在本群：{'已启用' if enabled else '未启用'}"
        plain = [title]
        md = [markdown_text(title)]
        for label, value in (("正向", preset.positive), ("反向", preset.negative)):
            plain.append(label + "：" + (value or "（空）"))
            md.append(label + "：" + (tag_markdown(value) if value else "（空）"))
        row = [(action, f"/预设 {action} {preset.key}"), ("预设列表", "/预设")]
        if preset.key not in OFFICIAL:
            row.append(("编辑预设", f"/预设 修改 {preset.key}\n昵称：{preset.name}\n"
                        f"正向：{preset.positive}\n反向：{preset.negative}"))
        return "\n".join(plain + [status]), "\n\n".join(md + [status]), [row]

    @staticmethod
    def panel(catalog, selected, page):
        total = (len(catalog) + 3) // 4
        if page > total:
            raise ValueError(f"预设只有 {total} 页。")
        lines = [f"本群预设 · {page}/{total}", "我的拼接顺序：" +
                 (" → ".join(catalog[k].name for k in selected) or "未启用")]
        rows = []
        for p in list(catalog.values())[(page-1)*4:page*4]:
            action = "停用" if p.key in selected else "启用"
            lines.append(f"{p.key} · {p.name}（{'官方' if p.key in OFFICIAL else '本群'}）")
            rows.append([(action + " " + p.key, f"/预设 {action} {p.key}"),
                         ("详情 " + p.key, f"/预设 详情 {p.key}")])
        nav = [("新建预设", "/预设 新建 我的画风\n正向：\n反向："), ("全关", "/预设 全关")]
        if page > 1:
            nav.append(("上一页", f"/预设 {page-1}"))
        if page < total:
            nav.append(("下一页", f"/预设 {page+1}"))
        rows.append(nav)
        text = "\n".join(lines)
        return text, markdown_text(text), rows
