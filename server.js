"use strict";

const crypto = require("crypto");
const express = require("express");
const admin = require("firebase-admin");
const path = require("path");
const QRCode = require("qrcode");
const twilio = require("twilio");

const { fetchPersonName } = require("./epica");
const {
  PERIOD_MS,
  ROLES,
  inspectTokenWithTrailingRecovery,
  issueToken,
} = require("./mipase");
const {
  isVisitor,
  isVisitorAccessActive,
} = require("./visitor");

const MOVEMENT_COOLDOWN_MS = 10_000;
const FINGERPRINT_COMMAND_TTL_MS = 2 * 60_000;

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "16kb" }));
app.use(express.static(path.join(__dirname, "public")));

function requireServerConfiguration() {
  if (!process.env.FIREBASE_CONFIG) {
    throw new Error("FIREBASE_CONFIG no está configurada");
  }
  if (!process.env.MIPASE_SECRET || process.env.MIPASE_SECRET.length < 12) {
    throw new Error(
      "MIPASE_SECRET no está configurada o tiene menos de 12 caracteres"
    );
  }
}

requireServerConfiguration();

const serviceAccount = JSON.parse(process.env.FIREBASE_CONFIG);
admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL:
    process.env.FIREBASE_DATABASE_URL ||
    "https://torniquete-universidad-default-rtdb.firebaseio.com",
});

const db = admin.database();
console.log("Firebase conectado");

function normalizePersonaId(value) {
  const text = String(value ?? "").trim();
  if (!/^\d+$/u.test(text)) {
    return null;
  }
  const numericId = Number(text);
  return Number.isSafeInteger(numericId) && numericId > 0
    ? String(numericId)
    : null;
}

function normalizeLabel(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .trim()
    .toUpperCase();
}

function roleCodeFor(user) {
  const directCode = Number(user?.codigoRol);
  if (Number.isInteger(directCode) && ROLES[directCode]) {
    return directCode;
  }

  const label = normalizeLabel(user?.rol || user?.tipo);
  const mappings = {
    ESTUDIANTE: 1,
    DOCENTE: 2,
    TRABAJADOR: 3,
    ADMINISTRATIVO: 3,
    EGRESADO: 4,
    VISITANTE: 5,
  };
  return mappings[label] || 1;
}

function publicPerson(personaId, user, codigoRol, institutionalName = null) {
  return {
    personaId: Number(personaId),
    nombre: institutionalName || String(user?.nombre ?? "").trim(),
    carrera: String(user?.carrera ?? "").trim(),
    tipo: String(user?.tipo ?? "").trim(),
    codigoRol,
    rol: ROLES[codigoRol],
    expiracion: isVisitor(user) ? Number(user?.expiracion) || null : null,
  };
}

function normalizeFingerprintReader(value) {
  const reader = String(value ?? "").trim().toLowerCase();
  return reader === "entrada" || reader === "salida" ? reader : null;
}

function publicFingerprintCommand(command) {
  if (!command || typeof command !== "object") {
    return null;
  }
  return {
    id: String(command.id ?? ""),
    accion: String(command.accion ?? ""),
    personaId: Number(command.personaId),
    nombre: String(command.nombre ?? ""),
    lector: String(command.lector ?? ""),
    estado: String(command.estado ?? ""),
    mensaje: String(command.mensaje ?? ""),
    huellaId:
      command.huellaId !== null &&
      command.huellaId !== undefined &&
      Number.isInteger(Number(command.huellaId))
      ? Number(command.huellaId)
      : null,
    creado: String(command.creado ?? ""),
    actualizado: String(command.actualizado ?? ""),
  };
}

async function refreshStoredPersonName(personaId, ref, currentName) {
  const freshName = await fetchPersonName(personaId);
  if (!freshName) {
    return null;
  }
  if (freshName !== currentName) {
    await ref.update({
      nombre: freshName,
      nombreEpicaActualizado: new Date().toISOString(),
    });
    console.log(`Nombre Epica actualizado: ${personaId} -> ${freshName}`);
  }
  return freshName;
}

async function currentPersonName(personaId, ref, storedName) {
  const lookup = refreshStoredPersonName(personaId, ref, storedName).catch(
    (error) => {
      console.warn(
        `No fue posible refrescar nombre Epica ${personaId}:`,
        error.message
      );
      return null;
    }
  );
  const timeout = new Promise((resolve) => {
    setTimeout(() => resolve(null), 800);
  });
  return (await Promise.race([lookup, timeout])) || storedName || null;
}

function colombiaDateTime(date) {
  return new Intl.DateTimeFormat("es-CO", {
    timeZone: "America/Bogota",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function colombiaDateKey(date) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Bogota",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function tokenFingerprint(token) {
  const canonicalToken = String(token).replace(/=+$/u, "");
  return crypto
    .createHash("sha256")
    .update(canonicalToken, "utf8")
    .digest("hex");
}

function qrIdentityFingerprint(info) {
  const windowNumber = Math.floor(info.emitidoMs / PERIOD_MS);
  return tokenFingerprint(
    `MIPASE:${info.personaId}:${info.codigoRol}:${windowNumber}`
  );
}

async function claimTokenOnce(info) {
  const fingerprint = qrIdentityFingerprint(info);
  const usedTokenRef = db.ref(`qrUsados/${fingerprint}`);
  const result = await usedTokenRef.transaction((currentValue) => {
    if (currentValue !== null) {
      return;
    }
    return Date.now();
  });
  return result.committed;
}

async function ensureVisitorIsActive(ref, user) {
  if (!isVisitor(user)) {
    return true;
  }

  const now = Date.now();
  const explicitExpiration = Number(user.expiracion);
  if (Number.isFinite(explicitExpiration) && explicitExpiration > 0) {
    return isVisitorAccessActive(user, now);
  }

  let startedAt = Number(user.inicio);
  if (!Number.isFinite(startedAt) || startedAt <= 0) {
    startedAt = now;
    await ref.update({ inicio: startedAt });
    user.inicio = startedAt;
  }

  return isVisitorAccessActive(
    {
      ...user,
      inicio: startedAt,
      expiracion: null,
    },
    now
  );
}

function getTwilioClient() {
  if (
    !process.env.TWILIO_SID ||
    !process.env.TWILIO_TOKEN ||
    !process.env.TWILIO_NUMERO
  ) {
    return null;
  }
  return twilio(process.env.TWILIO_SID, process.env.TWILIO_TOKEN);
}

async function sendVisitorSms(personaId, phone, user) {
  const client = getTwilioClient();
  if (!client) {
    console.warn("Twilio no configurado; SMS omitido");
    return false;
  }

  const link =
    `https://torniquete-system.onrender.com/qr.html?cedula=${personaId}`;
  const expiration = Number(user?.expiracion);
  const validityText =
    Number.isFinite(expiration) && expiration > 0
      ? `Su acceso es válido hasta ${colombiaDateTime(new Date(expiration))}`
      : "Su acceso tiene una vigencia temporal";
  try {
    await client.messages.create({
      body:
        "UAC ACCESO\n\n" +
        "Bienvenido a la Universidad Autónoma del Caribe\n\n" +
        `${validityText}\n\n` +
        `Ingrese aquí:\n${link}`,
      from: process.env.TWILIO_NUMERO,
      to: `+57${phone}`,
    });
    console.log(`SMS enviado a ${phone}`);
    return true;
  } catch (error) {
    console.error("Error enviando SMS:", error.message);
    return false;
  }
}

app.post("/registrar", async (req, res) => {
  try {
    const personaId = normalizePersonaId(req.body?.cedula);
    if (!personaId) {
      return res.status(400).json({ ok: false, error: "CEDULA_INVALIDA" });
    }

    const data = { ...req.body, cedula: personaId };
    if (isVisitor(data)) {
      const now = Date.now();
      const expiration = Number(data.expiracion);
      const maximumExpiration = now + 365 * 24 * 60 * 60 * 1000;
      if (
        !Number.isFinite(expiration) ||
        expiration <= now ||
        expiration > maximumExpiration
      ) {
        return res.status(400).json({
          ok: false,
          error: "VIGENCIA_VISITANTE_REQUERIDA",
          mensaje:
            "Indica una vigencia válida para el visitante, entre un minuto y 365 días.",
        });
      }
      const startedAt =
        Number.isFinite(Number(data.inicio)) && Number(data.inicio) > 0
          ? Number(data.inicio)
          : now;
      data.inicio = startedAt;
      data.expiracion = expiration;
      data.duracionMinutos = Math.max(
        1,
        Math.round((expiration - startedAt) / 60_000)
      );
      data.nombreManual = true;
    }
    await db.ref(`usuarios/${personaId}`).set(data);

    if (isVisitor(data) && data.celular) {
      await sendVisitorSms(personaId, data.celular, data);
    }

    return res.json({ ok: true });
  } catch (error) {
    console.error("Error en registro:", error.message);
    return res.status(500).json({ ok: false, error: "ERROR_REGISTRO" });
  }
});

// Genera el QR dentro del servidor. La clave y el algoritmo nunca se envían al
// navegador ni forman parte de los archivos estáticos.
app.post("/api/qr", async (req, res) => {
  try {
    const personaId = normalizePersonaId(
      req.body?.cedula ?? req.body?.personaId
    );
    if (!personaId) {
      return res.status(400).json({
        ok: false,
        error: "PERSONA_ID_INVALIDO",
        mensaje: "Ingresa una identificación numérica válida.",
      });
    }

    const ref = db.ref(`usuarios/${personaId}`);
    const snapshot = await ref.once("value");
    if (!snapshot.exists()) {
      return res.status(404).json({
        ok: false,
        error: "USUARIO_NO_ENCONTRADO",
        mensaje: "Usuario no encontrado.",
      });
    }

    const user = snapshot.val();
    if (!(await ensureVisitorIsActive(ref, user))) {
      return res.status(403).json({
        ok: false,
        error: "VISITANTE_EXPIRADO",
        mensaje: "El acceso del visitante ya expiró.",
      });
    }

    const codigoRol = roleCodeFor(user);
    const token = issueToken({ personaId, codigoRol });
    const [qrDataUrl, institutionalName] = await Promise.all([
      QRCode.toDataURL(token, {
        errorCorrectionLevel: "M",
        margin: 2,
        width: 320,
        color: {
          dark: "#111111",
          light: "#ffffff",
        },
      }),
      isVisitor(user) ? Promise.resolve(null) : fetchPersonName(personaId),
    ]);

    res.set("Cache-Control", "no-store");
    return res.json({
      ok: true,
      qrDataUrl,
      persona: publicPerson(
        personaId,
        user,
        codigoRol,
        institutionalName
      ),
      renuevaEnSegundos: 30,
      vigenciaMaximaSegundos: 120,
    });
  } catch (error) {
    console.error("Error al generar QR:", error.message);
    return res.status(500).json({
      ok: false,
      error: "ERROR_GENERANDO_QR",
      mensaje: "No fue posible generar el código QR.",
    });
  }
});

// Recibe exactamente el token leído por el escáner físico de la Raspberry.
app.post("/validar", async (req, res) => {
  try {
    const receivedToken = typeof req.body?.token === "string"
      ? req.body.token.trim()
      : "";

    // La búsqueda amplia permite identificar capturas vencidas y devolver el
    // código específico que la Raspberry anuncia por voz.
    const validation = inspectTokenWithTrailingRecovery(
      receivedToken,
      { tolerance: 1440 }
    );
    if (!validation.ok) {
      const expired = validation.reason === "EXPIRED";
      return res.status(401).json({
        ok: false,
        error: expired ? "TOKEN_EXPIRADO" : "TOKEN_INVALIDO",
        mensaje: expired
          ? "El código QR ya expiró."
          : "El código QR es inválido.",
      });
    }

    if (validation.recoveredMissingCharacter) {
      console.warn(
        "QR recuperado: el lector omitió un carácter " +
        `(posición=${validation.recoveredCharacterIndex}, ` +
        `longitud recibida=${receivedToken.length})`
      );
    }

    const info = validation.info;
    if (!(await claimTokenOnce(info))) {
      return res.status(409).json({
        ok: false,
        error: "TOKEN_YA_UTILIZADO",
        mensaje: "Este código QR ya fue utilizado.",
      });
    }

    const personaId = String(info.personaId);
    const ref = db.ref(`usuarios/${personaId}`);
    const snapshot = await ref.once("value");
    const storedUser = snapshot.exists() ? snapshot.val() : null;
    const storedName = storedUser
      ? String(storedUser.nombre ?? "").trim() || null
      : null;
    let institutionalName = storedUser
      ? (
          isVisitor(storedUser)
            ? storedName
            : await currentPersonName(personaId, ref, storedName)
        )
      : await fetchPersonName(personaId);

    let user;
    if (!snapshot.exists()) {
      if (!institutionalName) {
        return res.status(404).json({
          ok: false,
          error: "USUARIO_NO_ENCONTRADO",
          mensaje: "La persona del QR no está registrada en Épica.",
        });
      }

      user = {
        nombre: institutionalName,
        tipo: info.rol,
        rol: info.rol,
        codigoRol: info.codigoRol,
        estado: "fuera",
        origen: "MIPASE",
        creado: new Date().toISOString(),
      };
      await ref.set(user);
      console.log(`Usuario Mi Pase registrado automáticamente: ${personaId}`);
    } else {
      user = storedUser;
      if (institutionalName && institutionalName !== storedName) {
        user.nombre = institutionalName;
      }
    }

    const storedRole = roleCodeFor(user);
    if (storedRole !== info.codigoRol) {
      return res.status(403).json({
        ok: false,
        error: "ROL_NO_COINCIDE",
        mensaje: "El rol del QR no coincide con el registro institucional.",
      });
    }

    if (!(await ensureVisitorIsActive(ref, user))) {
      return res.status(403).json({
        ok: false,
        error: "VISITANTE_EXPIRADO",
        mensaje: "El acceso del visitante ya expiró.",
      });
    }

    const registeredAt = new Date();
    const lastMovementAt = new Date(user.ultimoMovimiento);
    const lastMovementMs = lastMovementAt.getTime();
    const movementAgeMs = registeredAt.getTime() - lastMovementMs;
    const lastMovementType = user.ultimoMovimientoTipo;

    if (
      Number.isFinite(lastMovementMs) &&
      movementAgeMs >= 0 &&
      movementAgeMs < MOVEMENT_COOLDOWN_MS &&
      (lastMovementType === "entrada" || lastMovementType === "salida")
    ) {
      const movementLabel =
        lastMovementType === "entrada" ? "entrada" : "salida";
      return res.status(409).json({
        ok: false,
        error: "MOVIMIENTO_RECIENTE",
        tipo: lastMovementType,
        mensaje: `La ${movementLabel} ya fue registrada.`,
      });
    }

    const stateIsFromToday =
      Number.isFinite(lastMovementMs) &&
      colombiaDateKey(lastMovementAt) === colombiaDateKey(registeredAt);
    const tipo =
      user.estado === "dentro" && stateIsFromToday ? "salida" : "entrada";
    const estado = tipo === "entrada" ? "dentro" : "fuera";

    await ref.update({
      estado,
      ultimoMovimiento: registeredAt.toISOString(),
      ultimoMovimientoTipo: tipo,
      ultimoQRHash: qrIdentityFingerprint(info),
    });

    await db.ref("historial").push({
      cedula: personaId,
      personaId: Number(personaId),
      nombre: institutionalName || user.nombre || "Usuario",
      tipo,
      fecha: registeredAt.toISOString(),
      origen: "QR_MIPASE",
    });

    return res.json({
      ok: true,
      tipo,
      mensaje: tipo === "entrada"
        ? "Bienvenido a la UAC"
        : "Salida registrada",
      persona: publicPerson(
        personaId,
        user,
        storedRole,
        institutionalName
      ),
      qr: {
        emitido: info.emitido.toISOString(),
        emitidoColombia: colombiaDateTime(info.emitido),
        edadSegundos: Math.max(0, Math.floor(info.edadMs / 1000)),
      },
      registrado: registeredAt.toISOString(),
      registradoColombia: colombiaDateTime(registeredAt),
    });
  } catch (error) {
    console.error("Error al validar QR:", error.message);
    return res.status(500).json({
      ok: false,
      error: "ERROR_VALIDANDO_QR",
      mensaje: "No fue posible validar el código QR.",
    });
  }
});

app.get("/health", (req, res) => {
  res.json({ ok: true });
});

app.post("/api/huellas/registrar", async (req, res) => {
  try {
    const personaId = normalizePersonaId(
      req.body?.personaId ?? req.body?.cedula
    );
    const lector = normalizeFingerprintReader(req.body?.lector);
    if (!personaId || !lector) {
      return res.status(400).json({
        ok: false,
        error: "DATOS_HUELLA_INVALIDOS",
        mensaje: "Selecciona una persona y un lector válidos.",
      });
    }

    const userSnapshot = await db.ref(`usuarios/${personaId}`).once("value");
    if (!userSnapshot.exists()) {
      return res.status(404).json({
        ok: false,
        error: "USUARIO_NO_ENCONTRADO",
        mensaje: "La persona no está registrada.",
      });
    }

    const user = userSnapshot.val();
    if (
      lector === "salida" &&
      user?.huella_entrada_id === undefined &&
      user?.huella_salida_id === undefined
    ) {
      return res.status(409).json({
        ok: false,
        error: "HUELLA_ENTRADA_REQUERIDA",
        mensaje:
          "Registre primero la huella en entrada; después se sincronizará con salida.",
      });
    }
    const now = Date.now();
    const command = {
      id: crypto.randomUUID(),
      personaId: Number(personaId),
      nombre: String(user?.nombre ?? "").trim() || `Usuario ${personaId}`,
      lector,
      estado: "pendiente",
      mensaje: `Esperando el lector de ${lector}.`,
      creado: new Date(now).toISOString(),
      actualizado: new Date(now).toISOString(),
      expira: new Date(now + FINGERPRINT_COMMAND_TTL_MS).toISOString(),
      expiraEpochMs: now + FINGERPRINT_COMMAND_TTL_MS,
    };

    const commandRef = db.ref("controlHuella/actual");
    const result = await commandRef.transaction((current) => {
      const currentState = String(current?.estado ?? "");
      const expiration = new Date(current?.expira ?? 0).getTime();
      const currentIsActive =
        (
          currentState === "pendiente" ||
          currentState === "procesando" ||
          currentState === "sincronizando" ||
          currentState === "borrando"
        ) &&
        Number.isFinite(expiration) &&
        expiration > now;
      return currentIsActive ? undefined : command;
    });

    if (!result.committed) {
      return res.status(409).json({
        ok: false,
        error: "LECTOR_OCUPADO",
        mensaje: "Ya hay un registro de huella en curso.",
        comando: publicFingerprintCommand(result.snapshot.val()),
      });
    }

    console.log(
      `Registro de huella solicitado: persona=${personaId} lector=${lector}`
    );
    return res.status(202).json({
      ok: true,
      mensaje: `Lector de ${lector} habilitado.`,
      comando: publicFingerprintCommand(command),
    });
  } catch (error) {
    console.error("Error solicitando registro de huella:", error.message);
    return res.status(500).json({
      ok: false,
      error: "ERROR_REGISTRO_HUELLA",
      mensaje: "No fue posible habilitar el lector de huella.",
    });
  }
});

app.post("/api/huellas/borrar-todas", async (req, res) => {
  try {
    if (String(req.body?.confirmacion ?? "") !== "BORRAR TODAS") {
      return res.status(400).json({
        ok: false,
        error: "CONFIRMACION_REQUERIDA",
        mensaje: "Escribe BORRAR para confirmar la eliminación.",
      });
    }

    const now = Date.now();
    const command = {
      id: crypto.randomUUID(),
      accion: "borrar_todas",
      lector: "ambos",
      estado: "pendiente",
      mensaje: "Esperando la Raspberry para borrar todas las huellas.",
      creado: new Date(now).toISOString(),
      actualizado: new Date(now).toISOString(),
      expira: new Date(now + FINGERPRINT_COMMAND_TTL_MS).toISOString(),
      expiraEpochMs: now + FINGERPRINT_COMMAND_TTL_MS,
    };

    const commandRef = db.ref("controlHuella/actual");
    const result = await commandRef.transaction((current) => {
      const currentState = String(current?.estado ?? "");
      const expiration = new Date(current?.expira ?? 0).getTime();
      const currentIsActive =
        [
          "pendiente",
          "procesando",
          "sincronizando",
          "borrando",
        ].includes(currentState) &&
        Number.isFinite(expiration) &&
        expiration > now;
      return currentIsActive ? undefined : command;
    });

    if (!result.committed) {
      return res.status(409).json({
        ok: false,
        error: "LECTOR_OCUPADO",
        mensaje: "Hay otra operación de huellas en curso.",
        comando: publicFingerprintCommand(result.snapshot.val()),
      });
    }

    console.warn("Eliminación total de huellas solicitada");
    return res.status(202).json({
      ok: true,
      mensaje: "La eliminación total fue enviada a la Raspberry.",
      comando: publicFingerprintCommand(command),
    });
  } catch (error) {
    console.error("Error solicitando borrado de huellas:", error.message);
    return res.status(500).json({
      ok: false,
      error: "ERROR_BORRANDO_HUELLAS",
      mensaje: "No fue posible solicitar la eliminación de huellas.",
    });
  }
});

app.get("/api/huellas/estado", async (req, res) => {
  try {
    const snapshot = await db.ref("controlHuella/actual").once("value");
    const command = publicFingerprintCommand(snapshot.val());
    if (
      req.query?.id &&
      command &&
      String(req.query.id) !== command.id
    ) {
      return res.status(404).json({
        ok: false,
        error: "COMANDO_NO_ENCONTRADO",
        mensaje: "El registro solicitado ya no está activo.",
      });
    }
    res.set("Cache-Control", "no-store");
    return res.json({ ok: true, comando: command });
  } catch (error) {
    console.error("Error consultando estado de huella:", error.message);
    return res.status(500).json({
      ok: false,
      error: "ERROR_ESTADO_HUELLA",
      mensaje: "No fue posible consultar el lector.",
    });
  }
});

app.get("/", (req, res) => {
  res.sendFile(path.join(__dirname, "public", "index.html"));
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Servidor web activo en puerto ${PORT}`);
});
