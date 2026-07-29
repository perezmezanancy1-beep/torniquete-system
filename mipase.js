"use strict";

const PERIOD_MS = 60_000;
const MAX_TOKEN_AGE_MS = 2 * 60_000;
const CLOCK_SKEW_MS = 15_000;

const ROLES = Object.freeze({
  1: "ESTUDIANTE",
  2: "DOCENTE",
  3: "ADMINISTRATIVO",
  4: "EGRESADO",
  5: "VISITANTE",
});

function requireSecret() {
  const secret = process.env.MIPASE_SECRET;
  if (!secret || secret.length < 16) {
    throw new Error(
      "MIPASE_SECRET no está configurada o tiene menos de 16 caracteres"
    );
  }
  return Buffer.from(secret, "utf8");
}

function windowFor(milliseconds) {
  return Math.floor(milliseconds / PERIOD_MS);
}

function checksum(data, secret) {
  let accumulator = 0x9e;
  for (const byte of data) {
    accumulator = (Math.imul(accumulator, 31) + byte) & 0xff;
  }
  for (const byte of secret) {
    accumulator = (Math.imul(accumulator, 31) + byte) & 0xff;
  }
  return accumulator;
}

function keyStream(windowNumber, length, secret) {
  const output = Buffer.allocUnsafe(length);
  let state = ((windowNumber & 0x7fffffff) ^ 0x5bd1e995) | 0;

  for (let index = 0; index < length; index += 1) {
    state = (Math.imul(state, 1103515245) + 12345) & 0x7fffffff;
    const keyByte = secret[index % secret.length];
    const windowByte = (windowNumber >> (8 * (index & 3))) & 0xff;
    output[index] = ((state >>> 16) ^ keyByte ^ windowByte) & 0xff;
  }
  return output;
}

function toBase64Url(buffer) {
  return buffer
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/u, "");
}

function fromBase64Url(value) {
  if (
    typeof value !== "string" ||
    !/^[A-Za-z0-9_-]+={0,2}$/u.test(value)
  ) {
    return null;
  }

  try {
    const unpadded = value.replace(/=+$/u, "");
    const padding = "=".repeat((4 - (unpadded.length % 4)) % 4);
    const decoded = Buffer.from(
      unpadded.replace(/-/g, "+").replace(/_/g, "/") + padding,
      "base64"
    );
    return decoded.length >= 2 ? decoded : null;
  } catch {
    return null;
  }
}

function normalizeRole(role) {
  const numericRole = Number(role);
  return Number.isInteger(numericRole) && ROLES[numericRole]
    ? numericRole
    : null;
}

function issueToken({ personaId, codigoRol, now = Date.now() }) {
  const numericId = Number(personaId);
  const role = normalizeRole(codigoRol);

  if (!Number.isSafeInteger(numericId) || numericId <= 0 || role === null) {
    throw new TypeError("personaId o codigoRol no válido");
  }

  const timestamp = Number(now);
  if (!Number.isSafeInteger(timestamp) || timestamp <= 0) {
    throw new TypeError("Fecha de emisión no válida");
  }

  const secret = requireSecret();
  const data = Buffer.from(`${numericId}|${role}|${timestamp}`, "utf8");
  const plain = Buffer.concat([data, Buffer.from([checksum(data, secret)])]);
  const stream = keyStream(windowFor(timestamp), plain.length, secret);
  const encrypted = Buffer.allocUnsafe(plain.length);

  for (let index = 0; index < plain.length; index += 1) {
    encrypted[index] = plain[index] ^ stream[index];
  }

  return toBase64Url(encrypted);
}

function inspectionDeltas(tolerance) {
  const deltas = [0];
  for (let offset = 1; offset <= tolerance; offset += 1) {
    deltas.push(-offset, offset);
  }
  return deltas;
}

function inspectToken(token, options = {}) {
  const encrypted = fromBase64Url(typeof token === "string" ? token.trim() : "");
  if (!encrypted) {
    return Object.freeze({ ok: false, reason: "MALFORMED" });
  }

  const now = Number(options.now ?? Date.now());
  const tolerance = Number.isInteger(options.tolerance)
    ? Math.max(0, Math.min(options.tolerance, 1440))
    : 2;
  const maxAgeMs = Number(options.maxAgeMs ?? MAX_TOKEN_AGE_MS);
  const futureToleranceMs = Number(
    options.futureToleranceMs ?? CLOCK_SKEW_MS
  );

  if (
    !Number.isFinite(now) ||
    !Number.isFinite(maxAgeMs) ||
    !Number.isFinite(futureToleranceMs)
  ) {
    return Object.freeze({ ok: false, reason: "INVALID_OPTIONS" });
  }

  const secret = requireSecret();
  const currentWindow = windowFor(now);

  for (const delta of inspectionDeltas(tolerance)) {
    const candidateWindow = currentWindow + delta;
    const stream = keyStream(candidateWindow, encrypted.length, secret);
    const plain = Buffer.allocUnsafe(encrypted.length);

    for (let index = 0; index < encrypted.length; index += 1) {
      plain[index] = encrypted[index] ^ stream[index];
    }

    const data = plain.subarray(0, -1);
    if (checksum(data, secret) !== plain[plain.length - 1]) {
      continue;
    }

    const parts = data.toString("utf8").split("|");
    if (parts.length !== 3 || parts.some((part) => !/^\d+$/u.test(part))) {
      continue;
    }

    const personaId = Number(parts[0]);
    const codigoRol = normalizeRole(parts[1]);
    const emittedAtMs = Number(parts[2]);
    const ageMs = now - emittedAtMs;

    if (
      !Number.isSafeInteger(personaId) ||
      personaId <= 0 ||
      codigoRol === null ||
      !Number.isSafeInteger(emittedAtMs)
    ) {
      continue;
    }

    if (ageMs > maxAgeMs) {
      return Object.freeze({
        ok: false,
        reason: "EXPIRED",
        emittedAtMs,
        ageMs,
      });
    }

    if (ageMs < -futureToleranceMs) {
      return Object.freeze({
        ok: false,
        reason: "FUTURE",
        emittedAtMs,
        ageMs,
      });
    }

    const info = Object.freeze({
      personaId,
      codigoRol,
      rol: ROLES[codigoRol],
      emitidoMs: emittedAtMs,
      emitido: new Date(emittedAtMs),
      edadMs: ageMs,
    });
    return Object.freeze({ ok: true, info });
  }

  return Object.freeze({ ok: false, reason: "INVALID" });
}

function validateToken(token, options = {}) {
  const result = inspectToken(token, options);
  return result.ok ? result.info : null;
}

module.exports = {
  CLOCK_SKEW_MS,
  MAX_TOKEN_AGE_MS,
  PERIOD_MS,
  ROLES,
  inspectToken,
  issueToken,
  validateToken,
};
