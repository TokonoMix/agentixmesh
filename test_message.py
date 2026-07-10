import json
import unittest

from pm_mesh import message
from pm_mesh.message import Message, MAX_BODY_BYTES


class MessageTest(unittest.TestCase):
    def _valid(self, **over):
        base = dict(
            id="i1", thread="i1", from_="1000:gallery", to="1000:peer",
            kind="request", ts_utc="2026-06-25T00:00:00Z", body="hoi",
        )
        base.update(over)
        return Message(**base)

    def test_roundtrip_with_newlines_and_unicode(self):
        msg = self._valid(body="regel1\nregel2 — café ☕")
        back = message.from_json(message.to_json(msg))
        self.assertEqual(back, msg)

    def test_json_uses_from_and_to_keys(self):
        d = json.loads(message.to_json(self._valid()))
        self.assertIn("from", d)
        self.assertIn("to", d)
        self.assertNotIn("from_", d)
        self.assertEqual(d["from"], "1000:gallery")

    def test_new_message_defaults(self):
        msg = message.new_message("1000:peer", "hoi")
        self.assertTrue(msg.id)
        self.assertEqual(msg.thread, msg.id)  # thread default = id
        self.assertTrue(msg.ts_utc.endswith("Z"))
        self.assertEqual(msg.kind, "request")

    def test_kind_enum_rejected(self):
        with self.assertRaises(ValueError):
            message.validate(self._valid(kind="bogus"))

    def test_body_limit_on_byte_boundary(self):
        ok = self._valid(body="a" * MAX_BODY_BYTES)
        message.validate(ok)  # exact limit is allowed
        with self.assertRaises(ValueError):
            message.validate(self._valid(body="a" * (MAX_BODY_BYTES + 1)))

    def test_multibyte_body_counts_bytes_not_chars(self):
        # '☕' is 3 utf-8 bytes; a string of chars whose byte length exceeds the cap must fail
        n = MAX_BODY_BYTES // 3 + 1
        with self.assertRaises(ValueError):
            message.validate(self._valid(body="☕" * n))

    def test_address_form_validated(self):
        with self.assertRaises(ValueError):
            message.validate(self._valid(to="notanaddress"))
        with self.assertRaises(ValueError):
            message.validate(self._valid(from_="abc:gallery"))  # uid must be numeric

    def test_missing_field_rejected(self):
        with self.assertRaises(ValueError):
            message.validate(self._valid(body=""))

    def test_from_json_missing_key_raises_valueerror(self):
        with self.assertRaises(ValueError):
            message.from_json(json.dumps({"id": "x"}))


if __name__ == "__main__":
    unittest.main()
