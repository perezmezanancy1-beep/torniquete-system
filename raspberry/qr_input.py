import time


SHIFT_KEYS = {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}
ENTER_KEYS = {"KEY_ENTER", "KEY_KPENTER"}

KEY_MAP = {
    **{f"KEY_{digit}": digit for digit in "0123456789"},
    **{f"KEY_{letter.upper()}": letter for letter in "abcdefghijklmnopqrstuvwxyz"},
    "KEY_MINUS": "-",
    # En el teclado latinoamericano la tecla física AB10 (KEY_SLASH en
    # evdev) corresponde a guion y, con Shift, a guion bajo.
    "KEY_SLASH": "-",
    "KEY_EQUAL": "=",
}


def keycode_to_character(keycode, shift_pressed=False):
    """Convierte un keycode evdev del lector USB a Base64 URL."""
    if isinstance(keycode, list):
        keycode = keycode[0] if keycode else None

    character = KEY_MAP.get(keycode)
    if character is None:
        return None

    if len(character) == 1 and character.isalpha():
        return character.upper() if shift_pressed else character
    # El lector está configurado como teclado latinoamericano y emite
    # Shift+0 para el signo "=" usado como padding por base64Url de Dart.
    if keycode == "KEY_0" and shift_pressed:
        return "="
    if keycode in {"KEY_MINUS", "KEY_SLASH"} and shift_pressed:
        return "_"
    if keycode == "KEY_EQUAL" and shift_pressed:
        return "+"
    return character


class QrKeyboardBuffer:
    """Reconstruye el texto enviado por un lector QR tipo teclado USB."""

    def __init__(self, max_length=2048):
        self.max_length = max_length
        self.buffer = []
        self.shift_keys_down = set()

    def reset(self):
        self.buffer.clear()
        self.shift_keys_down.clear()

    def feed(self, keycode, keystate):
        if isinstance(keycode, list):
            keycode = keycode[0] if keycode else None
        if not keycode:
            return None

        if keycode in SHIFT_KEYS:
            if keystate == 1:
                self.shift_keys_down.add(keycode)
            elif keystate == 0:
                self.shift_keys_down.discard(keycode)
            return None

        # Algunos lectores HID emiten el segundo carácter consecutivo como
        # repetición (2). Se aceptan pulsación y repetición, pero no liberación.
        if keystate not in (1, 2):
            return None

        if keycode in ENTER_KEYS:
            token = "".join(self.buffer).strip()
            self.buffer.clear()
            return token or None

        if keycode == "KEY_BACKSPACE":
            if self.buffer:
                self.buffer.pop()
            return None

        character = keycode_to_character(
            keycode,
            shift_pressed=bool(self.shift_keys_down),
        )
        if character is not None:
            if len(self.buffer) >= self.max_length:
                self.reset()
                return None
            self.buffer.append(character)
        return None


class RecentQrTokens:
    """Acepta una sola vez cada QR durante su vida útil visible."""

    def __init__(self, retention_seconds=120, max_entries=128):
        self.retention_seconds = float(retention_seconds)
        self.max_entries = int(max_entries)
        self.seen = {}

    def accept(self, token, now=None):
        current_time = time.monotonic() if now is None else float(now)
        cutoff = current_time - self.retention_seconds
        self.seen = {
            value: timestamp
            for value, timestamp in self.seen.items()
            if timestamp > cutoff
        }

        if token in self.seen:
            return False

        if len(self.seen) >= self.max_entries:
            oldest = min(self.seen, key=self.seen.get)
            del self.seen[oldest]

        self.seen[token] = current_time
        return True
