const express = require("express");
const admin = require("firebase-admin");
const path = require("path");

const app = express();
app.use(express.json());

// ✅ SERVIR TODAS LAS PÁGINAS WEB
app.use(express.static("public"));

// ✅ FIREBASE (DESDE RENDER ENV)
const serviceAccount = JSON.parse(process.env.FIREBASE_CONFIG);

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: "https://torniquete-universidad-default-rtdb.firebaseio.com"
});

const db = admin.database();

console.log("✅ Firebase conectado");

// ✅ API VALIDAR (RASPBERRY)
app.post("/validar", async (req, res) => {
  try {

    const { cedula } = req.body;

    if (!cedula) {
      return res.json({ ok: false });
    }

    const ref = db.ref("usuarios/" + cedula);
    const snap = await ref.once("value");

    if (!snap.exists()) {
      return res.json({ ok: false });
    }

    let user = snap.val();

    // ✅ ENTRADA
    if (!user.estado || user.estado === "fuera") {

      await ref.update({ estado: "dentro" });

      return res.json({
        ok: true,
        tipo: "entrada"
      });
    }

    // ✅ SALIDA
    if (user.estado === "dentro") {

      await ref.update({ estado: "fuera" });

      return res.json({
        ok: true,
        tipo: "salida"
      });
    }

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
