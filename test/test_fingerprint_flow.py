import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_torniquete_module():
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 1
    fcntl.LOCK_NB = 2
    fcntl.flock = lambda *_args: None
    sys.modules["fcntl"] = fcntl

    requests = types.ModuleType("requests")
    requests.RequestException = RuntimeError
    requests.Session = type(
        "Session",
        (),
        {"mount": lambda *_args: None},
    )
    requests_adapters = types.ModuleType("requests.adapters")
    requests_adapters.HTTPAdapter = type(
        "HTTPAdapter",
        (),
        {"__init__": lambda self, **_kwargs: None},
    )
    retry_module = types.ModuleType("urllib3.util.retry")
    retry_module.Retry = type(
        "Retry",
        (),
        {"__init__": lambda self, **_kwargs: None},
    )
    sys.modules["requests"] = requests
    sys.modules["requests.adapters"] = requests_adapters
    sys.modules["urllib3.util.retry"] = retry_module

    evdev = types.ModuleType("evdev")
    evdev.ecodes = types.SimpleNamespace(EV_KEY=1)
    evdev.InputDevice = object
    evdev.categorize = lambda event: event
    sys.modules["evdev"] = evdev

    fingerprint_package = types.ModuleType("pyfingerprint")
    fingerprint_module = types.ModuleType("pyfingerprint.pyfingerprint")
    fingerprint_module.PyFingerprint = object
    sys.modules["pyfingerprint"] = fingerprint_package
    sys.modules["pyfingerprint.pyfingerprint"] = fingerprint_module

    gpio = types.ModuleType("RPi.GPIO")
    rpi = types.ModuleType("RPi")
    rpi.GPIO = gpio
    sys.modules["RPi"] = rpi
    sys.modules["RPi.GPIO"] = gpio

    raspberry_path = Path(__file__).resolve().parents[1] / "raspberry"
    sys.path.insert(0, str(raspberry_path))
    spec = importlib.util.spec_from_file_location(
        "torniquete_test_module",
        raspberry_path / "torniquete.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.stop_event = types.SimpleNamespace(
        is_set=lambda: False,
        wait=lambda _seconds: False,
    )
    return module


class FakeSensor:
    def __init__(self, readings):
        self.readings = iter(readings)
        self.calls = 0

    def readImage(self):
        self.calls += 1
        return next(self.readings)


class FingerprintFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_torniquete_module()

    def test_requires_stable_finger_placement(self):
        sensor = FakeSensor([False, True, False, True, True, True])

        result = self.module.wait_for_finger(
            sensor,
            True,
            timeout_seconds=1,
            stable_reads=3,
        )

        self.assertTrue(result)
        self.assertEqual(sensor.calls, 6)

    def test_requires_stable_finger_removal(self):
        sensor = FakeSensor([False, False, True, False, False, False, False, False])

        result = self.module.wait_for_finger(
            sensor,
            False,
            timeout_seconds=1,
            stable_reads=5,
        )

        self.assertTrue(result)
        self.assertEqual(sensor.calls, 8)

    def test_voice_cache_changes_when_the_local_model_changes(self):
        original_model = self.module.active_voice_model
        try:
            self.module.active_voice_model = "/voices/old.onnx"
            old_path = self.module.voice_cache_path("Bienvenido")
            self.module.active_voice_model = "/voices/new.onnx"
            new_path = self.module.voice_cache_path("Bienvenido")
        finally:
            self.module.active_voice_model = original_model

        self.assertNotEqual(old_path, new_path)

if __name__ == "__main__":
    unittest.main()
