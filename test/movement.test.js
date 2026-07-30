"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { recentMovementBlock } = require("../movement");

test("indica cuánto falta y permite invertir de salida a entrada", () => {
  const now = Date.UTC(2026, 6, 30, 14, 30, 0);
  const result = recentMovementBlock(
    {
      ultimoMovimiento: new Date(now - 4_100).toISOString(),
      ultimoMovimientoTipo: "salida",
    },
    now,
    10_000
  );

  assert.deepEqual(result, {
    lastType: "salida",
    nextType: "entrada",
    retryAfterSeconds: 6,
  });
});

test("después de diez segundos permite el siguiente movimiento", () => {
  const now = Date.UTC(2026, 6, 30, 14, 30, 0);
  assert.equal(
    recentMovementBlock(
      {
        ultimoMovimiento: new Date(now - 10_000).toISOString(),
        ultimoMovimientoTipo: "salida",
      },
      now,
      10_000
    ),
    null
  );
});

test("ignora fechas o tipos de movimiento inválidos", () => {
  assert.equal(
    recentMovementBlock(
      { ultimoMovimiento: "fecha-invalida", ultimoMovimientoTipo: "salida" }
    ),
    null
  );
  assert.equal(
    recentMovementBlock(
      { ultimoMovimiento: new Date().toISOString(), ultimoMovimientoTipo: "x" }
    ),
    null
  );
});
