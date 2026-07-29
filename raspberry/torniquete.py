#!/usr/bin/env python3
"""Control físico del torniquete UAC para Raspberry Pi."""

import os
import fcntl
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

try:
    from piper import PiperVoice, SynthesisConfig
except ImportError:
    PiperVoice = None
    SynthesisConfig = None


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

RELE_ENTRADA = 17
RELE_SALIDA = 27
AUDIO_DEVICE = "hw:2,0"
AUDIO_CARD = "2"
AUDIO_MIXER_LEVEL = "95%"
PIPER_MODEL = "/home/pirb/voices/es_MX-ald-medium.onnx"
VOICE_CACHE_DIR = "/home/pirb/.torniquete_voice_cache"
REQUEST_TIMEOUT = (4, 35)
QR_DEDUP_SECONDS = 3
FINGERPRINT_ENROLL_TIMEOUT_SECONDS = 35
FINGERPRINT_SECURITY_LEVEL = 1
FINGERPRINT_MATCH_MIN_SCORE = 30
FINGERPRINT_STABLE_PRESENT_READS = 2
FINGERPRINT_STABLE_ABSENT_READS = 2
FINGERPRINT_CAPTURE_ROUNDS = 1
FINGERPRINT_SECOND_SAMPLE_ATTEMPTS = 1
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


def load_neural_voice():
    if PiperVoice is None or not os.path.isfile(PIPER_MODEL):
        print("Voz neuronal no disponible; usando voz de respaldo", flush=True)
        return None
    try:
        voice = PiperVoice.load(PIPER_MODEL, use_cuda=False)
        print("Voz neuronal en español lista", flush=True)
        return voice
    except Exception as error:
        print(f"No fue posible cargar la voz neuronal: {error}", flush=True)
        return None


def voice_cache_path(text):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return os.path.join(VOICE_CACHE_DIR, f"{digest}.wav")


def synthesize_voice(text, audio_path, neural_voice):
    if neural_voice is not None:
        synthesis_config = SynthesisConfig(
            volume=1.15,
            length_scale=0.9,
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


def fingerprint_field(direction):
    return (
        "huella_entrada_id"
        if direction == "entrada"
        else "huella_salida_id"
    )


def find_person_by_fingerprint(fingerprint_id, direction):
    field = fingerprint_field(direction)
    for person_id, user in fetch_users().items():
        assigned_id = user.get(field)
        if assigned_id is None:
            assigned_id = user.get("huella_id")
        if str(assigned_id) == str(fingerprint_id):
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
            sensor = PyFingerprint(
                path,
                57600,
                0xFFFFFFFF,
                0x00000000,
            )
            if sensor.verifyPassword():
                if sensor.getSecurityLevel() != FINGERPRINT_SECURITY_LEVEL:
                    sensor.setSecurityLevel(FINGERPRINT_SECURITY_LEVEL)
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
    stop_event.wait(0.1)


def assign_fingerprint_to_person(person_id, direction, position):
    field = fingerprint_field(direction)
    users = fetch_users(force=True)
    for existing_person_id, user in users.items():
        assigned_id = user.get(field)
        if assigned_id is None:
            assigned_id = user.get("huella_id")
        if (
            str(assigned_id) == str(position)
            and str(existing_person_id) != str(person_id)
        ):
            raise RuntimeError(
                "La huella ya pertenece a otra persona en este lector"
            )

    response = http.patch(
        f"{URL_FIREBASE}/{person_id}.json",
        json={field: int(position)},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    with users_cache_lock:
        users_cache["expires_at"] = 0


def enroll_exit_fingerprint_by_search(
    sensor,
    person_id,
    direction,
    command_id,
):
    """Valida dos muestras usando el buscador del firmware USB de salida."""
    temporary_position = sensor.storeTemplate(
        positionNumber=-1,
        charBufferNumber=0x01,
    )
    keep_template = False
    try:
        request_finger_removal(sensor, command_id)
        second_prompt = "Coloque de nuevo el mismo dedo."
        enqueue_voice(second_prompt)
        capture_fingerprint_sample(
            sensor,
            0x01,
            command_id,
            second_prompt,
        )
        found_position, accuracy = sensor.searchTemplate()
        print(
            "HUELLA VALIDACION SALIDA "
            f"persona={person_id} temporal={temporary_position} "
            f"encontrada={found_position} puntuacion={accuracy}",
            flush=True,
        )
        if found_position != temporary_position:
            if found_position >= 0:
                owner_id, _owner = find_person_by_fingerprint(
                    found_position,
                    direction,
                )
                if owner_id and owner_id != str(person_id):
                    raise RuntimeError(
                        "La huella ya pertenece a otra persona"
                    )
            raise RuntimeError(
                "Las dos muestras no coincidieron. "
                "Coloque el mismo dedo cubriendo el sensor."
            )

        assign_fingerprint_to_person(
            person_id,
            direction,
            temporary_position,
        )
        keep_template = True
        return temporary_position
    finally:
        if not keep_template:
            try:
                sensor.deleteTemplate(temporary_position)
            except Exception as cleanup_error:
                print(
                    "No se pudo eliminar la huella temporal "
                    f"id={temporary_position}: {cleanup_error}",
                    flush=True,
                )


def enroll_fingerprint(sensor, command, direction):
    command_id = str(command.get("id") or "")
    person_id = str(command.get("personaId") or "")
    reader_label = "entrada" if direction == "entrada" else "salida"
    if not command_id or not person_id.isdigit():
        return

    enqueue_voice("Coloque el dedo")

    try:
        position = None
        for capture_round in range(1, FINGERPRINT_CAPTURE_ROUNDS + 1):
            first_prompt = (
                f"Coloque el dedo en el lector de {reader_label}."
                if capture_round == 1
                else "Coloque nuevamente el dedo, centrado y sin moverlo."
            )
            if capture_round > 1:
                enqueue_voice(first_prompt)
            capture_fingerprint_sample(
                sensor,
                0x01,
                command_id,
                first_prompt,
            )
            existing_position, _accuracy = sensor.searchTemplate()

            if existing_position >= 0:
                assign_fingerprint_to_person(
                    person_id,
                    direction,
                    existing_position,
                )
                position = existing_position
                break

            if direction == "salida":
                position = enroll_exit_fingerprint_by_search(
                    sensor,
                    person_id,
                    direction,
                    command_id,
                )
                break

            for second_attempt in range(
                1,
                FINGERPRINT_SECOND_SAMPLE_ATTEMPTS + 1,
            ):
                request_finger_removal(sensor, command_id)
                second_prompt = (
                    "Coloque de nuevo el mismo dedo."
                    if second_attempt == 1
                    else "Ajuste ligeramente el dedo y colóquelo otra vez."
                )
                enqueue_voice(second_prompt)
                capture_fingerprint_sample(
                    sensor,
                    0x02,
                    command_id,
                    second_prompt,
                )
                comparison_score = sensor.compareCharacteristics()
                print(
                    "HUELLA COMPARACION "
                    f"persona={person_id} ronda={capture_round} "
                    f"intento={second_attempt} puntuacion={comparison_score}",
                    flush=True,
                )
                if comparison_score > 0 and sensor.createTemplate():
                    position = sensor.storeTemplate()
                    assign_fingerprint_to_person(
                        person_id,
                        direction,
                        position,
                    )
                    break

                if (
                    second_attempt <
                    FINGERPRINT_SECOND_SAMPLE_ATTEMPTS
                ):
                    update_fingerprint_command(
                        command_id,
                        estado="procesando",
                        mensaje=(
                            "La posición cambió. Retire el dedo y vuelva "
                            "a colocarlo centrado."
                        ),
                    )
                    enqueue_voice(
                        "La posición cambió. Vamos a intentarlo otra vez"
                    )

            if position is not None:
                break

            if capture_round < FINGERPRINT_CAPTURE_ROUNDS:
                update_fingerprint_command(
                    command_id,
                    estado="procesando",
                    mensaje=(
                        "Tomaremos una muestra nueva. Retire el dedo."
                    ),
                )
                enqueue_voice(
                    "Tomaremos una muestra nueva. Retire el dedo"
                )
                if not wait_for_finger(sensor, False, 15):
                    raise TimeoutError("No se retiró completamente el dedo")

        if position is None:
            raise RuntimeError(
                "No se logró una lectura estable. Limpie el sensor "
                "y coloque el dedo completamente centrado."
            )

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
                        enroll_fingerprint(sensor, command, direction)
                    finally:
                        enrollment_queue.task_done()
                    continue

                if not sensor.readImage():
                    stop_event.wait(0.08)
                    continue

                sensor.convertImage(0x01)
                position, accuracy = sensor.searchTemplate()
                if (
                    position >= 0 and
                    accuracy >= FINGERPRINT_MATCH_MIN_SCORE
                ):
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
