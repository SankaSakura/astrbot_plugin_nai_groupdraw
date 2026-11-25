import re
from dataclasses import dataclass, field
from html import escape

from .drawing import DIRECTIONS, Drawing

ENTRY = r"^[/／]?(群标识|图库权限|预设|画图|横图|竖图|草稿|出图|测试引用|原设置重画|我的预设重画|再出一张|再来一张|查看提示词|覆盖草稿|删除角色|清空图库|导出图库|清空提示词记录|清空草稿|清空|取消画图|清除记录)(?=\s|$)"
LABEL = r"(画面正向|画面负向|画面|正向|正面|负面|负向|反向|角色([1-6])(正向|正面|负面|负向|反向)?)"
FIELD = re.compile(LABEL + r"(?:[：:]\s*|\s+|$)([\s\S]*)")
MULTILINE_EDIT = re.compile(LABEL + r"[ \t]+")
HELP_GROUPS = {
    "绘图与个人设置": [
        ("/画图", "打开你的绘图草稿"),
        ("/测试引用", "回复本群的图片，验证能否读取参考图"),
        ("/出图", "用当前草稿出图，可直接附图或回复本群图片"),
        ("/预设", "选择画风和质量预设，或编辑本群预设"),
        ("/群标识", "查看本群标识和你的用户标识"),
        ("/图库权限", "查看你在本群的图库管理权限"),
        ("/清除记录", "清除你在本群的提示词和重画记录"),
    ],
    "DeepSeek 指令（DS 大小写均可）": [
        ("/ds整理", "修改个人草稿、开关预设或编辑本群预设。例如 /ds整理 在画面正向加上 solo 和 1girl"),
        ("/ds出图", "用自然语言画一张，可沿用草稿或指定预设。例如 /ds出图 沿用草稿角色1，画一张雨夜街景"),
    ],
    "管理员指令": [
        ("/导出图库", "将本群全部原图打包成 ZIP 发到群里"),
        ("/清空图库", "删除本群全部原图，保留提示词记录"),
        ("/清空提示词记录", "清除本群所有人的提示词和重画记录，保留原图和草稿"),
    ],
}


def help_text(markdown=False):
    sections = []
    for title, commands in HELP_GROUPS.items():
        lines = ["## " + title if markdown else title]
        if markdown:
            lines.append("")
        for command, explanation in commands:
            label = f"- `{command}`" if markdown else command
            lines.append(f"{label}　{explanation}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


HELP = help_text()
HELP_MARKDOWN = help_text(markdown=True)

def identifiers(value):
    if isinstance(value, (list, tuple)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set(re.findall(r"[^\s,，;；]+", str(value or "")))

def field_key(label, number, polarity):
    if number:
        return f"角色{number}" + ("负向" if polarity in ("负面","负向","反向") else "正向")
    return "画面负向" if label in ("负面","负向","反向","画面负向") else "画面正向"

def markdown_text(value):
    return re.sub(r"([\\`*_~\[\]|])", r"\\\1", escape(value, quote=False))


def tag_markdown(value, *, indent=""):
    """Keep prompt syntax literal inside QQ Markdown, including multiline prompts."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in value and "`" not in value:
        # Inline code strips one space from both ends when both are present.
        if value.startswith(" ") and value.endswith(" ") and value.strip(" "):
            value = " " + value + " "
        return "`" + value + "`"
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return "\n" + "\n".join(indent + line for line in [fence, *value.split("\n"), fence])

@dataclass
class Draft:
    direction: str = "竖图"
    fields: dict = field(default_factory=dict)
    # Populated on a generation snapshot only. Editable drafts keep their base text.
    presets: list = field(default_factory=list)
    original_request: dict | None = None

    def effective(self):
        fields = self.fields.copy()
        for key, side in (("画面正向", "positive"), ("画面负向", "negative")):
            value = fields.get(key, "")
            for preset in self.presets:
                addition = preset[side]
                if addition:
                    value = value.rstrip()
                    separator = " " if value.endswith((",", "，")) else ", "
                    value = value + separator + addition if value else addition
            if value:
                fields[key] = value
        return Draft(self.direction, fields)

    def history_text(self, presets, page, pages):
        text, md = self.text(f"草稿提示词（原图：{self.direction}）", preview=False)
        heading = "预设提示词（出图时）"
        if pages > 1:
            heading += f" · {page}/{pages}"
        lines = [heading]
        blocks = ["## " + heading]
        for preset in presets:
            label = f"{preset['key']} · {preset['name']}"
            lines.append(label)
            parts = ["**" + markdown_text(label) + "**"]
            for side, title in (("positive", "正向"), ("negative", "负向")):
                if preset[side]:
                    lines.append(title + "：" + preset[side])
                    parts.append("- " + title + "：" + tag_markdown(preset[side], indent="  "))
            blocks.append("\n".join(parts))
        if not presets:
            lines.append("无")
            blocks.append("无")
        return text + "\n\n" + "\n".join(lines), md + "\n\n" + "\n\n".join(blocks)

    def roles(self):
        return [i for i in range(1, 7) if any(
            self.fields.get(f"角色{i}{p}") for p in ("正向","负向"))]

    def drawing(self):
        return Drawing(self.direction, self.fields.get("画面正向", ""), self.fields.get("画面负向", ""),
                       [(self.fields.get(f"角色{i}正向",""), self.fields.get(f"角色{i}负向","")) for i in self.roles()])

    def text(self, title, *, preview, markdown_title=None):
        lines = [title]
        heading = "## " + (markdown_title or markdown_text(title))
        items = []
        for key in ["画面正向","画面负向"] + [f"角色{i}{p}" for i in self.roles() for p in ("正向","负向")]:
            value = self.fields.get(key, "")
            if preview:
                value = " ".join(value.split())
                if not value:
                    continue
                if len(value) > 36:
                    value = value[:35] + "…"
            else:
                value = value or "（空）"
            lines.append(f"{key}：{value}")
            continuation = tag_markdown(value, indent="  ")
            label = key + "：" if preview else f"**{key}：** "
            items.append(f"- {label}{continuation}")
        if not items:
            return title, heading
        return "\n".join(lines), heading + "\n\n" + "\n".join(items)

    def panel(self, display_name, *, mention_id=None, preview=True, page=1, edited_role=None):
        def edit(label):
            command = "负面" if label == "画面负向" else label
            return label.removeprefix("画面"), f"/画图 {command} {self.fields.get(label, '')}"
        first = [edit("画面正向"), edit("画面负向"), ("预设", "/预设")]
        roles = self.roles()
        vacant = next((i for i in range(1, 7) if i not in roles), None)
        if vacant is not None:
            first.append(("添加角色", f"/画图 角色{vacant}正向 "))
        if edited_role in roles:
            page = 1 + roles.index(edited_role) // 3
        start = 3 if page == 2 and len(roles) > 3 else 0
        rows = [first]
        for i in roles[start:start+3]:
            rows.append([edit(f"角色{i}正向"), edit(f"角色{i}负向"), ("删除", f"/删除角色 {i}")])
        target = "横图" if self.direction == "竖图" else "竖图"
        rows.append([(target, "/" + target),
                     ("出图", "/出图 参考模式：角色"), ("清空", "/清空草稿")])
        if len(roles) > 3:
            rows[-1].append(("上一页", "/草稿 1") if start else ("下一页", "/草稿 2"))
        suffix = f"的草稿：{self.direction}"
        title = (display_name or "你") + suffix
        md_title = None
        if not display_name and mention_id:
            md_title = f'<qqbot-at-user id="{escape(str(mention_id), quote=True)}" />' + suffix
        return (*self.text(title, preview=preview, markdown_title=md_title), rows)

def button_payload(rows):
    return {"content": {"rows": [{"buttons": [
        {"id": f"draw_{i}_{j}", "render_data": {"label": label, "visited_label": label, "style": 1},
         "action": {"type": 2, "permission": {"type": 2}, "data": command,
                    "enter": command.startswith("/查看提示词 ") or (command.startswith("/预设") and label not in ("新建预设", "编辑预设"))
                    or label in ("删除", "按原设置重画", "用我的预设重画", "查看提示词", "覆盖草稿", "上一页", "下一页"), "unsupport_tips": "请发送对应指令"}}
        for j, (label, command) in enumerate(row)]} for i, row in enumerate(rows)]}}

def parse_draft(body):
    """Read the existing one-line and multi-line shortcuts without renumbering roles."""
    direction = "竖图"
    parts = body.strip().split(maxsplit=1)
    if parts and parts[0] in DIRECTIONS:
        direction = DIRECTIONS[parts[0]]
        body = parts[1] if len(parts) > 1 else ""
    draft = Draft(direction)
    key = "画面正向"
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"^(画面正向|画面负向|画面|正向|正面|负面|负向|反向|角色\s*(\d+)\s*(正向|正面|负面|负向|反向)?)\s*[:：]\s*(.*)$", line)
        if match:
            label, number, polarity, value = match.groups()
            if number and number not in "1 2 3 4 5 6".split():
                raise ValueError("角色编号可用 1 到 6。")
            key = field_key(label, number, polarity)
            if key in draft.fields:
                raise ValueError(f"「{key}」重复，请合并。")
            draft.fields[key] = value.strip()
        else:
            if re.match(r"^角色\s*\d+\s*(正向|反向|负向)?(?:\s|$)",line):
                raise ValueError("多行指令请写成「角色1正向：提示词」。")
            draft.fields[key] = (draft.fields.get(key, "") + "\n" + line).strip()
    return draft

