#!/usr/bin/env python3
"""Control físico del torniquete UAC para Raspberry Pi."""

import os
import fcntl
import gc
import hashlib
import queue
import signal
import subprocess
import threading
import time
import wave

import evdev
from pyfingerprint.pyfingerprint import PyFingerprint
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import RPi.GPIO as GPIO

from qr_input import QrKeyboardBuffer

PiperVoice = None
SynthesisConfig = None
piper_import_attempted = False


URL_VALIDAR = "https://torniquete-system.onrender.com/validar"
URL_HEALTH = "https://torniquete-system.onrender.com/health"
URL_FIREBASE = (
    "https://torniquete-universidad-default-rtdb.firebaseio.com/usuarios"
)
URL_FINGERPRINT_CONTROL = (
    "https://torniquete-universidad-default-rtdb.firebaseio.com/"
    "controlHuella/actual"
)
QR_DEVICE = (
    "/dev/input/by-id/"
    "usb-BF_SCAN_SCAN_KEYBOARD_A-00000-event-kbd"
)
FINGERPRINT_ENTRY_DEVICE = "/dev/serial0"
FINGERPRINT_EXIT_DEVICE = "/dev/ttyUSB0"

RELE_ENTRADA = 17
RELE_SALIDA = 27
AUDIO_DEVICE = "hw:2,0"
AUDIO_CARD = "2"
AUDIO_MIXER_LEVEL = "95%"
PIPER_MODEL = "/home/pirb/voices/es_MX-claude-high.onnx"
PIPER_FALLBACK_MODEL = "/home/pirb/voices/es_MX-ald-medium.onnx"
VOICE_CACHE_DIR = "/home/pirb/.torniquete_voice_cache"
REQUEST_TIMEOUT = (4, 35)
QR_DEDUP_SECONDS = 3
FINGERPRINT_ENROLL_TIMEOUT_SECONDS = 35
FINGERPRINT_SECURITY_LEVEL = 1
FINGERPRINT_MATCH_MIN_SCORE = 1
FINGERPRINT_STABLE_PRESENT_READS = 2
FINGERPRINT_STABLE_ABSENT_READS = 2
FINGERPRINT_ENROLL_CAPTURE_ATTEMPTS = 3
LOG_FULL_QR = os.environ.get("TORNIQUETE_LOG_FULL_QR") == "1"

stop_event = threading.Event()
voice_queue = queue.Queue(maxsize=10)
qr_queue = queue.Queue(maxsize=5)
fingerprint_enrollment_queues = {
    "entrada": queue.Queue(maxsize=1),
    "salida": queue.Queue(maxsize=1),
}
relay_locks = {
    RELE_ENTRADA: threading.Lock(),
    RELE_SALIDA: threading.Lock(),
}

users_cache = {"expires_at": 0.0, "data": {}}
users_cache_lock = threading.Lock()
fingerprint_commands_seen = set()
fingerprint_commands_lock = threading.Lock()
fingerprint_no_match_last = {"entrada": 0.0, "salida": 0.0}
instance_lock_file = None
active_voice_model = None


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


def load_neural_voice():
    global active_voice_model
    global PiperVoice
    global SynthesisConfig
    global piper_import_attempted
    if not piper_import_attempted:
        piper_import_attempted = True
        try:
            from piper import PiperVoice as PiperVoiceClass
            from piper import SynthesisConfig as SynthesisConfigClass
            PiperVoice = PiperVoiceClass
            SynthesisConfig = SynthesisConfigClass
        except ImportError:
            PiperVoice = None
            SynthesisConfig = None
    if PiperVoice is None:
        print("Voz neuronal no disponible; usando voz de respaldo", flush=True)
        return None
    for model_path in (PIPER_MODEL, PIPER_FALLBACK_MODEL):
        if not os.path.isfile(model_path):
            continue
        try:
            voice = PiperVoice.load(model_path, use_cuda=False)
            active_voice_model = model_path
            print(
                "Voz neuronal en español lista: "
                f"{os.path.basename(model_path)}",
                flush=True,
            )
            return voice
        except Exception as error:
            print(
                "No fue posible cargar "
                f"{os.path.basename(model_path)}: {error}",
                flush=True,
            )
    print("Voz neuronal no disponible; usando voz de respaldo", flush=True)
    active_voice_model = "espeak-ng"
    return None


def voice_cache_path(text):
    cache_key = f"{active_voice_model or PIPER_MODEL}\0{text}"
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return os.path.join(VOICE_CACHE_DIR, f"{digest}.wav")


def synthesize_voice(text, audio_path, neural_voice):
    if neural_voice is not None:
        synthesis_config = SynthesisConfig(
            volume=1.15,
            length_scale=0.98,
            noise_scale=0.667,
            noise_w_scale=0.8,
            normalize_audio=True,
        )
        with wave.open(audio_path, "wb") as wav_file:
            neural_voice.synthesize_wav(
                text,
                wav_file,
                syn_config=synthesis_config,
            )
        return

    subprocess.run(
        [
            "espeak-ng",
            "-v",
            "es-419",
            "-s",
            "155",
            "-p",
            "48",
            "-a",
            "180",
            "-w",
            audio_path,
            text,
        ],
        check=True,
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def normalize_spoken_name(name):
    return " ".join(str(name).lower().title().split())


def voice_worker():
    os.makedirs(VOICE_CACHE_DIR, exist_ok=True)
    neural_voice = load_neural_voice()

    while not stop_event.is_set():
        try:
            text = voice_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        audio_path = voice_cache_path(text)
        try:
            if not os.path.isfile(audio_path):
                temporary_path = f"{audio_path}.tmp.wav"
                synthesize_voice(text, temporary_path, neural_voice)
                os.replace(temporary_path, audio_path)
            subprocess.run(
                ["aplay", "-q", "-D", AUDIO_DEVICE, audio_path],
                check=False,
                timeout=15,
            )
        except Exception as error:
            print(f"Error de voz: {error}", flush=True)
        finally:
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
    spoken_name = normalize_spoken_name(name)
    movement = data.get("tipo")

    if movement == "entrada":
        threading.Thread(target=open_entry, daemon=True).start()
        enqueue_voice(f"Bienvenido, {spoken_name}")
    elif movement == "salida":
        threading.Thread(target=open_exit, daemon=True).start()
        enqueue_voice(f"Nos vemos pronto, {spoken_name}")
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
        elif error_code == "MOVIMIENTO_RECIENTE":
            movement = data.get("tipo")
            if movement == "entrada":
                enqueue_voice("La entrada ya fue registrada")
            elif movement == "salida":
                enqueue_voice("La salida ya fue registrada")
            else:
                enqueue_voice("El movimiento ya fue registrado")
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


def backend_keepalive_worker():
    while not stop_event.is_set():
        try:
            response = http.get(URL_HEALTH, timeout=(4, 10))
            if not response.ok:
                print(
                    f"Backend health status={response.status_code}",
                    flush=True,
                )
        except requests.RequestException as error:
            print(f"Backend temporalmente no disponible: {error}", flush=True)
        stop_event.wait(240)


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


def fetch_person(person_id):
    response = http.get(
        f"{URL_FIREBASE}/{person_id}.json",
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    user = response.json() or {}
    if not isinstance(user, dict):
        raise ValueError("Respuesta de usuario inválida")
    return user


def fingerprint_field(direction):
    return (
        "huella_entrada_id"
        if direction == "entrada"
        else "huella_salida_id"
    )


def fingerprint_list_field(direction):
    return (
        "huellas_entrada_ids"
        if direction == "entrada"
        else "huellas_salida_ids"
    )


def fingerprint_positions(user, direction):
    positions = []
    plural = user.get(fingerprint_list_field(direction))
    if isinstance(plural, list):
        positions.extend(plural)
    primary = user.get(fingerprint_field(direction))
    if primary is not None:
        positions.append(primary)
    legacy = user.get("huella_id")
    if legacy is not None:
        positions.append(legacy)

    unique = []
    for position in positions:
        try:
            normalized = int(position)
        except (TypeError, ValueError):
            continue
        if normalized not in unique:
            unique.append(normalized)
    return unique


def fingerprint_positions_to_sync(command, user):
    requested_positions = command.get("huellaIds")
    if not isinstance(requested_positions, list):
        requested_positions = [command.get("huellaId")]

    positions = []
    for requested_position in requested_positions:
        try:
            requested_position = int(requested_position)
        except (TypeError, ValueError):
            continue
        if requested_position >= 0 and requested_position not in positions:
            positions.append(requested_position)
    return positions or fingerprint_positions(user, "entrada")


def find_person_by_fingerprint(fingerprint_id, direction):
    for person_id, user in fetch_users().items():
        if int(fingerprint_id) in fingerprint_positions(user, direction):
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
    spoken_name = normalize_spoken_name(name)
    if direction == "entrada":
        if update_firebase_state(person_id, "dentro"):
            threading.Thread(target=open_entry, daemon=True).start()
            enqueue_voice(f"Bienvenido, {spoken_name}")
    else:
        if update_firebase_state(person_id, "fuera"):
            threading.Thread(target=open_exit, daemon=True).start()
            enqueue_voice(f"Nos vemos pronto, {spoken_name}")


def initialize_fingerprint_sensor(path, name):
    while not stop_event.is_set():
        try:
            sensor = connect_fingerprint_sensor(path)
            print(
                f"{name} listo en {path} "
                f"sensibilidad={FINGERPRINT_SECURITY_LEVEL}",
                flush=True,
            )
            return sensor
        except Exception as error:
            print(f"Reintentando {name}: {error}", flush=True)
        stop_event.wait(2)
    return None


def connect_fingerprint_sensor(path):
    sensor = PyFingerprint(
        path,
        57600,
        0xFFFFFFFF,
        0x00000000,
    )
    if not sensor.verifyPassword():
        raise RuntimeError(f"Contraseña inválida para el sensor {path}")
    if sensor.getSecurityLevel() != FINGERPRINT_SECURITY_LEVEL:
        sensor.setSecurityLevel(FINGERPRINT_SECURITY_LEVEL)
    return sensor


def close_fingerprint_sensor(sensor):
    if sensor is None:
        return
    try:
        serial_port = getattr(sensor, "_PyFingerprint__serial", None)
        if serial_port is not None and serial_port.isOpen():
            serial_port.close()
    except Exception:
        pass


def store_synced_characteristics(sensor, characteristics):
    """Guarda y verifica una plantilla usando una conexión ya abierta."""
    characteristics = list(characteristics)
    source_digest = hashlib.sha256(bytes(characteristics)).digest()
    stored_position = None
    try:
        # pyfingerprint vuelve a descargar lo recién subido internamente.
        # Devolver la misma carga evita una transferencia serial redundante.
        original_download = sensor.downloadCharacteristics
        sensor.downloadCharacteristics = (
            lambda charBufferNumber=0x01: list(characteristics)
        )
        try:
            uploaded = sensor.uploadCharacteristics(
                0x01,
                characteristics,
            )
        finally:
            sensor.downloadCharacteristics = original_download
        if not uploaded:
            raise RuntimeError("El lector de salida no recibió la plantilla")

        stored_position = sensor.storeTemplate(
            positionNumber=-1,
            charBufferNumber=0x01,
        )
        if not sensor.loadTemplate(stored_position, 0x01):
            raise RuntimeError("No se pudo verificar la huella de salida")
        stored = sensor.downloadCharacteristics(0x01)
        if hashlib.sha256(bytes(stored)).digest() != source_digest:
            raise RuntimeError(
                "La huella guardada en salida no coincide con la original"
            )
        return int(stored_position)
    except Exception:
        if stored_position is not None:
            try:
                sensor.deleteTemplate(stored_position)
            except Exception:
                pass
        raise


def copy_entry_template_to_exit(entry_position):
    """Copia una plantilla entre sensores sin sacarla de la Raspberry."""
    entry_sensor = None
    exit_sensor = None
    stored_position = None
    started_at = time.monotonic()
    try:
        entry_sensor = connect_fingerprint_sensor(FINGERPRINT_ENTRY_DEVICE)
        if not entry_sensor.loadTemplate(int(entry_position), 0x01):
            raise RuntimeError("No se pudo cargar la huella de entrada")
        characteristics = entry_sensor.downloadCharacteristics(0x01)
        close_fingerprint_sensor(entry_sensor)
        entry_sensor = None
        gc.collect()

        exit_sensor = connect_fingerprint_sensor(FINGERPRINT_EXIT_DEVICE)
        stored_position = store_synced_characteristics(
            exit_sensor,
            characteristics,
        )
        print(
            "PLANTILLA TRANSFERIDA "
            f"entrada={entry_position} salida={stored_position} "
            f"segundos={time.monotonic() - started_at:.2f}",
            flush=True,
        )
        return int(stored_position)
    except Exception:
        if stored_position is not None and exit_sensor is not None:
            try:
                exit_sensor.deleteTemplate(stored_position)
            except Exception:
                pass
        raise
    finally:
        close_fingerprint_sensor(entry_sensor)
        close_fingerprint_sensor(exit_sensor)
        gc.collect()


def sync_fingerprint_in_open_exit_sensor(sensor, command):
    command_id = str(command.get("id") or "")
    person_id = str(command.get("personaId") or "")
    entry_position = int(command.get("huellaId"))
    characteristics = command.get("_syncCharacteristics")
    started_at = time.monotonic()
    if not command_id or not person_id.isdigit() or not characteristics:
        raise ValueError("Datos incompletos para sincronizar la huella")

    stored_position = store_synced_characteristics(
        sensor,
        characteristics,
    )
    try:
        assign_fingerprint_to_person(
            person_id,
            "salida",
            stored_position,
        )
        if not update_fingerprint_command(
            command_id,
            estado="completado",
            lector="salida",
            mensaje="Huella registrada en entrada y salida.",
            huellaId=int(stored_position),
            huellaIds=[int(stored_position)],
        ):
            raise RuntimeError(
                "La plantilla se copió, pero no se confirmó en Firebase"
            )
    except Exception:
        sensor.deleteTemplate(stored_position)
        raise

    print(
        "SINCRONIZACION DIRECTA COMPLETADA "
        f"persona={person_id} entrada={entry_position} "
        f"salida={stored_position} "
        f"segundos={time.monotonic() - started_at:.2f}",
        flush=True,
    )
    enqueue_voice("Huella registrada correctamente")
    return int(stored_position)


def delete_exit_template(position):
    sensor = None
    try:
        sensor = connect_fingerprint_sensor(FINGERPRINT_EXIT_DEVICE)
        sensor.deleteTemplate(int(position))
    except Exception as error:
        print(
            f"No se pudo retirar la plantilla de salida {position}: {error}",
            flush=True,
        )
    finally:
        close_fingerprint_sensor(sensor)
        gc.collect()


def clear_fingerprint_sensor(path, label):
    last_error = None
    for attempt in range(1, 4):
        sensor = None
        try:
            sensor = connect_fingerprint_sensor(path)
            sensor.clearDatabase()
            remaining = int(sensor.getTemplateCount())
            if remaining != 0:
                raise RuntimeError(
                    f"{label} conserva {remaining} plantillas"
                )
            print(f"HUELLAS BORRADAS {label} intento={attempt}", flush=True)
            return
        except Exception as error:
            last_error = error
            print(
                f"Reintentando borrado {label} intento={attempt}: {error}",
                flush=True,
            )
            time.sleep(0.4)
        finally:
            close_fingerprint_sensor(sensor)
            gc.collect()
    raise RuntimeError(f"No se pudo limpiar {label}: {last_error}")


def clear_fingerprint_assignments():
    response = http.get(f"{URL_FIREBASE}.json", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    users = response.json() or {}
    updates = {}
    fields = (
        "huella_id",
        "huella_entrada_id",
        "huella_salida_id",
        "huellas_entrada_ids",
        "huellas_salida_ids",
    )
    for person_id, user in users.items():
        if not isinstance(user, dict):
            continue
        for field in fields:
            if field in user:
                updates[f"{person_id}/{field}"] = None
    if updates:
        response = http.patch(
            f"{URL_FIREBASE}.json",
            json=updates,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    with users_cache_lock:
        users_cache["data"] = {}
        users_cache["expires_at"] = 0
    return len(updates)


def complete_pending_fingerprint_reset():
    try:
        response = http.get(
            f"{URL_FINGERPRINT_CONTROL}.json",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        command = response.json() or {}
        if (
            command.get("estado") != "borrando"
            or command.get("accion") != "borrar_todas"
        ):
            return

        command_id = str(command.get("id") or "")
        if not command_id:
            return
        print("INICIANDO BORRADO TOTAL DE HUELLAS", flush=True)
        clear_fingerprint_sensor(
            FINGERPRINT_ENTRY_DEVICE,
            "ENTRADA",
        )
        clear_fingerprint_sensor(
            FINGERPRINT_EXIT_DEVICE,
            "SALIDA",
        )
        cleared_fields = clear_fingerprint_assignments()
        update_fingerprint_command(
            command_id,
            estado="completado",
            mensaje=(
                "Todas las huellas fueron eliminadas de ambos lectores "
                "y de Firebase."
            ),
            camposEliminados=cleared_fields,
        )
        print(
            f"BORRADO TOTAL COMPLETADO campos={cleared_fields}",
            flush=True,
        )
        enqueue_voice("Todas las huellas fueron eliminadas")
    except Exception as error:
        command_id = locals().get("command_id", "")
        print(f"ERROR BORRANDO TODAS LAS HUELLAS: {error}", flush=True)
        if command_id:
            update_fingerprint_command(
                command_id,
                estado="error",
                mensaje=(
                    "No fue posible limpiar completamente ambos lectores. "
                    "Intente nuevamente."
                ),
            )


def complete_pending_fingerprint_sync():
    """Completa una sincronización pendiente antes de abrir los lectores."""
    try:
        response = http.get(
            f"{URL_FINGERPRINT_CONTROL}.json",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        command = response.json() or {}
        if (
            command.get("estado") != "sincronizando"
            or str(command.get("lector") or "").lower() != "salida"
        ):
            return

        command_id = str(command.get("id") or "")
        person_id = str(command.get("personaId") or "")
        expiration = int(command.get("expiraEpochMs") or 0)
        if not command_id or not person_id.isdigit():
            return
        if expiration and int(time.time() * 1000) > expiration:
            update_fingerprint_command(
                command_id,
                estado="error",
                mensaje="La solicitud de sincronización expiró.",
            )
            return

        user_response = http.get(
            f"{URL_FIREBASE}/{person_id}.json",
            timeout=REQUEST_TIMEOUT,
        )
        user_response.raise_for_status()
        user = user_response.json() or {}
        exit_positions = fingerprint_positions(user, "salida")
        if exit_positions and not command.get("forzarSincronizacion"):
            update_fingerprint_command(
                command_id,
                estado="completado",
                mensaje="Huella sincronizada con el lector de salida.",
                huellaId=int(exit_positions[-1]),
            )
            return

        entry_positions = fingerprint_positions_to_sync(command, user)
        if not entry_positions:
            update_fingerprint_command(
                command_id,
                estado="error",
                mensaje=(
                    "Registre primero la huella en el lector de entrada."
                ),
            )
            return

        print(
            f"SINCRONIZANDO HUELLA persona={person_id} hacia salida",
            flush=True,
        )
        copied_positions = []
        try:
            for entry_position in entry_positions[-1:]:
                copied_positions.append(
                    copy_entry_template_to_exit(entry_position)
                )
            assign_fingerprint_to_person(
                person_id,
                "salida",
                copied_positions,
            )
        except Exception:
            for copied_position in copied_positions:
                delete_exit_template(copied_position)
            raise

        update_fingerprint_command(
            command_id,
            estado="completado",
            mensaje="Huella registrada en entrada y salida.",
            huellaId=int(copied_positions[-1]),
            huellaIds=copied_positions,
        )
        print(
            "HUELLA SINCRONIZADA "
            f"persona={person_id} salidas={copied_positions}",
            flush=True,
        )
        enqueue_voice("Huella registrada correctamente")
    except (requests.RequestException, ValueError) as error:
        print(f"No fue posible consultar la sincronización: {error}", flush=True)
    except Exception as error:
        command_id = locals().get("command_id", "")
        print(f"ERROR SINCRONIZANDO HUELLA: {error}", flush=True)
        if command_id:
            update_fingerprint_command(
                command_id,
                estado="error",
                mensaje=(
                    "No fue posible sincronizar el lector de salida. "
                    "Intente nuevamente."
                ),
            )


def update_fingerprint_command(command_id, **values):
    values["actualizado"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        current = http.get(
            f"{URL_FINGERPRINT_CONTROL}.json",
            timeout=REQUEST_TIMEOUT,
        )
        current.raise_for_status()
        current_data = current.json() or {}
        if str(current_data.get("id")) != str(command_id):
            return False
        response = http.patch(
            f"{URL_FINGERPRINT_CONTROL}.json",
            json=values,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except (requests.RequestException, ValueError) as error:
        print(f"Error actualizando comando de huella: {error}", flush=True)
        return False


def fingerprint_command_worker():
    while not stop_event.is_set():
        try:
            response = http.get(
                f"{URL_FINGERPRINT_CONTROL}.json",
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            command = response.json() or {}
            command_id = str(command.get("id") or "")
            direction = str(command.get("lector") or "").lower()
            expiration = int(command.get("expiraEpochMs") or 0)
            if (
                command.get("estado") == "pendiente"
                and command.get("accion") == "borrar_todas"
                and command_id
            ):
                if expiration and int(time.time() * 1000) > expiration:
                    update_fingerprint_command(
                        command_id,
                        estado="error",
                        mensaje="La solicitud de borrado expiró.",
                    )
                    stop_event.wait(0.75)
                    continue
                if update_fingerprint_command(
                    command_id,
                    estado="borrando",
                    mensaje=(
                        "Borrando huellas de entrada, salida y Firebase."
                    ),
                ):
                    print("REINICIO PARA BORRADO TOTAL DE HUELLAS", flush=True)
                    stop_event.set()
                    return
            if (
                command.get("estado") == "pendiente"
                and command_id
                and direction in fingerprint_enrollment_queues
            ):
                if expiration and int(time.time() * 1000) > expiration:
                    update_fingerprint_command(
                        command_id,
                        estado="error",
                        mensaje=(
                            "La solicitud expiró antes de llegar al lector."
                        ),
                    )
                    stop_event.wait(0.75)
                    continue
                if direction == "salida":
                    person_id = str(command.get("personaId") or "")
                    user = fetch_person(person_id)
                    exit_position = user.get("huella_salida_id")
                    entry_position = user.get("huella_entrada_id")
                    if exit_position is not None:
                        update_fingerprint_command(
                            command_id,
                            estado="completado",
                            mensaje=(
                                "La huella ya está disponible en el "
                                "lector de salida."
                            ),
                            huellaId=int(exit_position),
                        )
                        stop_event.wait(0.75)
                        continue
                    if entry_position is None:
                        update_fingerprint_command(
                            command_id,
                            estado="error",
                            mensaje=(
                                "Registre primero la huella en el lector "
                                "de entrada; luego se sincronizará con salida."
                            ),
                        )
                        enqueue_voice(
                            "Registre primero la huella en el lector de entrada"
                        )
                        stop_event.wait(0.75)
                        continue
                    if update_fingerprint_command(
                        command_id,
                        estado="sincronizando",
                        mensaje=(
                            "Sincronizando la huella con el lector de salida."
                        ),
                    ):
                        print(
                            "REINICIO PARA SINCRONIZAR HUELLA "
                            f"persona={person_id}",
                            flush=True,
                        )
                        stop_event.set()
                        return
                with fingerprint_commands_lock:
                    if command_id in fingerprint_commands_seen:
                        stop_event.wait(0.5)
                        continue
                    target_queue = fingerprint_enrollment_queues[direction]
                    try:
                        target_queue.put_nowait(command)
                    except queue.Full:
                        stop_event.wait(0.5)
                        continue
                    fingerprint_commands_seen.add(command_id)
                    print(
                        "COMANDO HUELLA "
                        f"id={command_id} persona={command.get('personaId')} "
                        f"lector={direction}",
                        flush=True,
                    )
        except (requests.RequestException, ValueError) as error:
            print(f"Control de huella no disponible: {error}", flush=True)
        stop_event.wait(0.75)


def wait_for_finger(
    sensor,
    present,
    timeout_seconds,
    stable_reads=None,
):
    if stable_reads is None:
        stable_reads = (
            FINGERPRINT_STABLE_PRESENT_READS
            if present
            else FINGERPRINT_STABLE_ABSENT_READS
        )
    deadline = time.monotonic() + timeout_seconds
    consecutive_reads = 0
    while not stop_event.is_set() and time.monotonic() < deadline:
        if bool(sensor.readImage()) == present:
            consecutive_reads += 1
            if consecutive_reads >= stable_reads:
                return True
        else:
            consecutive_reads = 0
        stop_event.wait(0.08)
    return False


def capture_fingerprint_sample(
    sensor,
    buffer_number,
    command_id,
    prompt,
    timeout_seconds=FINGERPRINT_ENROLL_TIMEOUT_SECONDS,
):
    update_fingerprint_command(
        command_id,
        estado="procesando",
        mensaje=prompt,
    )
    if not wait_for_finger(sensor, True, timeout_seconds):
        raise TimeoutError("Tiempo agotado esperando el dedo")
    sensor.convertImage(buffer_number)


def request_finger_removal(sensor, command_id):
    update_fingerprint_command(
        command_id,
        estado="procesando",
        mensaje="Retire completamente el dedo.",
    )
    enqueue_voice("Retire el dedo")
    if not wait_for_finger(sensor, False, 15):
        raise TimeoutError("No se retiró completamente el dedo")
    stop_event.wait(0.25)


def assign_fingerprint_to_person(person_id, direction, position):
    field = fingerprint_field(direction)
    list_field = fingerprint_list_field(direction)
    requested_positions = (
        position if isinstance(position, (list, tuple)) else [position]
    )
    requested_positions = list(dict.fromkeys(
        int(item) for item in requested_positions
    ))
    users = fetch_users(force=True)
    for existing_person_id, user in users.items():
        assigned_positions = fingerprint_positions(user, direction)
        if (
            set(assigned_positions).intersection(requested_positions)
            and str(existing_person_id) != str(person_id)
        ):
            raise RuntimeError(
                "La huella ya pertenece a otra persona en este lector"
            )

    current_user = users.get(str(person_id)) or {}
    positions = fingerprint_positions(current_user, direction)
    for requested_position in requested_positions:
        if requested_position not in positions:
            positions.append(requested_position)
    positions = positions[-3:]

    response = http.patch(
        f"{URL_FIREBASE}/{person_id}.json",
        json={
            field: int(requested_positions[-1]),
            list_field: positions,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    with users_cache_lock:
        users_cache["expires_at"] = 0


def register_fingerprint_with_original_flow(
    sensor,
    command_id,
    reader_label,
    fingerprint_id,
):
    """Ejecuta sin alterar la secuencia del registro original probado."""
    update_fingerprint_command(
        command_id,
        estado="procesando",
        mensaje=(
            f"Coloque el dedo en el lector de {reader_label} "
            "y manténgalo quieto."
        ),
    )
    enqueue_voice("Coloque el dedo y manténgalo quieto")

    while not sensor.readImage():
        time.sleep(0.1)

    sensor.convertImage(0x01)
    print("PRIMERA CAPTURA DE HUELLA", flush=True)
    update_fingerprint_command(
        command_id,
        estado="procesando",
        mensaje="Primera captura lista. Retire completamente el dedo.",
    )
    enqueue_voice("Retire el dedo")

    while sensor.readImage():
        time.sleep(0.1)

    update_fingerprint_command(
        command_id,
        estado="procesando",
        mensaje="Coloque nuevamente el mismo dedo.",
    )
    enqueue_voice("Coloque el mismo dedo una vez más")

    while not sensor.readImage():
        time.sleep(0.1)

    sensor.convertImage(0x02)
    sensor.createTemplate()
    stored_position = sensor.storeTemplate(int(fingerprint_id))
    print(
        "HUELLA CREADA CON FLUJO ORIGINAL "
        f"id={stored_position} lector={reader_label}",
        flush=True,
    )
    return int(stored_position)


def enroll_fingerprint(sensor, command, direction):
    command_id = str(command.get("id") or "")
    person_id = str(command.get("personaId") or "")
    reader_label = "entrada" if direction == "entrada" else "salida"
    if not command_id or not person_id.isdigit():
        return

    created_position = None
    try:
        position = int(sensor.getTemplateCount())
        print(
            f"ID ASIGNADO HUELLA persona={person_id} id={position}",
            flush=True,
        )
        position = register_fingerprint_with_original_flow(
            sensor,
            command_id,
            reader_label,
            position,
        )
        created_position = position

        assign_fingerprint_to_person(
            person_id,
            direction,
            position,
        )
        created_position = None

        if direction == "entrada":
            if not update_fingerprint_command(
                command_id,
                estado="sincronizando",
                lector="salida",
                forzarSincronizacion=True,
                mensaje=(
                    "Huella creada. Sincronizando el lector de salida."
                ),
                huellaId=int(position),
                huellaIds=[int(position)],
            ):
                raise RuntimeError(
                    "La huella se guardó, pero no se pudo iniciar "
                    "la sincronización de salida."
                )

            try:
                if not sensor.loadTemplate(position, 0x01):
                    raise RuntimeError(
                        "No se pudo preparar la plantilla de entrada"
                    )
                characteristics = sensor.downloadCharacteristics(0x01)
                local_sync_command = dict(command)
                local_sync_command.update(
                    {
                        "huellaId": int(position),
                        "huellaIds": [int(position)],
                        "_syncCharacteristics": characteristics,
                    }
                )
                fingerprint_enrollment_queues["salida"].put(
                    local_sync_command,
                    timeout=2,
                )
                print(
                    "SINCRONIZACION DIRECTA ENCOLADA "
                    f"persona={person_id} entrada={position}",
                    flush=True,
                )
            except Exception as sync_error:
                print(
                    "SINCRONIZACION DIRECTA NO DISPONIBLE; "
                    f"reiniciando como respaldo: {sync_error}",
                    flush=True,
                )
                stop_event.set()
                return

            enqueue_voice("Retire el dedo")
            wait_for_finger(sensor, False, 10)
            return

        update_fingerprint_command(
            command_id,
            estado="completado",
            mensaje="Huella registrada correctamente.",
            huellaId=int(position),
        )
        print(
            "HUELLA REGISTRADA "
            f"persona={person_id} lector={direction} id={position}",
            flush=True,
        )
        enqueue_voice("Huella registrada correctamente")
        wait_for_finger(sensor, False, 10)
    except Exception as error:
        if created_position is not None:
            try:
                sensor.deleteTemplate(created_position)
                print(
                    "HUELLA REVERTIDA "
                    f"persona={person_id} id={created_position}",
                    flush=True,
                )
            except Exception as rollback_error:
                print(
                    "ERROR REVERTIR HUELLA "
                    f"persona={person_id} id={created_position}: "
                    f"{rollback_error}",
                    flush=True,
                )
        message = str(error) or "No fue posible registrar la huella"
        print(
            f"ERROR REGISTRO HUELLA persona={person_id}: {message}",
            flush=True,
        )
        update_fingerprint_command(
            command_id,
            estado="error",
            mensaje=message,
        )
        enqueue_voice("No fue posible registrar la huella")


def fingerprint_worker(path, name, direction):
    enrollment_queue = fingerprint_enrollment_queues[direction]
    while not stop_event.is_set():
        sensor = initialize_fingerprint_sensor(path, name)
        if sensor is None:
            return
        try:
            while not stop_event.is_set():
                try:
                    command = enrollment_queue.get_nowait()
                except queue.Empty:
                    command = None
                if command is not None:
                    try:
                        if command.get("_syncCharacteristics") is not None:
                            try:
                                sync_fingerprint_in_open_exit_sensor(
                                    sensor,
                                    command,
                                )
                            except Exception as sync_error:
                                print(
                                    "ERROR SINCRONIZACION DIRECTA; "
                                    "reiniciando como respaldo: "
                                    f"{sync_error}",
                                    flush=True,
                                )
                                stop_event.set()
                                return
                        else:
                            enroll_fingerprint(sensor, command, direction)
                    finally:
                        enrollment_queue.task_done()
                    continue

                if not sensor.readImage():
                    stop_event.wait(0.08)
                    continue

                sensor.convertImage(0x01)
                position, accuracy = sensor.searchTemplate()
                accepted = (
                    position >= 0
                    and accuracy >= FINGERPRINT_MATCH_MIN_SCORE
                )

                if accepted:
                    print(
                        f"HUELLA {direction.upper()} "
                        f"id={position} precision={accuracy}",
                        flush=True,
                    )
                    person_id, user = find_person_by_fingerprint(
                        position,
                        direction,
                    )
                    if person_id:
                        process_fingerprint(person_id, user, direction)
                    else:
                        enqueue_voice("Huella no registrada")
                    stop_event.wait(2)
                else:
                    print(
                        f"HUELLA SIN COINCIDENCIA {direction.upper()} "
                        f"id={position} precision={accuracy}",
                        flush=True,
                    )
                    now = time.monotonic()
                    if now - fingerprint_no_match_last[direction] >= 5:
                        fingerprint_no_match_last[direction] = now
                        enqueue_voice("Huella no reconocida")
                    stop_event.wait(0.4)
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
    complete_pending_fingerprint_reset()
    complete_pending_fingerprint_sync()

    workers = [
        threading.Thread(target=voice_worker, name="voice", daemon=True),
        threading.Thread(
            target=qr_validation_worker,
            name="qr-validation",
            daemon=True,
        ),
        threading.Thread(
            target=backend_keepalive_worker,
            name="backend-keepalive",
            daemon=True,
        ),
        threading.Thread(
            target=fingerprint_command_worker,
            name="fingerprint-command",
            daemon=True,
        ),
        threading.Thread(target=qr_reader_worker, name="qr-reader", daemon=True),
        threading.Thread(
            target=fingerprint_worker,
            args=(
                FINGERPRINT_ENTRY_DEVICE,
                "Sensor Entrada",
                "entrada",
            ),
            name="fingerprint-entry",
            daemon=True,
        ),
        threading.Thread(
            target=fingerprint_worker,
            args=(
                FINGERPRINT_EXIT_DEVICE,
                "Sensor Salida",
                "salida",
            ),
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
