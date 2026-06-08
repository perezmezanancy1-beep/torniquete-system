const express = require("express");
const admin = require("firebase-admin");

const app = express();
app.use(express.json());

// 🔥 CARGA TU JSON DE FIREBASE
const serviceAccount = require("./firebase-admin.json");

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: "https://torniquete-universidad-default-rtdb.firebaseio.com"
});

const db = admin.database();

// ✅ RUTA PRINCIPAL (VALIDAR)
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

        // ✅ entrada
        if (!user.estado || user.estado === "fuera") {

            await ref.update({ estado: "dentro" });

            return res.json({
                ok: true,
                tipo: "entrada"
            });
        }

        // ✅ salida
        if (user.estado === "dentro") {

            await ref.update({ estado: "fuera" });

            return res.json({
                ok: true,
                tipo: "salida"
            });
        }

    } catch (error) {
        console.log(error);
        res.json({ ok: false });
    }
});

// ✅ SERVIDOR
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
    console.log("✅ Servidor corriendo en puerto", PORT);
});