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


class FakeExitSensor:
    def __init__(self, search_result):
        self.search_result = search_result
        self.deleted = []

    def storeTemplate(self, positionNumber=-1, charBufferNumber=0x01):
        self.stored_buffer = charBufferNumber
        return 7

    def searchTemplate(self):
        return self.search_result

    def deleteTemplate(self, position):
        self.deleted.append(position)
        return True


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

    def test_exit_reader_validates_second_sample_with_internal_search(self):
        sensor = FakeExitSensor((7, 42))
        original_functions = (
            self.module.request_finger_removal,
            self.module.capture_fingerprint_sample,
            self.module.assign_fingerprint_to_person,
            self.module.enqueue_voice,
        )
        self.module.request_finger_removal = lambda *_args: None
        self.module.capture_fingerprint_sample = lambda *_args: None
        self.module.assign_fingerprint_to_person = lambda *_args: None
        self.module.enqueue_voice = lambda *_args: None
        try:
            position = self.module.enroll_exit_fingerprint_by_search(
                sensor,
                "176723",
                "salida",
                "command-1",
            )
        finally:
            (
                self.module.request_finger_removal,
                self.module.capture_fingerprint_sample,
                self.module.assign_fingerprint_to_person,
                self.module.enqueue_voice,
            ) = original_functions

        self.assertEqual(position, 7)
        self.assertEqual(sensor.stored_buffer, 0x01)
        self.assertEqual(sensor.deleted, [])

    def test_exit_reader_removes_temporary_template_on_mismatch(self):
        sensor = FakeExitSensor((-1, -1))
        original_functions = (
            self.module.request_finger_removal,
            self.module.capture_fingerprint_sample,
            self.module.enqueue_voice,
        )
        self.module.request_finger_removal = lambda *_args: None
        self.module.capture_fingerprint_sample = lambda *_args: None
        self.module.enqueue_voice = lambda *_args: None
        try:
            with self.assertRaisesRegex(RuntimeError, "no coincidieron"):
                self.module.enroll_exit_fingerprint_by_search(
                    sensor,
                    "176723",
                    "salida",
                    "command-2",
                )
        finally:
            (
                self.module.request_finger_removal,
                self.module.capture_fingerprint_sample,
                self.module.enqueue_voice,
            ) = original_functions

        self.assertEqual(sensor.deleted, [7])

if __name__ == "__main__":
    unittest.main()
