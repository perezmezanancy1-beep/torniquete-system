# Torniquete UAC · QR MiPase

Sistema web para generar y leer códigos QR temporales de acceso. La generación,
el descifrado y la validación ocurren exclusivamente en el servidor.

## Seguridad de la clave

La clave real **no está incluida en este repositorio**, en los archivos HTML ni
en el JavaScript que recibe el navegador. El servidor la obtiene desde:

```text
MIPASE_SECRET
```

Configure esa variable en el administrador de secretos del entorno donde se
ejecuta Node (por ejemplo, Environment de Render). No cree un archivo con la
clave dentro de `public/`, no la agregue al código y no la confirme en Git.

También se requiere:

```text
FIREBASE_CONFIG
FIREBASE_DATABASE_URL   # opcional; ya existe un valor predeterminado
EPICA_TIMEOUT_MS        # opcional; 5000 ms por defecto
```

El servidor se niega a iniciar si falta `MIPASE_SECRET` o si tiene menos de 12
caracteres.

## Flujo

1. `qr.html` envía la identificación a `POST /api/qr`.
2. El servidor consulta la persona en Firebase y genera la imagen QR.
3. `index.html` lee el QR con la cámara y envía su contenido a
   `POST /validar`.
4. El servidor descifra el token y exige:
   - checksum correcto;
   - persona y rol válidos;
   - fecha no futura (salvo 15 segundos de tolerancia);
   - antigüedad máxima de 30 segundos;
   - que el token no se haya utilizado anteriormente.
5. Con `personaId`, el servidor consulta `NombrePersona` en Servicios Épica para
   resolver `nombreCompleto`. La consulta tiene timeout y caché de 15 minutos;
   si Épica no responde, se usa el nombre existente en Firebase.
6. La respuesta incluye nombre, ID, rol, carrera, movimiento y horas de emisión
   y registro.

Si el token superó los 30 segundos, el lector muestra **Código QR expirado** y
reproduce por voz: **«El código QR ya expiró»**.

## Endpoints

### Generar QR

```http
POST /api/qr
Content-Type: application/json

{"cedula":"123456789"}
```

### Validar lectura

El campo `token` debe contener exactamente el texto leído del QR:

```http
POST /validar
Content-Type: application/json

{"token":"..."}
```

Una lectura válida devuelve:

```json
{
  "ok": true,
  "tipo": "entrada",
  "mensaje": "Bienvenido a la UAC",
  "persona": {
    "personaId": 123456789,
    "nombre": "Nombre de la persona",
    "carrera": "Programa",
    "codigoRol": 1,
    "rol": "ESTUDIANTE"
  }
}
```

## Ejecución y pruebas

```powershell
npm test
npm start
```

La página principal (`/`) es el lector por cámara. La página `/qr.html` genera
el pase temporal.

## Raspberry Pi

Los archivos instalados en la Raspberry están versionados en `raspberry/`:

- `torniquete.py`: lector QR USB, dos sensores de huella, voz y relés.
- `qr_input.py`: reconstrucción del token enviado por el lector tipo teclado.
- `torniquete.service`: inicio automático y reinicio ante fallos.

El lector físico envía el token completo a `/validar`; no recorta una cédula y
no contiene `MIPASE_SECRET`. Reconoce mayúsculas, minúsculas, números, `-`, `_`
y padding `=`.

El servicio se administra con:

```bash
sudo systemctl status torniquete
sudo systemctl restart torniquete
journalctl -u torniquete -f
```

La instalación realizada conserva una copia recuperable del script anterior en
`/home/pirb/torniquete.py.bak-20260729-095429`.
