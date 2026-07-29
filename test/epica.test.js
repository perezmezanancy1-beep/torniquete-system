"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  clearPersonNameCache,
  fetchPersonName,
} = require("../epica");

test.beforeEach(() => clearPersonNameCache());

test("consulta NombrePersona con personaId y devuelve nombreCompleto", async () => {
  let request;
  const fetchImpl = async (url, options) => {
    request = { url, options };
    return {
      ok: true,
      json: async () => ({
        nombreCompleto: "KEILY JOHANA DE LA CRUZ DE LA HOZ",
      }),
    };
  };

  const name = await fetchPersonName(139573, { fetchImpl, now: 1000 });

  assert.equal(name, "KEILY JOHANA DE LA CRUZ DE LA HOZ");
  assert.match(request.url, /ServiciosEpica\.asmx\/NombrePersona$/u);
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.body, "id=139573");
  assert.match(
    request.options.headers["Content-Type"],
    /^application\/x-www-form-urlencoded/u
  );
});

test("usa caché para no consultar Épica en cada lectura", async () => {
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    return {
      ok: true,
      json: async () => ({ nombreCompleto: "PERSONA DE PRUEBA" }),
    };
  };

  assert.equal(
    await fetchPersonName(139573, { fetchImpl, now: 1000 }),
    "PERSONA DE PRUEBA"
  );
  assert.equal(
    await fetchPersonName(139573, { fetchImpl, now: 2000 }),
    "PERSONA DE PRUEBA"
  );
  assert.equal(calls, 1);
});

test("falla de forma segura si el id, HTTP o respuesta no son válidos", async () => {
  const failingFetch = async () => ({ ok: false });
  const emptyFetch = async () => ({
    ok: true,
    json: async () => ({ nombreCompleto: "   " }),
  });

  assert.equal(await fetchPersonName("abc", { fetchImpl: failingFetch }), null);
  assert.equal(await fetchPersonName(139573, { fetchImpl: failingFetch }), null);
  assert.equal(await fetchPersonName(139573, { fetchImpl: emptyFetch }), null);
});

test("una excepción de red no bloquea la lectura", async () => {
  const fetchImpl = async () => {
    throw new Error("red no disponible");
  };

  assert.equal(await fetchPersonName(139573, { fetchImpl }), null);
});
