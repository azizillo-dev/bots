"""Telegram dormitory rubbish rota bot. Python 3.11+, standard library only."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger("navbatchi")


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


class TelegramError(Exception):
    def __init__(self, description: str, retry_after: int = 0):
        super().__init__(description)
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params):
        body = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(self.base + method, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=int(params.get("timeout", 0)) + 15) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.load(exc)
            except (ValueError, OSError):
                raise TelegramError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TelegramError(str(exc)) from exc
        if not result.get("ok"):
            raise TelegramError(result.get("description", "Telegram xatosi"), result.get("parameters", {}).get("retry_after", 0))
        return result["result"]


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT,
                private_started INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS floors (
                id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL UNIQUE, title TEXT NOT NULL,
                remind_time TEXT NOT NULL, rooms TEXT NOT NULL,
                next_position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS group_members (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (chat_id, user_id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS assignments (
                floor_id INTEGER NOT NULL, user_id INTEGER NOT NULL, room TEXT NOT NULL,
                PRIMARY KEY (floor_id, user_id),
                FOREIGN KEY(floor_id) REFERENCES floors(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS join_requests (
                floor_id INTEGER NOT NULL, user_id INTEGER NOT NULL, room TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                PRIMARY KEY (floor_id, user_id),
                FOREIGN KEY(floor_id) REFERENCES floors(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS floor_admins (
                floor_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                PRIMARY KEY(floor_id, user_id),
                FOREIGN KEY(floor_id) REFERENCES floors(id)
            );
            CREATE TABLE IF NOT EXISTS duties (
                id INTEGER PRIMARY KEY, floor_id INTEGER NOT NULL, duty_date TEXT NOT NULL,
                room TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                reminded INTEGER NOT NULL DEFAULT 0, completed_by INTEGER,
                completed_at TEXT, group_message_id INTEGER,
                UNIQUE(floor_id, duty_date),
                FOREIGN KEY(floor_id) REFERENCES floors(id)
            );
        """)
        self.db.commit()

    def one(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchall()

    def execute(self, sql: str, args=()):
        result = self.db.execute(sql, args)
        self.db.commit()
        return result

    def user(self, user: dict, private=False):
        if not user or user.get("is_bot"):
            return
        uid = user["id"]
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip() or str(uid)
        self.execute("""INSERT INTO users(id,name,username,private_started) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username,
            private_started=MAX(users.private_started,excluded.private_started)""",
            (uid, name, user.get("username"), int(private)))

    def member(self, chat_id: int, user: dict, active=True):
        if not user or user.get("is_bot"):
            return
        self.user(user)
        self.execute("""INSERT INTO group_members(chat_id,user_id,active) VALUES(?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET active=excluded.active""",
            (chat_id, user["id"], int(active)))

    def setup(self, number: str, chat_id: int, title: str, remind_time: str):
        existing = self.one("SELECT * FROM floors WHERE number=? OR chat_id=?", (number, chat_id))
        if existing:
            if existing["number"] != number or existing["chat_id"] != chat_id:
                raise ValueError("Bu qavat yoki guruh allaqachon boshqa joyga ulangan.")
            self.execute("UPDATE floors SET title=? WHERE id=?", (title, existing["id"]))
            return existing["id"]
        rooms = [f"{number}{n:02d}" for n in range(1, 13)]
        return self.execute("INSERT INTO floors(number,chat_id,title,remind_time,rooms) VALUES(?,?,?,?,?)",
                            (number, chat_id, title, remind_time, json.dumps(rooms))).lastrowid

    def current_duty(self, floor_id: int):
        return self.one("SELECT * FROM duties WHERE floor_id=? ORDER BY duty_date DESC LIMIT 1", (floor_id,))

    def advance(self, floor_id: int):
        floor = self.one("SELECT * FROM floors WHERE id=?", (floor_id,))
        rooms = json.loads(floor["rooms"])
        self.execute("UPDATE floors SET next_position=? WHERE id=?", ((floor["next_position"] + 1) % len(rooms), floor_id))

    def prepare_duty(self, floor_id: int, today: str):
        """Create each elapsed day, recording unfinished days as missed."""
        latest = self.current_duty(floor_id)
        date = datetime.fromisoformat(latest["duty_date"]).date() + timedelta(days=1) if latest else datetime.fromisoformat(today).date()
        end = datetime.fromisoformat(today).date()
        while date <= end:
            if latest and latest["status"] == "pending":
                self.execute("UPDATE duties SET status='missed' WHERE id=?", (latest["id"],))
                self.advance(floor_id)
            floor = self.one("SELECT * FROM floors WHERE id=?", (floor_id,))
            room = json.loads(floor["rooms"])[floor["next_position"]]
            duty_id = self.execute("INSERT INTO duties(floor_id,duty_date,room) VALUES(?,?,?)",
                                   (floor_id, date.isoformat(), room)).lastrowid
            latest = self.one("SELECT * FROM duties WHERE id=?", (duty_id,))
            date += timedelta(days=1)
        return latest

    def complete(self, duty_id: int, uid: int, now: datetime):
        duty = self.one("SELECT * FROM duties WHERE id=?", (duty_id,))
        if not duty or duty["status"] != "pending" or duty["duty_date"] != now.date().isoformat():
            raise ValueError("Bu navbat hozir faol emas.")
        self.execute("UPDATE duties SET status='done',completed_by=?,completed_at=? WHERE id=?",
                     (uid, now.isoformat(), duty_id))
        self.advance(duty["floor_id"])
        return duty


class Bot:
    def __init__(self, store: Store, api: Telegram, superadmins: set[int], zone: ZoneInfo, default_time: str):
        self.s = store
        self.api = api
        self.superadmins = superadmins
        self.zone = zone
        self.default_time = default_time
        identity = self.api.call("getMe")
        self.username = identity["username"]
        self.bot_id = identity["id"]
        self.broadcast_drafts: dict[int, tuple[str, str]] = {}

    def send(self, chat_id: int, text: str, markup=None, html_mode=False):
        args = {"chat_id": chat_id, "text": text}
        if markup:
            args["reply_markup"] = markup
        if html_mode:
            args["parse_mode"] = "HTML"
        return self.api.call("sendMessage", **args)

    def answer(self, query_id: str, text: str = "", alert=False):
        try:
            self.api.call("answerCallbackQuery", callback_query_id=query_id, text=text[:200], show_alert=alert)
        except TelegramError:
            LOG.exception("Callback answer failed")

    def is_super(self, uid: int) -> bool:
        return uid in self.superadmins or bool(self.s.one("SELECT 1 FROM admins WHERE user_id=?", (uid,)))

    def is_chat_admin(self, chat_id: int, uid: int) -> bool:
        if self.is_super(uid):
            return True
        try:
            return self.api.call("getChatMember", chat_id=chat_id, user_id=uid)["status"] in ("creator", "administrator")
        except TelegramError:
            return False

    def is_floor_admin(self, floor, uid: int) -> bool:
        return self.is_super(uid) or bool(self.s.one("SELECT 1 FROM floor_admins WHERE floor_id=? AND user_id=?", (floor["id"], uid))) or self.is_chat_admin(floor["chat_id"], uid)

    def accessible_floors(self, uid: int):
        floors = self.s.all("SELECT * FROM floors ORDER BY CAST(number AS INTEGER)")
        return floors if self.is_super(uid) else [f for f in floors if self.is_floor_admin(f, uid)]

    def status_text(self, floor) -> str:
        duty = self.s.current_duty(floor["id"])
        members = self.s.one("SELECT COUNT(*) AS n FROM group_members WHERE chat_id=? AND active=1", (floor["chat_id"],))["n"]
        assigned = self.s.one("SELECT COUNT(*) AS n FROM assignments WHERE floor_id=?", (floor["id"],))["n"]
        missed = self.s.one("SELECT COUNT(*) AS n FROM duties WHERE floor_id=? AND status='missed'", (floor["id"],))["n"]
        rooms = json.loads(floor["rooms"])
        today = datetime.now(self.zone).date().isoformat()
        next_position = (floor["next_position"] + 1) % len(rooms) if duty and duty["status"] == "pending" and duty["duty_date"] == today else floor["next_position"]
        next_room = rooms[next_position]
        active = f"{duty['room']} — {duty['status']}" if duty and duty["duty_date"] == today else "hali boshlanmagan"
        return (f"🏢 {floor['number']}-qavat · {floor['title']}\n"
                f"Bugungi navbat: {active}\nKeyingi xona: {next_room}\n"
                f"Eslatma: {floor['remind_time']} ({self.zone.key})\n"
                f"Topilgan a'zolar: {members}; xonaga biriktirilgan: {assigned}\n"
                f"O'tkazib yuborilgan navbatlar: {missed}")

    def admin_panel(self, uid: int, chat_id: int):
        floors = self.accessible_floors(uid)
        if not floors:
            self.send(chat_id, "Siz boshqaradigan qavat yo'q. Guruhda /setup 8 buyrug'ini yuboring yoki katta admindan ruxsat oling.")
            return
        rows = [[(f"{f['number']}-qavat · {f['title'][:25]}", f"floor:{f['id']}")] for f in floors]
        if self.is_super(uid):
            rows.append([("📊 Umumiy holat", "overview")])
        self.send(chat_id, "Admin panel. Qavatni tanlang:", keyboard(rows))

    def floor_panel(self, floor, chat_id: int):
        fid = floor["id"]
        pending = self.s.one("SELECT COUNT(*) AS n FROM join_requests WHERE floor_id=?", (fid,))["n"]
        rows = [[("🚪 Xonalar", f"rooms:{fid}"), ("👥 A'zolar", f"members:{fid}:0")],
                [(f"📝 So'rovlar ({pending})", f"requests:{fid}"), ("📋 Tarix", f"history:{fid}")],
                [("🔄 Yangilash", f"floor:{fid}")]]
        self.send(chat_id, self.status_text(floor), keyboard(rows))

    def requests_panel(self, floor, chat_id: int):
        requests = self.s.all("""SELECT r.user_id,r.room,u.name FROM join_requests r JOIN users u ON u.id=r.user_id
            WHERE r.floor_id=? ORDER BY r.requested_at LIMIT 10""", (floor["id"],))
        if not requests:
            self.send(chat_id, "Kutilayotgan xona so'rovi yo'q.", keyboard([[("⬅️ Qavat", f"floor:{floor['id']}")]]))
            return
        buttons = []
        for req in requests:
            buttons.append([(f"{req['name'][:24]} → {req['room']}", f"requestinfo:{floor['id']}:{req['user_id']}")])
            buttons.append([("✅ Tasdiqlash", f"requestyes:{floor['id']}:{req['user_id']}"),
                            ("❌ Rad etish", f"requestno:{floor['id']}:{req['user_id']}")])
        buttons.append([("🔄 Yangilash", f"requests:{floor['id']}")])
        self.send(chat_id, "Xonaga qo'shilish so'rovlari (birinchi 10 ta):", keyboard(buttons))

    def members_panel(self, floor, chat_id: int, page: int):
        rows = self.s.all("""SELECT u.id,u.name,a.room FROM group_members gm JOIN users u ON u.id=gm.user_id
            LEFT JOIN assignments a ON a.floor_id=? AND a.user_id=u.id
            WHERE gm.chat_id=? AND gm.active=1 ORDER BY u.name COLLATE NOCASE LIMIT 10 OFFSET ?""",
            (floor["id"], floor["chat_id"], page * 10))
        buttons = [[(f"{u['name'][:23]} · {u['room'] or 'xonasiz'}", f"choose:{floor['id']}:{u['id']}:{page}")] for u in rows]
        total = self.s.one("SELECT COUNT(*) AS n FROM group_members WHERE chat_id=? AND active=1", (floor["chat_id"],))["n"]
        nav = []
        if page:
            nav.append(("⬅️", f"members:{floor['id']}:{page-1}"))
        if (page + 1) * 10 < total:
            nav.append(("➡️", f"members:{floor['id']}:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([("⬅️ Qavat", f"floor:{floor['id']}")])
        self.send(chat_id, f"{floor['number']}-qavat a'zolari ({total}). A'zoni tanlang:", keyboard(buttons))

    def room_panel(self, floor, chat_id: int, uid: int | None = None, page: int = 0):
        rooms = json.loads(floor["rooms"])
        buttons = []
        for i in range(0, len(rooms), 3):
            buttons.append([(room, f"setroom:{floor['id']}:{uid}:{room}:{page}" if uid else f"room:{floor['id']}:{room}") for room in rooms[i:i+3]])
        buttons.append([("⬅️ A'zolar" if uid else "⬅️ Qavat", f"members:{floor['id']}:{page}" if uid else f"floor:{floor['id']}")])
        self.send(chat_id, "Xonani tanlang:" if uid else "Xonalar:", keyboard(buttons))

    def member_of_group(self, floor, uid: int) -> bool:
        try:
            member = self.api.call("getChatMember", chat_id=floor["chat_id"], user_id=uid)
            return member["status"] not in ("left", "kicked") and (member["status"] != "restricted" or member.get("is_member", False))
        except TelegramError:
            return False

    def handle_message(self, msg: dict):
        chat = msg["chat"]
        user = msg.get("from")
        if user:
            self.s.user(user, chat["type"] == "private")
            if chat["type"] in ("group", "supergroup"):
                self.s.member(chat["id"], user)
        for joined in msg.get("new_chat_members", []):
            self.s.member(chat["id"], joined)
        if msg.get("left_chat_member"):
            self.s.member(chat["id"], msg["left_chat_member"], False)
        text = msg.get("text", "").strip()
        if not text.startswith("/") or not user:
            return
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        uid, chat_id = user["id"], chat["id"]
        private = chat["type"] == "private"

        if command == "/start":
            self.send(chat_id, "Salom! Men qavatlar bo'yicha axlat navbatini eslataman.\n"
                      "Shaxsiy navbatingiz: /my\nXonaga yozilish: /join 8 803\nAdmin panel: /admin\nBarcha buyruqlar: /help")
        elif command == "/help":
            self.send(chat_id, "Guruh admini: /setup 8, /time 08:00, /rooms 801 802 803, /register\n"
                      "Talaba: /start (bot bilan shaxsiy chatda), /join 8 803, /my\n"
                      "Admin: /admin, /status, /history; katta admin: /overview, /broadcast groups|users matn, /admin_add ID, /admin_remove ID, /floor_admin ID QAVAT, /floor_admin_remove ID QAVAT")
        elif command == "/register" and not private:
            self.send(chat_id, f"{user.get('first_name','Aʼzo')}, ro'yxatga olindingiz. Shaxsiy eslatma uchun botni oching: https://t.me/{self.username}")
        elif command == "/setup":
            if private or not self.is_chat_admin(chat_id, uid):
                self.send(chat_id, "Bu buyruqni guruh administratori guruhda yuborishi kerak.")
                return
            try:
                bot_membership = self.api.call("getChatMember", chat_id=chat_id, user_id=self.bot_id)
            except TelegramError:
                bot_membership = {}
            if bot_membership.get("status") not in ("creator", "administrator"):
                self.send(chat_id, "Avval botga guruhda admin huquqini bering; shunda a'zolik o'zgarishlarini ko'ra oladi.")
                return
            if len(args) != 1 or not re.fullmatch(r"[1-9]\d{0,2}", args[0]):
                self.send(chat_id, "Misol: /setup 8")
                return
            try:
                fid = self.s.setup(args[0], chat_id, chat.get("title", "Guruh"), self.default_time)
            except ValueError as exc:
                self.send(chat_id, str(exc))
                return
            self.send(chat_id, f"{args[0]}-qavat ulandi. Standart xonalar: {', '.join(json.loads(self.s.one('SELECT rooms FROM floors WHERE id=?',(fid,))['rooms']))}.\n"
                      "Vaqt: /time 08:00. Xonalar boshqacha bo'lsa: /rooms 801 802 803. A'zolarga /register yuborishni ayting; admin /admin orqali ularni xonaga biriktiradi.")
        elif command in ("/time", "/rooms"):
            floor = self.s.one("SELECT * FROM floors WHERE chat_id=?", (chat_id,)) if not private else None
            if not floor or not self.is_floor_admin(floor, uid):
                self.send(chat_id, "Bu buyruq ulangan guruhda admin uchun.")
                return
            if command == "/time":
                if len(args) != 1 or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", args[0]):
                    self.send(chat_id, "Misol: /time 08:00")
                    return
                self.s.execute("UPDATE floors SET remind_time=? WHERE id=?", (args[0], floor["id"]))
                self.send(chat_id, f"Eslatma vaqti {args[0]} ga o'zgardi ({self.zone.key}).")
            else:
                if not 1 <= len(args) <= 50 or len(set(args)) != len(args) or any(not re.fullmatch(r"\d{1,6}", x) for x in args):
                    self.send(chat_id, "1–50 ta takrorlanmagan xona kiriting. Misol: /rooms 801 802 803")
                    return
                duty = self.s.current_duty(floor["id"])
                if duty and duty["status"] == "pending" and duty["duty_date"] == datetime.now(self.zone).date().isoformat():
                    self.send(chat_id, "Bugungi navbat tugagandan keyin xonalar ro'yxatini o'zgartiring.")
                    return
                old_rooms = set(json.loads(floor["rooms"]))
                removed = old_rooms - set(args)
                if removed:
                    placeholders = ",".join("?" for _ in removed)
                    self.s.execute(f"DELETE FROM assignments WHERE floor_id=? AND room IN ({placeholders})", (floor["id"], *removed))
                self.s.execute("UPDATE floors SET rooms=?,next_position=0 WHERE id=?", (json.dumps(args), floor["id"]))
                self.send(chat_id, f"Xonalar saqlandi: {', '.join(args)}. Navbat yangi ro'yxatning birinchi xonasidan davom etadi.")
        elif command == "/join":
            if not private or len(args) != 2:
                self.send(chat_id, "Shaxsiy chatda yozing: /join 8 803")
                return
            floor = self.s.one("SELECT * FROM floors WHERE number=?", (args[0],))
            if not floor or args[1] not in json.loads(floor["rooms"]):
                self.send(chat_id, "Qavat yoki xona topilmadi.")
                return
            if not self.member_of_group(floor, uid):
                self.send(chat_id, "Avval shu qavat guruhiga a'zo bo'ling.")
                return
            self.s.member(floor["chat_id"], user)
            assigned = self.s.one("SELECT room FROM assignments WHERE floor_id=? AND user_id=?", (floor["id"], uid))
            if assigned and assigned["room"] == args[1]:
                self.send(chat_id, f"Siz allaqachon {args[1]}-xonaga biriktirilgansiz. /my orqali navbatni ko'ring.")
                return
            self.s.execute("""INSERT INTO join_requests(floor_id,user_id,room,requested_at) VALUES(?,?,?,?)
                ON CONFLICT(floor_id,user_id) DO UPDATE SET room=excluded.room,requested_at=excluded.requested_at""",
                (floor["id"], uid, args[1], datetime.now(self.zone).isoformat()))
            self.send(chat_id, f"{args[0]}-qavat, {args[1]}-xona uchun so'rov yuborildi. Admin tasdiqlagach eslatmalar keladi.")
        elif command == "/my":
            rows = self.s.all("""SELECT f.*,a.room FROM assignments a JOIN floors f ON f.id=a.floor_id WHERE a.user_id=? ORDER BY f.number""", (uid,))
            if not rows:
                self.send(chat_id, "Hozircha xonaga biriktirilmagansiz. /join 8 803 yoki admin orqali birikishingiz mumkin.")
                return
            out = []
            for floor in rows:
                duty = self.s.current_duty(floor["id"])
                active = duty and duty["room"] == floor["room"] and duty["status"] == "pending" and duty["duty_date"] == datetime.now(self.zone).date().isoformat()
                out.append(f"{floor['number']}-qavat, {floor['room']}-xona: {'bugun sizlarning navbat' if active else 'hozir navbat sizda emas'}")
            self.send(chat_id, "\n".join(out))
        elif command == "/admin" and private:
            self.admin_panel(uid, chat_id)
        elif command in ("/status", "/history"):
            floors = self.accessible_floors(uid) if private else [f for f in [self.s.one("SELECT * FROM floors WHERE chat_id=?", (chat_id,))] if f and self.is_floor_admin(f, uid)]
            if not floors:
                self.send(chat_id, "Admin ruxsati kerak.")
                return
            if command == "/status":
                self.send(chat_id, "\n\n".join(self.status_text(f) for f in floors)[:4000])
            else:
                self.send(chat_id, "\n\n".join(self.history_text(f) for f in floors)[:4000])
        elif command == "/overview" and self.is_super(uid):
            self.overview(chat_id)
        elif command in ("/admin_add", "/admin_remove", "/floor_admin", "/floor_admin_remove"):
            if not self.is_super(uid) or not private:
                self.send(chat_id, "Faqat katta admin shaxsiy chatda boshqaradi.")
                return
            if command in ("/floor_admin", "/floor_admin_remove"):
                if len(args) != 2 or not args[0].isdigit():
                    self.send(chat_id, "Misol: /floor_admin 123456789 8")
                    return
                floor = self.s.one("SELECT * FROM floors WHERE number=?", (args[1],))
                if not floor:
                    self.send(chat_id, "Qavat topilmadi.")
                    return
                if command == "/floor_admin":
                    self.s.execute("INSERT OR IGNORE INTO floor_admins(floor_id,user_id) VALUES(?,?)", (floor["id"], int(args[0])))
                else:
                    self.s.execute("DELETE FROM floor_admins WHERE floor_id=? AND user_id=?", (floor["id"], int(args[0])))
            else:
                if len(args) != 1 or not args[0].isdigit():
                    self.send(chat_id, f"Misol: {command} 123456789")
                    return
                if command == "/admin_add":
                    self.s.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (int(args[0]),))
                else:
                    self.s.execute("DELETE FROM admins WHERE user_id=?", (int(args[0]),))
            self.send(chat_id, "Admin ruxsati yangilandi.")
        elif command == "/broadcast":
            if not self.is_super(uid) or not private:
                self.send(chat_id, "Faqat katta admin shaxsiy chatda yuboradi.")
                return
            if len(args) < 2 or args[0] not in ("groups", "users"):
                self.send(chat_id, "Misol: /broadcast groups Bugun yig'ilish bor\nYoki: /broadcast users Salom")
                return
            body = text.split(None, 2)[2]
            if len(body) > 4000:
                self.send(chat_id, "Xabar 4000 belgidan oshmasin.")
                return
            self.broadcast_drafts[uid] = (args[0], body)
            count = self.s.one("SELECT COUNT(*) AS n FROM floors" if args[0] == "groups" else "SELECT COUNT(*) AS n FROM users WHERE private_started=1")["n"]
            self.send(chat_id, f"{count} ta manzilga yuborilsinmi?\n\n{body}", keyboard([[("✅ Yuborish", "broadcast:yes"), ("Bekor qilish", "broadcast:no")]]))

    def history_text(self, floor):
        duties = self.s.all("""SELECT d.*,u.name FROM duties d LEFT JOIN users u ON u.id=d.completed_by
            WHERE d.floor_id=? ORDER BY d.duty_date DESC LIMIT 10""", (floor["id"],))
        if not duties:
            return f"{floor['number']}-qavat: tarix bo'sh."
        names = {"done": "✅ to'kildi", "pending": "⏳ kutilmoqda", "missed": "❌ to'kilmadi"}
        return f"{floor['number']}-qavat, oxirgi navbatlar:\n" + "\n".join(
            f"{d['duty_date']} · {d['room']} · {names[d['status']]}" + (f" · {d['name']}" if d["name"] else "") for d in duties)

    def overview(self, chat_id: int):
        floors = self.s.all("SELECT * FROM floors ORDER BY CAST(number AS INTEGER)")
        if not floors:
            self.send(chat_id, "Hali qavatlar qo'shilmagan.")
            return
        self.send(chat_id, "\n\n".join(self.status_text(f) for f in floors)[:4000])

    def handle_callback(self, query: dict):
        uid = query["from"]["id"]
        data = query.get("data", "")
        msg = query.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        self.s.user(query["from"], chat.get("type") == "private")
        if data.startswith("done:"):
            try:
                duty_id = int(data.split(":")[1])
            except (ValueError, IndexError):
                self.answer(query["id"], "Noto'g'ri tugma.", True)
                return
            duty = self.s.one("SELECT * FROM duties WHERE id=?", (duty_id,))
            floor = self.s.one("SELECT * FROM floors WHERE id=?", (duty["floor_id"],)) if duty else None
            if not floor or chat_id != floor["chat_id"]:
                self.answer(query["id"], "Bu guruh uchun emas.", True)
                return
            assigned = self.s.one("SELECT 1 FROM assignments WHERE floor_id=? AND user_id=? AND room=?", (floor["id"], uid, duty["room"]))
            if not assigned and not self.is_floor_admin(floor, uid):
                self.answer(query["id"], "Faqat shu xona a'zosi yoki admin tasdiqlay oladi.", True)
                return
            try:
                self.s.complete(duty_id, uid, datetime.now(self.zone))
            except ValueError as exc:
                self.answer(query["id"], str(exc), True)
                return
            self.answer(query["id"], "Qayd etildi!")
            try:
                self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg["message_id"], reply_markup=keyboard([]))
            except TelegramError:
                LOG.exception("Could not remove completion button")
            self.send(chat_id, f"✅ {duty['room']}-xona navbatni bajardi. Keyingi xona ertangi eslatmada chiqadi.")
            return

        if chat.get("type") != "private":
            self.answer(query["id"], "Admin panel bot bilan shaxsiy chatda ishlaydi.", True)
            return
        if data.startswith("broadcast:"):
            if not self.is_super(uid) or uid not in self.broadcast_drafts:
                self.answer(query["id"], "Yuboriladigan xabar yo'q.", True)
                return
            target, body = self.broadcast_drafts.pop(uid)
            if data == "broadcast:no":
                self.answer(query["id"], "Bekor qilindi.")
                return
            self.answer(query["id"], "Yuborish boshlandi.")
            ids = [r["chat_id"] for r in self.s.all("SELECT chat_id FROM floors")] if target == "groups" else [r["id"] for r in self.s.all("SELECT id FROM users WHERE private_started=1")]
            ok = failed = 0
            for recipient in ids:
                try:
                    self.send(recipient, body)
                    ok += 1
                except TelegramError as exc:
                    if exc.retry_after:
                        time.sleep(min(exc.retry_after, 60))
                        try:
                            self.send(recipient, body)
                            ok += 1
                            continue
                        except TelegramError:
                            pass
                    failed += 1
                time.sleep(0.04)
            self.send(chat_id, f"Broadcast tugadi: {ok} ta yuborildi, {failed} ta yetkazilmadi.")
            return

        if data == "overview":
            if self.is_super(uid):
                self.answer(query["id"])
                self.overview(chat_id)
            else:
                self.answer(query["id"], "Ruxsat yo'q.", True)
            return
        parts = data.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            self.answer(query["id"], "Noto'g'ri tugma.", True)
            return
        floor = self.s.one("SELECT * FROM floors WHERE id=?", (int(parts[1]),))
        if not floor or not self.is_floor_admin(floor, uid):
            self.answer(query["id"], "Admin ruxsati kerak.", True)
            return
        self.answer(query["id"])
        action = parts[0]
        try:
            if action == "floor":
                self.floor_panel(floor, chat_id)
            elif action == "members":
                self.members_panel(floor, chat_id, int(parts[2]))
            elif action == "rooms":
                self.room_panel(floor, chat_id)
            elif action == "requests":
                self.requests_panel(floor, chat_id)
            elif action == "requestinfo":
                req = self.s.one("SELECT r.room,u.name FROM join_requests r JOIN users u ON u.id=r.user_id WHERE r.floor_id=? AND r.user_id=?", (floor["id"], int(parts[2])))
                self.send(chat_id, f"{req['name']} ({parts[2]}) → {req['room']}" if req else "So'rov topilmadi.")
            elif action in ("requestyes", "requestno"):
                target = int(parts[2])
                req = self.s.one("SELECT room FROM join_requests WHERE floor_id=? AND user_id=?", (floor["id"], target))
                if not req:
                    self.send(chat_id, "So'rov allaqachon ko'rib chiqilgan.")
                    return
                if action == "requestyes":
                    if req["room"] not in json.loads(floor["rooms"]) or not self.member_of_group(floor, target):
                        self.send(chat_id, "A'zo guruhda emas yoki xona endi mavjud emas. So'rov rad etildi.")
                        action = "requestno"
                    else:
                        self.s.execute("INSERT INTO assignments(floor_id,user_id,room) VALUES(?,?,?) ON CONFLICT(floor_id,user_id) DO UPDATE SET room=excluded.room", (floor["id"], target, req["room"]))
                        self.send(chat_id, f"{target} → {req['room']}-xona tasdiqlandi.")
                self.s.execute("DELETE FROM join_requests WHERE floor_id=? AND user_id=?", (floor["id"], target))
                try:
                    self.send(target, f"{floor['number']}-qavat, {req['room']}-xona so'rovingiz {'tasdiqlandi' if action == 'requestyes' else 'rad etildi'}.")
                except TelegramError:
                    pass
                self.requests_panel(floor, chat_id)
            elif action == "choose":
                self.room_panel(floor, chat_id, int(parts[2]), int(parts[3]))
            elif action == "setroom":
                target, room, page = int(parts[2]), parts[3], int(parts[4])
                if room not in json.loads(floor["rooms"]) or not self.s.one("SELECT 1 FROM group_members WHERE chat_id=? AND user_id=? AND active=1", (floor["chat_id"], target)):
                    self.send(chat_id, "Xona yoki a'zo topilmadi.")
                    return
                self.s.execute("INSERT INTO assignments(floor_id,user_id,room) VALUES(?,?,?) ON CONFLICT(floor_id,user_id) DO UPDATE SET room=excluded.room", (floor["id"], target, room))
                self.s.execute("DELETE FROM join_requests WHERE floor_id=? AND user_id=?", (floor["id"], target))
                self.send(chat_id, f"A'zo {target} → {room}-xona.")
                self.members_panel(floor, chat_id, page)
            elif action == "room":
                room = parts[2]
                users = self.s.all("""SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id
                    WHERE a.floor_id=? AND a.room=? ORDER BY u.name""", (floor["id"], room))
                self.send(chat_id, f"{room}-xona: " + (", ".join(u["name"] for u in users) or "hali a'zo yo'q"))
            elif action == "history":
                self.send(chat_id, self.history_text(floor))
        except (IndexError, ValueError):
            self.send(chat_id, "Tugma ma'lumoti eskirgan.")

    def handle_update(self, update: dict):
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "chat_member" in update:
            event = update["chat_member"]
            member = event["new_chat_member"]
            active = member["status"] not in ("left", "kicked")
            self.s.member(event["chat"]["id"], member["user"], active)
        elif "my_chat_member" in update:
            event = update["my_chat_member"]
            if event["new_chat_member"]["status"] in ("left", "kicked"):
                LOG.warning("Bot removed from chat %s", event["chat"]["id"])

    def tick(self, now: datetime | None = None):
        now = now or datetime.now(self.zone)
        today = now.date().isoformat()
        current_hm = now.strftime("%H:%M")
        for floor in self.s.all("SELECT * FROM floors"):
            if current_hm < floor["remind_time"]:
                continue
            duty = self.s.prepare_duty(floor["id"], today)
            if duty["status"] != "pending" or duty["reminded"]:
                continue
            people = self.s.all("""SELECT u.id,u.name,u.private_started FROM assignments a JOIN users u ON u.id=a.user_id
                JOIN group_members gm ON gm.user_id=u.id AND gm.chat_id=? AND gm.active=1
                WHERE a.floor_id=? AND a.room=? ORDER BY u.name""", (floor["chat_id"], floor["id"], duty["room"]))
            mentions = ", ".join(f'<a href="tg://user?id={p["id"]}">{html.escape(p["name"])}</a>' for p in people)
            body = f"🗑 Bugun {html.escape(duty['room'])}-xonaning axlat to'kish navbati. Esingizdan chiqmasin!"
            if mentions:
                body += "\n" + mentions
            try:
                sent = self.send(floor["chat_id"], body, keyboard([[("✅ To'kdim", f"done:{duty['id']}")]]), html_mode=True)
            except TelegramError:
                LOG.exception("Reminder failed for floor %s", floor["number"])
                continue
            self.s.execute("UPDATE duties SET reminded=1,group_message_id=? WHERE id=?", (sent["message_id"], duty["id"]))
            for person in people:
                if person["private_started"]:
                    try:
                        self.send(person["id"], f"🗑 Bugun {duty['room']}-xonangizning axlat to'kish navbati. Guruhdagi «To'kdim» tugmasini bosing.")
                    except TelegramError:
                        LOG.info("Private reminder unavailable for user %s", person["id"])


def main():
    load_env()
    token = os.getenv("BOT_TOKEN", "")
    admin_ids = os.getenv("ADMIN_IDS", "")
    if not token or not admin_ids:
        sys.exit("BOT_TOKEN va ADMIN_IDS ni .env faylida kiriting.")
    try:
        admins = {int(x.strip()) for x in admin_ids.split(",") if x.strip()}
        zone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
    except (ValueError, KeyError) as exc:
        sys.exit(f"Sozlama xatosi: {exc}")
    default_time = os.getenv("DEFAULT_REMINDER_TIME", "08:00")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", default_time):
        sys.exit("DEFAULT_REMINDER_TIME HH:MM shaklida bo'lishi kerak.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(os.getenv("DATABASE_PATH", "navbatchi.sqlite3"))
    bot = Bot(store, Telegram(token), admins, zone, default_time)
    LOG.info("Bot @%s started", bot.username)
    offset = None
    while True:
        try:
            bot.tick()
            updates = bot.api.call("getUpdates", offset=offset, timeout=15,
                                   allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    bot.handle_update(update)
                except Exception:
                    LOG.exception("Update %s failed", update["update_id"])
        except (TelegramError, OSError):
            LOG.exception("Polling failed; retrying")
            time.sleep(5)


if __name__ == "__main__":
    main()
