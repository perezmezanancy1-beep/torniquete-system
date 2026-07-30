"use strict";

function recentMovementBlock(user, now = Date.now(), cooldownMs = 10_000) {
  const lastMovementAt = new Date(user?.ultimoMovimiento);
  const lastMovementMs = lastMovementAt.getTime();
  const movementAgeMs = now - lastMovementMs;
  const lastType = user?.ultimoMovimientoTipo;

  if (
    !Number.isFinite(lastMovementMs) ||
    movementAgeMs < 0 ||
    movementAgeMs >= cooldownMs ||
    (lastType !== "entrada" && lastType !== "salida")
  ) {
    return null;
  }

  const nextType = lastType === "entrada" ? "salida" : "entrada";
  return Object.freeze({
    lastType,
    nextType,
    retryAfterSeconds: Math.max(
      1,
      Math.ceil((cooldownMs - movementAgeMs) / 1000)
    ),
  });
}

module.exports = { recentMovementBlock };
