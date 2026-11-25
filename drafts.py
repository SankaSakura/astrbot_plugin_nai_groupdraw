"""Small, on-disk drafts and per-image repeat instructions."""
import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass

from .menu import Draft


@dataclass(frozen=True)
class GroupMember:
    platform: str
    bot: str
    group: str
    user: str
    private: bool = False

    @property
    def draft_key(self):
        return json.dumps([self.platform, self.bot, self.group, self.user], ensure_ascii=False)

    # Match the two identifiers already stored on group-scoped image records.
    @property
    def history_owner(self):
        return json.dumps([self.platform, self.bot, self.user], ensure_ascii=False)

    @property
    def group_scope(self):
        return json.dumps([self.platform, self.bot, self.group], ensure_ascii=False)


class Drafts:
    def __init__(self, path):
        self.path = path
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS draft (owner TEXT PRIMARY KEY, data TEXT NOT NULL, epoch INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS recipe (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL)")
            if "group_scope" not in {col[1] for col in db.execute("PRAGMA table_info(recipe)")}:
                db.execute("ALTER TABLE recipe ADD COLUMN group_scope TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS by_owner ON recipe(owner)")
            db.execute("CREATE INDEX IF NOT EXISTS by_recipe_group ON recipe(group_scope)")
            if "private" not in {row[1] for row in db.execute("PRAGMA table_info(recipe)")}:
                db.execute("ALTER TABLE recipe ADD COLUMN private INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS history_epoch (group_scope TEXT PRIMARY KEY, epoch INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS private_history_epoch (owner TEXT PRIMARY KEY, epoch INTEGER NOT NULL)")

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def edit(self, member, direction=None, updates=None, replacement=None, replace_fields=None):
        with self.transaction() as db:
            draft, epoch = self.read(db, member)
            draft = replacement or draft
            if direction:
                draft.direction = direction
            if replace_fields is not None:
                draft.fields = replace_fields.copy()
            for key, value in (updates or {}).items():
                if not value:
                    draft.fields.pop(key, None)
                else:
                    draft.fields[key] = value
            self.store(db, member, draft, epoch)
            return draft, (epoch, self.group_epoch(db, member))

    @staticmethod
    def read(db, member):
        row = db.execute("SELECT data, epoch FROM draft WHERE owner=?", (member.draft_key,)).fetchone()
        return (Draft(**json.loads(row[0])), row[1]) if row else (Draft(), 0)

    @staticmethod
    def store(db, member, draft, epoch):
        db.execute("INSERT OR REPLACE INTO draft VALUES (?,?,?)",
                   (member.draft_key, json.dumps(asdict(draft), ensure_ascii=False), epoch))

    @staticmethod
    def group_epoch(db, member):
        row = db.execute("SELECT epoch FROM history_epoch WHERE group_scope=?", (member.group_scope,)).fetchone()
        epoch = row[0] if row else 0
        if member.private:
            personal = db.execute("SELECT epoch FROM private_history_epoch WHERE owner=?", (member.draft_key,)).fetchone()
            return epoch, personal[0] if personal else 0
        return epoch

    def clear_group_history(self, member):
        with self.transaction() as db:
            removed = db.execute("DELETE FROM recipe WHERE group_scope=?", (member.group_scope,)).rowcount
            # In-flight generations may still save their PNG, but cannot restore a deleted recipe.
            db.execute("INSERT INTO history_epoch(group_scope,epoch) VALUES (?,1) "
                       "ON CONFLICT(group_scope) DO UPDATE SET epoch=epoch+1", (member.group_scope,))
            return removed

    def clear_private_history(self, member):
        if not member.private:
            raise ValueError("请在私聊中清理个人私聊记录。")
        with self.transaction() as db:
            removed = db.execute("DELETE FROM recipe WHERE owner=? AND group_scope=? AND private=1",
                                 (member.history_owner, member.group_scope)).rowcount
            # A request already in flight must not recreate the cleared history.
            db.execute("INSERT INTO private_history_epoch VALUES (?,1) ON CONFLICT(owner) "
                       "DO UPDATE SET epoch=epoch+1", (member.draft_key,))
            return removed

    def remember(self, member, draft, epoch):
        with self.transaction() as db:
            row = db.execute("SELECT epoch FROM draft WHERE owner=?", (member.draft_key,)).fetchone()
            if not row or (row[0], self.group_epoch(db, member)) != epoch:
                return None
            code = secrets.token_hex(6)
            db.execute("INSERT INTO recipe (id,owner,data,group_scope,private) VALUES (?,?,?,?,?)",
                       (code, member.history_owner, json.dumps(asdict(draft), ensure_ascii=False), member.group_scope,member.private))
            return code

    def recipe(self, member, code, *, shared=False):
        with self.transaction() as db:
            if shared:
                row = db.execute("SELECT data FROM recipe WHERE id=? AND group_scope=? "
                                 "AND (private=0 OR (private=? AND owner=?))",
                                 (code, member.group_scope, member.private, member.history_owner)).fetchone()
            elif code:
                row = db.execute("SELECT data FROM recipe WHERE owner=? AND group_scope=? AND id=? AND private<=?",
                                 (member.history_owner, member.group_scope, code, member.private)).fetchone()
            else:
                row = db.execute("SELECT data FROM recipe WHERE owner=? AND group_scope=? AND private=? ORDER BY rowid DESC LIMIT 1",
                                 (member.history_owner, member.group_scope,member.private)).fetchone()
        if not row:
            if shared or code:
                raise ValueError("找不到这张图的出图记录。记录可能不存在或已被删除。")
            raise ValueError("找不到你最近一次的出图记录。你可以用当前草稿发送 /出图。")
        data = json.loads(row[0])
        if "fields" in data:
            return Draft(**data)
        # Earlier versions stored only the ordered characters sent to NovelAI.
        fields = {"画面正向": data["scene"], "画面负向": data["negative"]}
        for i, (positive, negative) in enumerate(data["characters"], 1):
            fields[f"角色{i}正向"] = positive
            fields[f"角色{i}负向"] = negative
        return Draft(data["direction"], fields)
