"""Per-group rolling budgets for all generation entry points."""
import math
import sqlite3
import time
from contextlib import closing

from .menu import identifiers

WINDOW = 24 * 3600

def migrate_groups(config):
    legacy_groups = config.get("group_id")
    overrides = config.get("group_quotas", [])
    if not legacy_groups and not overrides:
        return
    if not isinstance(overrides, list):
        raise ValueError("旧版指定群上限格式异常，请检查插件配置。")
    groups = list(config.get("groups", []))
    for group in sorted(identifiers(legacy_groups)):
        if matching_groups(config, group):
            continue
        limits = [rule.get("limit") for rule in overrides if isinstance(rule, dict)
                  and str(rule.get("group_id", "")).strip() == group]
        # Retain duplicate overrides for correction. An invalid old cap must
        # never become an empty (inherited) cap and silently enable generation.
        caps = [str(limit).strip() or "0" for limit in limits]
        for cap in caps or [""]:
            groups.append({"__template_key": "group", "group_id": group, "nickname": "",
                           "limit": cap})
    config.update(groups=groups, group_id="", group_quotas=[])
    config.save_config()


def matching_groups(config, group):
    groups = config.get("groups", [])
    if not group or not isinstance(groups, list):
        return []
    return [rule for rule in groups if isinstance(rule, dict)
            and isinstance(rule.get("group_id"), str) and rule["group_id"].strip() == group]


def group_limit(config, group):
    matches = matching_groups(config, group)
    if not matches:
        raise ValueError("这个群尚未加入绘图白名单。")
    if len(matches) > 1:
        raise ValueError("本群有多条配置，请管理员在「群白名单」中只保留一条。")
    value = matches[0].get("limit", "")
    if isinstance(value, str) and not value.strip():
        value = config.get("max_images_24h", 100)
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or value < 0:
        raise ValueError("请管理员将本群的出图上限设为 0 或正整数。")
    return value

class Budget:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, stamp REAL NOT NULL, group_scope TEXT)")
            if "group_scope" not in {row[1] for row in db.execute("PRAGMA table_info(requests)")}:
                db.execute("ALTER TABLE requests ADD COLUMN group_scope TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS by_time ON requests(stamp)")
            db.execute("CREATE INDEX IF NOT EXISTS by_group_time ON requests(group_scope, stamp)")

    def reserve(self, key, limit, *, group_scope, now=None):
        now = time.time() if now is None else now
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM requests WHERE stamp <= ?", (now - WINDOW,))
            if db.execute("SELECT 1 FROM requests WHERE id=?", (key,)).fetchone():
                raise ValueError("这条请求已经提交过。")
            if limit <= 0:
                raise ValueError("管理员已暂停本群绘图（24 小时上限为 0）。")
            count = db.execute("SELECT COUNT(*) FROM requests WHERE group_scope=?", (group_scope,)).fetchone()[0]
            if count >= limit:
                stamp = db.execute("SELECT stamp FROM requests WHERE group_scope=? ORDER BY stamp LIMIT 1 OFFSET ?",
                                   (group_scope, count - limit)).fetchone()[0]
                minutes = max(1, math.ceil((stamp + WINDOW - now) / 60))
                raise ValueError(f"本群近 24 小时已使用 {count}/{limit} 个名额，约 {minutes} 分钟后再试。")
            db.execute("INSERT INTO requests(id,stamp,group_scope) VALUES (?,?,?)", (key, now, group_scope))

    def release(self, key):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM requests WHERE id=?", (key,))

    def complete(self, key):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE requests SET stamp=? WHERE id=?", (time.time(), key))
