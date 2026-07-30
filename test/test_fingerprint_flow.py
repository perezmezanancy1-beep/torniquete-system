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
        self.readings = iter([False, True, True, False, False, True])

    def readImage(self):
        result = next(self.readings)
        self.events.append(f"read:{result}")
        return result

    def convertImage(self, buffer_number):
        self.events.append(f"convert:{buffer_number}")

    def createTemplate(self):
        self.events.append("create")
        return True

    def storeTemplate(self, positionNumber=-1, charBufferNumber=0x01):
        self.events.append(f"store:{positionNumber}")
        return positionNumber


class FakeSyncSensor:
    def __init__(self, characteristics):
        self.events = []
        self.characteristics = list(characteristics)

    def downloadCharacteristics(self, charBufferNumber=0x01):
        self.events.append(f"download:{charBufferNumber}")
        return list(self.characteristics)

    def uploadCharacteristics(self, char_buffer, characteristics):
        self.events.append(f"upload:{char_buffer}")
        self.characteristics = list(characteristics)
        return True

    def storeTemplate(self, positionNumber=-1, charBufferNumber=0x01):
        self.events.append(f"store:{positionNumber}:{charBufferNumber}")
        return 12

    def loadTemplate(self, position_number, char_buffer):
        self.events.append(f"load:{position_number}:{char_buffer}")
        return True

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

    def test_syncs_only_the_template_created_by_the_current_registration(self):
        positions = self.module.fingerprint_positions_to_sync(
            {"huellaId": 9, "huellaIds": [9]},
            {
                "huellas_entrada_ids": [5, 6, 9],
                "huella_entrada_id": 9,
            },
        )

        self.assertEqual(positions, [9])

    def test_fast_sync_stores_and_verifies_using_the_open_sensor(self):
        characteristics = [index % 256 for index in range(512)]
        sensor = FakeSyncSensor(characteristics)

        position = self.module.store_synced_characteristics(
            sensor,
            characteristics,
        )

        self.assertEqual(position, 12)
        self.assertEqual(
            sensor.events,
            [
                "upload:1",
                "store:-1:1",
                "load:12:1",
                "download:1",
            ],
        )

    def test_preserves_the_original_enrollment_sequence(self):
        sensor = FakeEnrollmentSensor()
        original_functions = (
            self.module.enqueue_voice,
            self.module.update_fingerprint_command,
            self.module.time.sleep,
        )
        self.module.enqueue_voice = lambda *_args: None
        self.module.update_fingerprint_command = lambda *_args, **_kwargs: True
        self.module.time.sleep = lambda *_args: None
        try:
            position = self.module.register_fingerprint_with_original_flow(
                sensor,
                "command-1",
                "entrada",
                6,
            )
        finally:
            (
                self.module.enqueue_voice,
                self.module.update_fingerprint_command,
                self.module.time.sleep,
            ) = original_functions

        self.assertEqual(
            sensor.events,
            [
                "read:False",
                "read:True",
                "convert:1",
                "read:True",
                "read:False",
                "read:False",
                "read:True",
                "convert:2",
                "create",
                "store:6",
            ],
        )
        self.assertEqual(position, 6)

if __name__ == "__main__":
    unittest.main()
