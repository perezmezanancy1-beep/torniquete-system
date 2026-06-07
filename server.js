const express = require("express");

const app = express();
app.use(express.json());

// ✅ SERVIR ARCHIVOS (IMPORTANTE)
app.use(express.static(__dirname));

// ✅ VALIDAR QR (para Raspberry)
app.post("/validar-qr", (req, res) => {
    try {
        const { token } = req.body;
        const data = JSON.parse(token);

        console.log("✅ QR recibido:", data);

        res.json({ ok: true });

    } catch (err) {
        res.json({ ok: false });
    }
});

// ✅ PUERTO RENDER
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
    console.log("✅ SERVIDOR ACTIVO EN " + PORT);
});