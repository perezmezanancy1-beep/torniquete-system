"use strict";

const DEFAULT_VISITOR_DURATION_MS = 7 * 60 * 60 * 1000;

function normalizeVisitorLabel(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .trim()
    .toUpperCase();
}

function isVisitor(user) {
  return normalizeVisitorLabel(user?.tipo) === "VISITANTE";
}

function isVisitorAccessActive(user, now = Date.now()) {
  if (!isVisitor(user)) {
    return true;
  }

  const explicitExpiration = Number(user?.expiracion);
  if (Number.isFinite(explicitExpiration) && explicitExpiration > 0) {
    return now <= explicitExpiration;
  }

  const startedAt = Number(user?.inicio);
  if (!Number.isFinite(startedAt) || startedAt <= 0) {
    return true;
  }
  return now - startedAt <= DEFAULT_VISITOR_DURATION_MS;
}

module.exports = {
  DEFAULT_VISITOR_DURATION_MS,
  isVisitor,
  isVisitorAccessActive,
};
