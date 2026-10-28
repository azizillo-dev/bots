import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import Bot, Store


ZONE = ZoneInfo("Asia/Tashkent")


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.next_id = 1

    def call(self, method, **params):
        if method == "getMe":
            return {"id": 777, "username": "test_navbatchi_bot"}
        if method == "sendMessage":
            self.sent.append(params)
            result = {"message_id": self.next_id}
            self.next_id += 1
            return result
        if method == "getChatMember":
            return {"status": "member"}
        if method in ("answerCallbackQuery", "editMessageReplyMarkup"):
            return True
        raise AssertionError(method)


class RotaTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.api = FakeTelegram()
        self.bot = Bot(self.store, self.api, {999}, ZONE, "08:00")
        self.floor_id = self.store.setup("8", -800, "8-qavat", "08:00")
        for uid, private in [(1, True), (2, False), (3, True)]:
            self.store.user({"id": uid, "first_name": f"Talaba {uid}"}, private)
            self.store.member(-800, {"id": uid, "first_name": f"Talaba {uid}"})
        self.store.execute("INSERT INTO assignments(floor_id,user_id,room) VALUES(?,?,?)", (self.floor_id, 1, "801"))
        self.store.execute("INSERT INTO assignments(floor_id,user_id,room) VALUES(?,?,?)", (self.floor_id, 2, "801"))

    def test_reminder_completion_and_next_day(self):
        self.bot.tick(datetime(2026, 10, 28, 7, 59, tzinfo=ZONE))
        self.assertEqual(0, len(self.api.sent))
        self.bot.tick(datetime(2026, 10, 28, 8, 0, tzinfo=ZONE))
        self.bot.tick(datetime(2026, 10, 28, 8, 10, tzinfo=ZONE))
        self.assertEqual(2, len(self.api.sent))  # group and one opted-in private user
        self.assertIn("Talaba 1", self.api.sent[0]["text"])
        self.assertIn("Talaba 2", self.api.sent[0]["text"])
        duty = self.store.current_duty(self.floor_id)
        self.bot.handle_callback({"id": "cb", "from": {"id": 1, "first_name": "Talaba 1"},
                                  "data": f"done:{duty['id']}",
                                  "message": {"chat": {"id": -800, "type": "supergroup"}, "message_id": 1}})
        self.assertEqual("done", self.store.current_duty(self.floor_id)["status"])
        self.bot.tick(datetime(2026, 10, 29, 8, 0, tzinfo=ZONE))
        self.assertEqual("802", self.store.current_duty(self.floor_id)["room"])

    def test_missed_days_rotate_and_are_recorded(self):
        self.bot.tick(datetime(2026, 10, 28, 8, 0, tzinfo=ZONE))
        self.bot.tick(datetime(2026, 10, 30, 8, 0, tzinfo=ZONE))
        duties = self.store.all("SELECT room,status FROM duties ORDER BY duty_date")
        self.assertEqual([("801", "missed"), ("802", "missed"), ("803", "pending")],
                         [(d["room"], d["status"]) for d in duties])

    def test_other_room_cannot_complete(self):
        self.bot.tick(datetime(2026, 10, 28, 8, 0, tzinfo=ZONE))
        duty = self.store.current_duty(self.floor_id)
        self.bot.handle_callback({"id": "cb", "from": {"id": 3, "first_name": "Talaba 3"},
                                  "data": f"done:{duty['id']}",
                                  "message": {"chat": {"id": -800, "type": "supergroup"}, "message_id": 1}})
        self.assertEqual("pending", self.store.current_duty(self.floor_id)["status"])

    def test_join_requires_admin_approval(self):
        self.bot.handle_message({"chat": {"id": 3, "type": "private"},
                                 "from": {"id": 3, "first_name": "Talaba 3"},
                                 "text": "/join 8 803"})
        self.assertIsNone(self.store.one("SELECT room FROM assignments WHERE floor_id=? AND user_id=3", (self.floor_id,)))
        self.assertEqual("803", self.store.one("SELECT room FROM join_requests WHERE floor_id=? AND user_id=3", (self.floor_id,))["room"])
        self.bot.handle_callback({"id": "approve", "from": {"id": 999, "first_name": "Admin"},
                                  "data": f"requestyes:{self.floor_id}:3",
                                  "message": {"chat": {"id": 999, "type": "private"}, "message_id": 10}})
        self.assertEqual("803", self.store.one("SELECT room FROM assignments WHERE floor_id=? AND user_id=3", (self.floor_id,))["room"])


if __name__ == "__main__":
    unittest.main()
