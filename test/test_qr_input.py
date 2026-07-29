import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry"))

from qr_input import QrKeyboardBuffer, keycode_to_character


class QrInputTests(unittest.TestCase):
    def test_maps_lowercase_uppercase_and_urlsafe_symbols(self):
        self.assertEqual(keycode_to_character("KEY_A"), "a")
        self.assertEqual(keycode_to_character("KEY_A", True), "A")
        self.assertEqual(keycode_to_character("KEY_MINUS"), "-")
        self.assertEqual(keycode_to_character("KEY_MINUS", True), "_")
        self.assertEqual(keycode_to_character("KEY_EQUAL"), "=")
        self.assertEqual(keycode_to_character("KEY_0", True), "=")

    def test_reconstructs_complete_mipase_token(self):
        reader = QrKeyboardBuffer()
        events = [
            ("KEY_LEFTSHIFT", 1),
            ("KEY_A", 1),
            ("KEY_A", 0),
            ("KEY_LEFTSHIFT", 0),
            ("KEY_B", 1),
            ("KEY_B", 0),
            ("KEY_1", 1),
            ("KEY_1", 0),
            ("KEY_MINUS", 1),
            ("KEY_MINUS", 0),
            ("KEY_LEFTSHIFT", 1),
            ("KEY_MINUS", 1),
            ("KEY_MINUS", 0),
            ("KEY_LEFTSHIFT", 0),
            ("KEY_ENTER", 1),
        ]

        completed = None
        for keycode, state in events:
            token = reader.feed(keycode, state)
            if token is not None:
                completed = token

        self.assertEqual(completed, "Ab1-_")

    def test_accepts_scanner_repeat_as_consecutive_character(self):
        reader = QrKeyboardBuffer()
        reader.feed("KEY_A", 0)
        reader.feed("KEY_A", 1)
        reader.feed("KEY_A", 2)
        self.assertEqual(reader.feed("KEY_ENTER", 1), "aa")


if __name__ == "__main__":
    unittest.main()
