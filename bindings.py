"""Group grants for QQ direct messages, scoped to one platform and bot."""
import secrets
import time
from dataclasses import dataclass

from .drafts import GroupMember


@dataclass(frozen=True)
class Binding:
    owner: GroupMember
    expires: int
    revision: str


class Bindings:
    def __init__(self, transaction):
        self.transaction = transaction
        with transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS dm_binding (platform TEXT, bot TEXT, dm TEXT, "
                       "grp TEXT, member TEXT, expires INTEGER, revision TEXT, PRIMARY KEY(platform,bot,dm), "
                       "UNIQUE(platform,bot,grp,member))")
            db.execute("CREATE TABLE IF NOT EXISTS dm_pair (platform TEXT, bot TEXT, dm TEXT, code TEXT UNIQUE, "
                       "expires INTEGER, PRIMARY KEY(platform,bot,dm))")
            db.execute("CREATE TABLE IF NOT EXISTS dm_command (receipt TEXT PRIMARY KEY, at INTEGER)")

    @staticmethod
    def once(db, receipt):
        now = int(time.time())
        db.execute("DELETE FROM dm_command WHERE at < ?", (now-172800,))
        if not db.execute("INSERT OR IGNORE INTO dm_command VALUES (?,?)", (receipt,now)).rowcount:
            raise ValueError("这条绑定操作已处理。")

    def pairing_code(self, platform, bot, dm, receipt):
        code = secrets.token_hex(8)
        with self.transaction() as db:
            self.once(db, receipt)
            db.execute("DELETE FROM dm_pair WHERE expires<=?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO dm_pair VALUES (?,?,?,?,?)",
                       (platform,bot,dm,code,int(time.time())+300))
        return code

    def bind(self, owner, member_openid, code, receipt, blocked):
        with self.transaction() as db:
            self.once(db, receipt)
            dm = member_openid
            if code:
                row = db.execute("SELECT dm FROM dm_pair WHERE platform=? AND bot=? AND code=? AND expires>?",
                                 (owner.platform,owner.bot,code,time.time())).fetchone()
                if not row:
                    raise ValueError("绑定码无效或已过期。")
                dm = row[0]
            else:
                previous = db.execute("SELECT dm FROM dm_binding WHERE platform=? AND bot=? AND grp=? AND member=?",
                                      (owner.platform,owner.bot,owner.group,owner.user)).fetchone()
                if previous:
                    dm = previous[0]
            if dm in blocked:
                raise ValueError("当前账号没有绘图权限。")
            db.execute("DELETE FROM dm_pair WHERE platform=? AND bot=? AND dm=?", (owner.platform,owner.bot,dm))
            expires = int(time.time()) + 48*3600
            db.execute("INSERT OR REPLACE INTO dm_binding VALUES (?,?,?,?,?,?,?)",
                       (owner.platform,owner.bot,dm,owner.group,owner.user,expires,secrets.token_hex(12)))
        return expires

    def get(self, platform, bot, dm):
        with self.transaction() as db:
            row = db.execute("SELECT grp,member,expires,revision FROM dm_binding "
                             "WHERE platform=? AND bot=? AND dm=? AND expires>?",
                             (platform,bot,dm,time.time())).fetchone()
        if row:
            return Binding(GroupMember(platform,bot,row[0],row[1],private=True),row[2],row[3])

    def unbind(self, platform, bot, dm, receipt):
        with self.transaction() as db:
            self.once(db, receipt)
            db.execute("DELETE FROM dm_pair WHERE platform=? AND bot=? AND dm=?", (platform,bot,dm))
            db.execute("DELETE FROM dm_binding WHERE platform=? AND bot=? AND dm=?", (platform,bot,dm))
