#!/usr/bin/env python3
"""Control físico del torniquete UAC para Raspberry Pi."""

import os
import fcntl
import queue
import signal
import subprocess
import tempfile
import threading
import time

import evdev
from gtts import gTTS
from pyfingerprint.pyfingerprint import PyFingerprint
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import RPi.GPIO as GPIO

from qr_input import QrKeyboardBuffer


URL_VALIDAR = "https://torniquete-system.onrender.com/validar"
URL_FIREBASE = (
    "https://torniquete-universidad-default-rtdb.firebaseio.com/usuarios"
)
QR_DEVICE = (
    "/dev/input/by-id/"
    "usb-BF_SCAN_SCAN_KEYBOARD_A-00000-event-kbd"
)

RELE_ENTRADA = 17
RELE_SALIDA = 27
AUDIO_DEVICE = "hw:2,0"
AUDIO_CARD = "2"
AUDIO_MIXER_LEVEL = "95%"
REQUEST_TIMEOUT = (4, 35)
QR_DEDUP_SECONDS = 3
LOG_FULL_QR = os.environ.get("TORNIQUETE_LOG_FULL_QR") == "1"

stop_event = threading.Event()
voice_queue = queue.Queue(maxsize=10)
qr_queue = queue.Queue(maxsize=5)
relay_locks = {
    RELE_ENTRADA: threading.Lock(),
    RELE_SALIDA: threading.Lock(),
}

users_cache = {"expires_at": 0.0, "data": {}}
users_cache_lock = threading.Lock()
instance_lock_file = None


def acquire_instance_lock():
    """Impide que una ejecución manual compita con el servicio systemd."""
    global instance_lock_file
    instance_lock_file = open(
        "/tmp/torniquete-uac.lock",
        "w",
        encoding="utf-8",
    )
    try:
        fcntl.flock(
            instance_lock_file.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        print(
            "El torniquete ya está activo mediante torniquete.service. "
            "Use: sudo systemctl status torniquete",
            flush=True,
        )
        instance_lock_file.close()
        instance_lock_file = None
        return False

    instance_lock_file.write(str(os.getpid()))
    instance_lock_file.flush()
    return True


def create_http_session():
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=0.25,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PATCH"}),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session = requests.Session()
    session.mount("https://", adapter)
    return session


http = create_http_session()


def setup_gpio():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELE_ENTRADA, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(RELE_SALIDA, GPIO.OUT, initial=GPIO.HIGH)


def configure_audio():
    """Fija un volumen alto sin llevar el mezclador al máximo con distorsión."""
    try:
        result = subprocess.run(
            [
                "amixer",
                "-q",
                "-c",
                AUDIO_CARD,
                "sset",
                "PCM",
                AUDIO_MIXER_LEVEL,
                "unmute",
            ],
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            print("No fue posible ajustar el volumen PCM", flush=True)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Error ajustando volumen: {error}", flush=True)


def activate_relay(pin, label):
    with relay_locks[pin]:
        try:
            print(f"ABRIENDO {label}...", flush=True)
            GPIO.output(pin, GPIO.LOW)
            stop_event.wait(1)
        finally:
            GPIO.output(pin, GPIO.HIGH)


def open_entry():
    activate_relay(RELE_ENTRADA, "ENTRADA")


def open_exit():
    activate_relay(RELE_SALIDA, "SALIDA")


def enqueue_voice(text):
    try:
        voice_queue.put_nowait(text)
    except queue.Full:
        print("Cola de voz llena; aviso omitido", flush=True)


def voice_worker():
    while not stop_event.is_set():
        try:
            text = voice_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        audio_path = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="voz_torniquete_",
                suffix=".mp3",
                delete=False,
            ) as temporary:
                audio_path = temporary.name

            gTTS(text=text, lang="es").save(audio_path)
            subprocess.run(
                [
                    "mpg123",
                    "-q",
                    "-f",
                    "65536",
                    "-a",
                    AUDIO_DEVICE,
                    audio_path,
                ],
                check=False,
                timeout=20,
            )
        except Exception as error:
            print(f"Error de voz: {error}", flush=True)
        finally:
            if audio_path:
                try:
                    os.remove(audio_path)
                except FileNotFoundError:
                    pass
            voice_queue.task_done()


def safe_response_json(response):
    try:
        return response.json()
    except ValueError:
        return {}


def process_authorized_access(data):
    person = data.get("persona") or {}
    person_id = person.get("personaId", "")
    name = (person.get("nombre") or f"usuario {person_id}").strip()
    movement = data.get("tipo")

    if movement == "entrada":
        threading.Thread(target=open_entry, daemon=True).start()
        enqueue_voice(f"Bienvenido {name}")
    elif movement == "salida":
        threading.Thread(target=open_exit, daemon=True).start()
        enqueue_voice(f"Nos vemos pronto {name}")
    else:
        print("Respuesta autorizada sin tipo de movimiento", flush=True)
        enqueue_voice("Acceso autorizado")

    print(
        f"ACCESO OK tipo={movement} personaId={person_id} nombre={name}",
        flush=True,
    )


def validate_qr(token):
    """Envía exactamente el token leído; la Raspberry nunca descifra."""
    try:
        response = http.post(
            URL_VALIDAR,
            json={"token": token},
            timeout=REQUEST_TIMEOUT,
        )
        data = safe_response_json(response)

        if response.ok and data.get("ok"):
            process_authorized_access(data)
            return

        error_code = data.get("error", "TOKEN_INVALIDO")
        print(
            f"QR DENEGADO status={response.status_code} error={error_code}",
            flush=True,
        )
        if error_code == "TOKEN_EXPIRADO":
            enqueue_voice("El código QR ya expiró")
        elif error_code == "TOKEN_YA_UTILIZADO":
            enqueue_voice("Este código QR ya fue utilizado")
        else:
            enqueue_voice("Acceso denegado")
    except requests.RequestException as error:
        print(f"Error comunicando con backend: {error}", flush=True)
        enqueue_voice("No hay conexión con el servidor")


def qr_validation_worker():
    while not stop_event.is_set():
        try:
            token = qr_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            validate_qr(token)
        finally:
            qr_queue.task_done()


def qr_reader_worker():
    last_token = None
    last_token_at = 0.0

    while not stop_event.is_set():
        device = None
        try:
            device = evdev.InputDevice(QR_DEVICE)
            device.grab()
            keyboard_buffer = QrKeyboardBuffer()
            print(f"Lector QR listo: {device.path}", flush=True)

            for event in device.read_loop():
                if stop_event.is_set():
                    return
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                key_event = evdev.categorize(event)
                token = keyboard_buffer.feed(
                    key_event.keycode,
                    key_event.keystate,
                )
                if not token:
                    continue

                now = time.monotonic()
                if token == last_token and now - last_token_at < QR_DEDUP_SECONDS:
                    continue
                last_token = token
                last_token_at = now

                print(
                    f"QR recibido longitud={len(token)} final={token[-6:]}",
                    flush=True,
                )
                if LOG_FULL_QR:
                    print(f"QR_DIAGNOSTICO={token}", flush=True)
                try:
                    qr_queue.put_nowait(token)
                except queue.Full:
                    print("Cola QR llena; lectura omitida", flush=True)
        except (FileNotFoundError, OSError) as error:
            print(f"Reconectando lector QR: {error}", flush=True)
            stop_event.wait(2)
        finally:
            if device is not None:
                try:
                    device.ungrab()
                except OSError:
                    pass
                device.close()


def fetch_users(force=False):
    now = time.monotonic()
    with users_cache_lock:
        if not force and users_cache["expires_at"] > now:
            return users_cache["data"]

    try:
        response = http.get(f"{URL_FIREBASE}.json", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json() or {}
        if not isinstance(data, dict):
            data = {}
        with users_cache_lock:
            users_cache["data"] = data
            users_cache["expires_at"] = now + 10
        return data
    except (requests.RequestException, ValueError) as error:
        print(f"Error consultando usuarios: {error}", flush=True)
        with users_cache_lock:
            return users_cache["data"]


def find_person_by_fingerprint(fingerprint_id):
    for person_id, user in fetch_users().items():
        if str(user.get("huella_id")) == str(fingerprint_id):
            return str(person_id), user
    return None, None


def update_firebase_state(person_id, state):
    try:
        response = http.patch(
            f"{URL_FIREBASE}/{person_id}.json",
            json={
                "estado": state,
                "ultimoMovimiento": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z"
                ),
                "ultimoMovimientoTipo": (
                    "entrada" if state == "dentro" else "salida"
                ),
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        with users_cache_lock:
            users_cache["expires_at"] = 0
        return True
    except requests.RequestException as error:
        print(f"Error actualizando estado: {error}", flush=True)
        return False


def process_fingerprint(person_id, user, direction):
    name = (user.get("nombre") or f"usuario {person_id}").strip()
    if direction == "entrada":
        if update_firebase_state(person_id, "dentro"):
            threading.Thread(target=open_entry, daemon=True).start()
            enqueue_voice(f"Bienvenido {name}")
    else:
        if update_firebase_state(person_id, "fuera"):
            threading.Thread(target=open_exit, daemon=True).start()
            enqueue_voice(f"Nos vemos pronto {name}")


def initialize_fingerprint_sensor(path, name):
    while not stop_event.is_set():
        try:
            sensor = PyFingerprint(
                path,
                57600,
                0xFFFFFFFF,
                0x00000000,
            )
            if sensor.verifyPassword():
                print(f"{name} listo en {path}", flush=True)
                return sensor
        except Exception as error:
            print(f"Reintentando {name}: {error}", flush=True)
        stop_event.wait(2)
    return None


def fingerprint_worker(path, name, direction):
    while not stop_event.is_set():
        sensor = initialize_fingerprint_sensor(path, name)
        if sensor is None:
            return
        try:
            while not stop_event.is_set():
                if not sensor.readImage():
                    stop_event.wait(0.08)
                    continue

                sensor.convertImage(0x01)
                position, accuracy = sensor.searchTemplate()
                if position >= 0 and accuracy > 50:
                    print(
                        f"HUELLA {direction.upper()} "
                        f"id={position} precision={accuracy}",
                        flush=True,
                    )
                    person_id, user = find_person_by_fingerprint(position)
                    if person_id:
                        process_fingerprint(person_id, user, direction)
                    else:
                        enqueue_voice("Huella no registrada")
                    stop_event.wait(2)
        except Exception as error:
            print(f"Reiniciando {name}: {error}", flush=True)
            stop_event.wait(1)


def request_shutdown(signum, _frame):
    print(f"Apagando sistema por señal {signum}", flush=True)
    stop_event.set()


def main():
    if not acquire_instance_lock():
        return
    setup_gpio()
    configure_audio()
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    workers = [
        threading.Thread(target=voice_worker, name="voice", daemon=True),
        threading.Thread(
            target=qr_validation_worker,
            name="qr-validation",
            daemon=True,
        ),
        threading.Thread(target=qr_reader_worker, name="qr-reader", daemon=True),
        threading.Thread(
            target=fingerprint_worker,
            args=("/dev/serial0", "Sensor Entrada", "entrada"),
            name="fingerprint-entry",
            daemon=True,
        ),
        threading.Thread(
            target=fingerprint_worker,
            args=("/dev/ttyUSB0", "Sensor Salida", "salida"),
            name="fingerprint-exit",
            daemon=True,
        ),
    ]
    for worker in workers:
        worker.start()

    print("SISTEMA ACTIVO (QR + HUELLA + VOZ + RELÉS)", flush=True)
    try:
        while not stop_event.wait(1):
            pass
    finally:
        GPIO.output(RELE_ENTRADA, GPIO.HIGH)
        GPIO.output(RELE_SALIDA, GPIO.HIGH)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
