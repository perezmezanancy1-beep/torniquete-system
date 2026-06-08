const express = require("express");
const admin = require("firebase-admin");

const app = express();
app.use(express.json()); // 🔥 IMPORTANTE

// ✅ CONFIGURACIÓN FIREBASE DESDE RENDER (ENV)
const serviceAccount = JSON.parse(process.env.FIREBASE_CONFIG);

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: "https://torniquete-universidad-default-rtdb.firebaseio.com"
});

const db = admin.database();

console.log("✅ Firebase conectado");

// ✅ RUTA PRINCIPAL (VALIDACIÓN DE ACCESO)
app.post("/validar", async (req, res) => {

    try {
        const { cedula } = req.body;

        console.log("📡 Petición recibida:", cedula);

        if (!cedula) {
            return res.json({ ok: false, msg: "Sin cédula" });
        }

        // ✅ BUSCAR USUARIO
        const ref = db.ref("usuarios/" + cedula);
        const snap = await ref.once("value");

        if (!snap.exists()) {
            console.log("❌ Usuario no existe:", cedula);
            return res.json({ ok: false });
        }

        let user = snap.val();

        console.log("👤 Usuario:", user.nombre);

        // ✅ LÓGICA AUTOMÁTICA

        // 🔓 ENTRADA
        if (!user.estado || user.estado === "fuera") {

            await ref.update({
                estado: "dentro",
                ultima_entrada: Date.now()
            });

            console.log("✅ ENTRADA:", cedula);

            return res.json({
                ok: true,
                tipo: "entrada"
            });
        }

        // 🚪 SALIDA
        if (user.estado === "dentro") {

            await ref.update({
                estado: "fuera",
                ultima_salida: Date.now()
            });

            console.log("🚪 SALIDA:", cedula);

            return res.json({
                ok: true,
                tipo: "salida"
            });
        }

    } catch (error) {
        console.error("❌ ERROR:", error);
        res.json({ ok: false });
    }

});

// ✅ RUTA DE PRUEBA (MUY IMPORTANTE)
app.get("/", (req, res) => {
    res.send("✅ Servidor torniquete activo");
});

// ✅ PUERTO
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
    console.log("🚀 Servidor corriendo en puerto", PORT);
});