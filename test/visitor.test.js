"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  DEFAULT_VISITOR_DURATION_MS,
  isVisitor,
  isVisitorAccessActive,
} = require("../visitor");

test("identifica visitantes sin depender de mayúsculas o tildes", () => {
  assert.equal(isVisitor({ tipo: " visitante " }), true);
  assert.equal(isVisitor({ tipo: "ESTUDIANTE" }), false);
});

test("respeta la expiración elegida aunque supere las siete horas", () => {
  const now = 1_000_000;
  const visitor = {
    tipo: "VISITANTE",
    inicio: now - DEFAULT_VISITOR_DURATION_MS - 1,
    expiracion: now + 60_000,
  };

  assert.equal(isVisitorAccessActive(visitor, now), true);
  assert.equal(isVisitorAccessActive(visitor, now + 60_001), false);
});

test("mantiene siete horas solo como compatibilidad para registros antiguos", () => {
  const now = 1_000_000_000;
  assert.equal(
    isVisitorAccessActive(
      { tipo: "VISITANTE", inicio: now - DEFAULT_VISITOR_DURATION_MS },
      now
    ),
    true
  );
  assert.equal(
    isVisitorAccessActive(
      { tipo: "VISITANTE", inicio: now - DEFAULT_VISITOR_DURATION_MS - 1 },
      now
    ),
    false
  );
});

test("los usuarios institucionales no quedan sujetos a vigencia de visitante", () => {
  assert.equal(
    isVisitorAccessActive(
      { tipo: "DOCENTE", expiracion: 1 },
      Date.now()
    ),
    true
  );
});
