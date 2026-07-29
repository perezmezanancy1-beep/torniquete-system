SHIFT_KEYS = {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}
ENTER_KEYS = {"KEY_ENTER", "KEY_KPENTER"}

KEY_MAP = {
    **{f"KEY_{digit}": digit for digit in "0123456789"},
    **{f"KEY_{letter.upper()}": letter for letter in "abcdefghijklmnopqrstuvwxyz"},
    "KEY_MINUS": "-",
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
    if keycode == "KEY_MINUS" and shift_pressed:
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

        # Solo se procesa el evento de pulsación, no repetición ni liberación.
        if keystate != 1:
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
