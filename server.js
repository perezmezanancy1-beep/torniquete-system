const express = require("express");
const admin = require("firebase-admin");
const path = require("path");
const twilio = require("twilio");

const app = express();
app.use(express.json());


app.use(express.static("public"));

/* =========================
    FIREBASE
========================= */
const serviceAccount = JSON.parse(process.env.FIREBASE_CONFIG);

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: "https://torniquete-universidad-default-rtdb.firebaseio.com"
});

const db = admin.database();

console.log(" Firebase conectado");

/* =========================
    TWILIO CONFIG
========================= */
const client = twilio(
  process.env.TWILIO_SID,
  process.env.TWILIO_TOKEN
);

const numeroTwilio = process.env.TWILIO_NUMERO;

/* =========================
    FUNCIÓN ENVIAR SMS
========================= */
async function enviarSMS(cedula, telefono) {
  try {

    const link = `https://torniquete-system.onrender.com/qr.html?cedula=${cedula}`;

    await client.messages.create({
      body: `UAC ACCESO

Bienvenido a la Universidad Autónoma del Caribe

Su acceso es válido por 7 horas

Ingrese aquí:
${link}`,
      from: numeroTwilio,
      to: "+57" + telefono
    });

    console.log("📱 SMS enviado →", telefono);

  } catch (error) {
    console.log(" Error enviando SMS:", error.message);
  }
}

/* =========================
    REGISTRAR DESDE PANEL
========================= */
app.post("/registrar", async (req, res) => {
  try {

    const data = req.body;

    if (!data.cedula) {
      return res.json({ ok: false });
    }

    //  guardar usuario
    await db.ref("usuarios/" + data.cedula).set(data);

    //  enviar SMS si es visitante
    if (data.tipo === "VISITANTE" && data.celular) {
      await enviarSMS(data.cedula, data.celular);
    }

    console.log(" Usuario registrado:", data.cedula);

    res.json({ ok: true });

  } catch (error) {

    console.log(" Error en registro:", error);
    res.json({ ok: false });

  }
});

/* =========================
    VALIDAR (Raspberry)
========================= */
app.post("/validar", async (req, res) => {
  try {

    const { cedula, qr } = req.body;

    if (!cedula) return res.json({ ok: false });

    const ref = db.ref("usuarios/" + cedula);
    const snap = await ref.once("value");

    if (!snap.exists()) {
      console.log(" Usuario no existe:", cedula);
      return res.json({ ok: false });
    }

    let user = snap.val();

// Solo validar QR si realmente viene QR

// VALIDACIÓN QR
if (qr) {

  console.log("================================");
  console.log("CEDULA:", cedula);
  console.log("QR:", qr);
  console.log("ULTIMO QR:", user.ultimoQR);

  const tiempoQR = parseInt(
    qr.substring(10)
);

  console.log("PARTES QR:", partesQR);

  if (partesQR.length < 2) {

    console.log("⛔ FORMATO QR INVÁLIDO");

    return res.json({
      ok: false
    });
  }

  const tiempoQR = parseInt(
    partesQR[1]
  );

  console.log("TIEMPO QR:", tiempoQR);
  console.log("AHORA:", Date.now());

  const diferencia = Date.now() - tiempoQR;

  console.log("DIFERENCIA:", diferencia);

  if (diferencia > 30000) {

    console.log(
      "⛔ QR EXPIRADO:",
      cedula
    );

    return res.json({
      ok: false
    });
  }

  if (user.ultimoQR === qr) {

    console.log(
      "⛔ QR YA UTILIZADO:",
      cedula
    );

    return res.json({
      ok: false
    });
  }

}


let tipoAcceso = "";


    //  CONTROL VISITANTE (7H)
if (user.tipo === "VISITANTE") {

  const limite = 7 * 60 * 60 * 1000;

  if (!user.inicio) {

    await ref.update({
      inicio: Date.now()
    });

    user.inicio = Date.now();
  }

  if (Date.now() - user.inicio > limite) {

    console.log(
      "⛔ Visitante expirado:",
      cedula
    );

    return res.json({
      ok: false
    });
  }
}

    //  ENTRADA / SALIDA
    if (!user.estado || user.estado === "fuera") {

      await ref.update({ estado: "dentro" });
      tipoAcceso = "entrada";

    } else {
      
      await ref.update({ estado: "fuera" });
      tipoAcceso = "salida";
    }

    //  HISTORIAL
    await db.ref("historial").push({
      cedula: cedula,
      nombre: user.nombre || "Usuario",
      tipo: tipoAcceso,
      fecha: new Date().toISOString()
    });

    console.log(" Acceso:", cedula, tipoAcceso);

if (qr) {

  await ref.update({
    ultimoQR: qr
  });

}

    res.json({
      ok: true,
      tipo: tipoAcceso
    });

  } catch (error) {

    console.log(" Error validación:", error);
    res.json({ ok: false });

  }
});

/* =========================
    RUTA PRINCIPAL
========================= */
app.get("/", (req, res) => {
  res.send(" Sistema activo");
});

/* =========================
    SERVIDOR
========================= */
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log(" Servidor activo en puerto", PORT);
});
