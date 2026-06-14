const express = require("express");
const admin = require("firebase-admin");
const path = require("path");

const app = express();
app.use(express.json());

// ✅ SERVIR WEB
app.use(express.static("public"));

// ✅ FIREBASE
const serviceAccount = JSON.parse(process.env.FIREBASE_CONFIG);

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: "https://torniquete-universidad-default-rtdb.firebaseio.com"
});

const db = admin.database();

console.log("✅ Firebase conectado");

// ✅ API VALIDAR (MEJORADA 🔥)
app.post("/validar", async (req, res) => {
  try {

    // ✅ AHORA RECIBE TAMBIÉN exp
    const { cedula, exp } = req.body;

    if (!cedula) {
      return res.json({ ok: false });
    }

    // 🔒 SEGURIDAD: VALIDAR EXPIRACIÓN DEL QR
    if (exp) {
      const ahora = Date.now();

      if (ahora - exp > 30000) {
        console.log("⛔ QR EXPIRADO:", cedula);
        return res.json({ ok: false, error: "QR EXPIRADO" });
      }
    }

    const ref = db.ref("usuarios/" + cedula);
    const snap = await ref.once("value");

    if (!snap.exists()) {
      console.log("❌ Usuario no existe:", cedula);
      return res.json({ ok: false });
    }

    let user = snap.val();
    let tipoAcceso = "";

    // ✅ ENTRADA
    if (!user.estado || user.estado === "fuera") {

      await ref.update({ estado: "dentro" });
      tipoAcceso = "entrada";

    } else {

      // ✅ SALIDA
      await ref.update({ estado: "fuera" });
      tipoAcceso = "salida";
    }

    // ✅ GUARDAR HISTORIAL AUTOMÁTICO 🔥
    await db.ref("historial").push({
      cedula: cedula,
      nombre: user.nombre || "Usuario",
      tipo: tipoAcceso,
      fecha: new Date().toISOString()
    });

    console.log(`✅ ${cedula} → ${tipoAcceso}`);

    return res.json({
      ok: true,
      tipo: tipoAcceso
    });

  } catch (error) {
    console.log("❌ Error:", error);
    res.json({ ok: false });
  }
});

// ✅ RUTA PRINCIPAL
app.get("/", (req, res) => {
  res.sendFile(path.join(__dirname, "public", "index.html"));
});

// ✅ SERVER
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log("🚀 Servidor web activo en puerto", PORT);
});
