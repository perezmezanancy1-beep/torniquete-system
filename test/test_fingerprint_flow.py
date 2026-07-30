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


class FakeEnrollmentSensor:
    def __init__(self):
        self.events = []
        self.search_results = iter([(-1, 0), (-1, 0), (6, 100)])

    def searchTemplate(self):
        self.events.append("search")
        return next(self.search_results)

    def convertImage(self, buffer_number):
        self.events.append(f"convert:{buffer_number}")

    def compareCharacteristics(self):
        self.events.append("compare")
        return 18

    def createTemplate(self):
        self.events.append("create")
        return True

    def storeTemplate(self, positionNumber=-1, charBufferNumber=0x01):
        self.events.append("store")
        return 6

    def deleteTemplate(self, position_number):
        self.events.append(f"delete:{position_number}")


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

    def test_reads_primary_plural_and_legacy_fingerprint_positions(self):
        positions = self.module.fingerprint_positions(
            {
                "huellas_entrada_ids": [5, "6", 5],
                "huella_entrada_id": 7,
                "huella_id": "8",
            },
            "entrada",
        )

        self.assertEqual(positions, [5, 6, 7, 8])

    def test_uses_two_independent_samples_and_stores_one_template(self):
        sensor = FakeEnrollmentSensor()
        assigned = []
        capture_attempts = []
        original_functions = (
            self.module.enqueue_voice,
            self.module.capture_fingerprint_sample,
            self.module.request_finger_removal,
            self.module.update_fingerprint_command,
            self.module.assign_fingerprint_to_person,
            self.module.fetch_person,
            self.module.wait_for_finger,
        )
        self.module.enqueue_voice = lambda *_args: None

        def capture_with_two_messy_images(sensor_arg, buffer_number, *_args):
            capture_attempts.append(True)
            if len(capture_attempts) < 3:
                raise RuntimeError("The image is too messy")
            sensor_arg.convertImage(buffer_number)

        self.module.capture_fingerprint_sample = capture_with_two_messy_images
        self.module.request_finger_removal = lambda *_args: None
        self.module.update_fingerprint_command = lambda *_args, **_kwargs: True
        self.module.assign_fingerprint_to_person = (
            lambda person_id, direction, position:
            assigned.append((person_id, direction, position))
        )
        self.module.fetch_person = lambda _person_id: {"huella_salida_id": 3}
        self.module.wait_for_finger = lambda *_args, **_kwargs: True
        try:
            self.module.enroll_fingerprint(
                sensor,
                {"id": "command-1", "personaId": 123},
                "salida",
            )
        finally:
            (
                self.module.enqueue_voice,
                self.module.capture_fingerprint_sample,
                self.module.request_finger_removal,
                self.module.update_fingerprint_command,
                self.module.assign_fingerprint_to_person,
                self.module.fetch_person,
                self.module.wait_for_finger,
            ) = original_functions

        self.assertEqual(len(capture_attempts), 4)
        self.assertEqual(
            sensor.events,
            [
                "convert:1",
                "search",
                "convert:2",
                "compare",
                "create",
                "search",
                "store",
                "search",
            ],
        )
        self.assertEqual(assigned, [("123", "salida", 6)])

if __name__ == "__main__":
    unittest.main()
