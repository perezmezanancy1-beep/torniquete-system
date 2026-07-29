"use strict";

const EPICA_NAME_URL =
  "https://epica.uac.edu.co/ServiciosWeb/ServiciosEpica.asmx/NombrePersona";
const DEFAULT_TIMEOUT_MS = 30_000;
const CACHE_TTL_MS = 15 * 60_000;

const nameCache = new Map();

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

function normalizeName(value) {
  if (typeof value !== "string") {
    return null;
  }
  const name = value.replace(/\s+/gu, " ").trim();
  return name || null;
}

async function fetchPersonName(
  personaId,
  {
    fetchImpl = globalThis.fetch,
    now = Date.now(),
    timeoutMs = Number(process.env.EPICA_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS,
  } = {}
) {
  const id = normalizePersonaId(personaId);
  if (!id || typeof fetchImpl !== "function") {
    return null;
  }

  const cached = nameCache.get(id);
  if (cached && cached.expiresAt > now) {
    return cached.name;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchImpl(EPICA_NAME_URL, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: new URLSearchParams({ id }).toString(),
      signal: controller.signal,
    });

    if (!response.ok) {
      return null;
    }

    const payload = await response.json();
    const name = normalizeName(payload?.nombreCompleto);
    if (!name) {
      return null;
    }

    nameCache.set(id, {
      name,
      expiresAt: now + CACHE_TTL_MS,
    });
    return name;
  } catch {
    // El servicio institucional complementa la lectura; una caída temporal no
    // debe bloquear el torniquete. El servidor usará el nombre de Firebase.
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

function clearPersonNameCache() {
  nameCache.clear();
}

module.exports = {
  CACHE_TTL_MS,
  EPICA_NAME_URL,
  clearPersonNameCache,
  fetchPersonName,
};
