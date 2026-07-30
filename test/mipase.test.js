"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

process.env.MIPASE_SECRET = "clave-exclusiva-para-pruebas-automatizadas";

const {
  MAX_TOKEN_AGE_MS,
  inspectToken,
  inspectTokenWithTrailingRecovery,
  issueToken,
  secondsUntilNextToken,
  validateToken,
} = require("../mipase");

const NOW = Date.UTC(2026, 6, 29, 15, 0, 0);
const DART_REFERENCE_TOKEN = "i0m2irprvaZcNEL5AOYP9vUqWfdfHEwwgRg";

test("es compatible byte a byte con Mi Pase en Dart", () => {
  const token = issueToken({
    personaId: 123456789,
    codigoRol: 2,
    now: 1785334800000,
  });

  assert.equal(token, DART_REFERENCE_TOKEN);
});

test("emite y valida un token reciente con los datos de la persona", () => {
  const token = issueToken({
    personaId: 123456789,
    codigoRol: 2,
    now: NOW - 30_000,
  });

  const result = validateToken(token, { now: NOW });

  assert.equal(result.personaId, 123456789);
  assert.equal(result.codigoRol, 2);
  assert.equal(result.rol, "DOCENTE");
  assert.equal(result.emitidoMs, NOW - 30_000);
});

test("acepta token Base64 URL con padding opcional del lector", () => {
  const token = issueToken({
    personaId: 139573,
    codigoRol: 1,
    now: NOW,
  });
  const padded = token + "=".repeat((4 - (token.length % 4)) % 4);

  assert.equal(validateToken(padded, { now: NOW }).personaId, 139573);
});

test("permite emitir pases temporales para visitantes registrados", () => {
  const token = issueToken({
    personaId: 139573,
    codigoRol: 5,
    now: NOW,
  });

  const result = validateToken(token, { now: NOW });
  assert.equal(result.rol, "VISITANTE");
});

test("acepta un token justo antes de cumplir treinta segundos", () => {
  const token = issueToken({
    personaId: 123456789,
    codigoRol: 1,
    now: NOW - MAX_TOKEN_AGE_MS + 1,
  });

  assert.ok(validateToken(token, { now: NOW }));
});

test("recupera exactamente el último carácter omitido por el lector HID", () => {
  const token = issueToken({
    personaId: 1047037821,
    codigoRol: 1,
    now: NOW - 10_000,
  });
  const truncated = token.slice(0, -1);

  const result = inspectTokenWithTrailingRecovery(truncated, { now: NOW });

  assert.equal(result.ok, true);
  assert.equal(result.info.personaId, 1047037821);
  assert.equal(result.token, token);
  assert.equal(result.recoveredTrailingCharacter, true);
});

test("recupera un carácter interno omitido en un token Dart con padding", () => {
  const token = issueToken({
    personaId: 176723,
    codigoRol: 3,
    now: NOW - 10_000,
  });
  const padded = token + "=".repeat((4 - (token.length % 4)) % 4);
  const missingIndex = 8;
  const truncated =
    padded.slice(0, missingIndex) + padded.slice(missingIndex + 1);

  const result = inspectTokenWithTrailingRecovery(truncated, { now: NOW });

  assert.equal(result.ok, true);
  assert.equal(result.info.personaId, 176723);
  assert.equal(result.token, token);
  assert.equal(result.recoveredMissingCharacter, true);
  assert.equal(result.recoveredCharacterIndex, missingIndex);
  assert.equal(result.recoveredTrailingCharacter, false);
});

test("mantiene la expiración aunque el lector omita el último carácter", () => {
  const token = issueToken({
    personaId: 1047037821,
    codigoRol: 1,
    now: NOW - MAX_TOKEN_AGE_MS - 1,
  });

  const result = inspectTokenWithTrailingRecovery(token.slice(0, -1), {
    now: NOW,
    tolerance: 5,
  });

  assert.equal(result.ok, false);
  assert.equal(result.reason, "EXPIRED");
  assert.equal(result.token, token);
  assert.equal(result.recoveredTrailingCharacter, true);
});

test("rechaza una fotografía del QR después de treinta segundos", () => {
  const token = issueToken({
    personaId: 123456789,
    codigoRol: 1,
    now: NOW - MAX_TOKEN_AGE_MS - 1,
  });

  const result = inspectToken(token, { now: NOW, tolerance: 5 });
  assert.equal(result.ok, false);
  assert.equal(result.reason, "EXPIRED");
  assert.equal(validateToken(token, { now: NOW, tolerance: 5 }), null);
});

test("calcula cuánto falta para que Mi Pase genere el siguiente QR", () => {
  assert.equal(secondsUntilNextToken(NOW, NOW), 22);
  assert.equal(secondsUntilNextToken(NOW, NOW + 19_001), 3);
  assert.equal(secondsUntilNextToken(NOW, NOW + 22_000), 0);
  assert.equal(secondsUntilNextToken(NOW, NOW + 19_001, 30_000), 11);
});

test("rechaza tokens futuros más allá del margen de reloj", () => {
  const token = issueToken({
    personaId: 123456789,
    codigoRol: 1,
    now: NOW + 16_000,
  });

  assert.equal(validateToken(token, { now: NOW }), null);
});

test("rechaza tokens alterados, texto libre y roles desconocidos", () => {
  const token = issueToken({
    personaId: 123456789,
    codigoRol: 4,
    now: NOW,
  });
  const replacement = token.endsWith("A") ? "B" : "A";

  assert.equal(validateToken(token.slice(0, -1) + replacement, { now: NOW }), null);
  assert.equal(validateToken("123456789", { now: NOW }), null);
  assert.throws(
    () => issueToken({ personaId: 123456789, codigoRol: 99, now: NOW }),
    /no válido/u
  );
});

test("no funciona si el servidor no tiene configurado MIPASE_SECRET", () => {
  const original = process.env.MIPASE_SECRET;
  delete process.env.MIPASE_SECRET;

  assert.throws(
    () => issueToken({ personaId: 1, codigoRol: 1, now: NOW }),
    /MIPASE_SECRET/u
  );

  process.env.MIPASE_SECRET = original;
});
