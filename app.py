"""
Bot de WhatsApp para Botas de Equitación
Twilio + Flask + SQLite
"""

from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import sqlite3
import os
from datetime import datetime

app = Flask(__name__)

# ─── Configuración del negocio ─────────────────────────────────
NEGOCIO    = "Botas de Equitación 🥾"
HORARIOS   = "Martes a Viernes 10-18hs | Sábados 10-17hs"
DIRECCION  = ""   # Completar con la dirección real
PRECIOS    = (
    "💰 *Precios - Botas de Equitación*\n\n"
    "• Compostura Express: consultar\n"
    "• Limpieza completa: consultar\n"
    "• Reparación de suela: consultar\n"
    "• Teñido / restauración: consultar\n\n"
    "📞 Para cotización exacta enviá foto de tus botas.\n"
    "Respondé *0* para volver al menú."
)

MENU = (
    f"👋 ¡Bienvenido a *{NEGOCIO}*!\n\n"
    "¿En qué te puedo ayudar?\n\n"
    "1️⃣  Sacar turno - Compostura Express\n"
    "2️⃣  Ver precios\n"
    "3️⃣  Consultar horarios\n"
)

# ─── Base de datos (SQLite) ────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "bot.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS estados (
            telefono   TEXT PRIMARY KEY,
            estado     TEXT DEFAULT 'MENU',
            nombre     TEXT,
            fecha      TEXT,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS turnos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            telefono   TEXT,
            nombre     TEXT,
            fecha      TEXT,
            servicio   TEXT DEFAULT 'Compostura Express',
            estado     TEXT DEFAULT 'confirmado',
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_estado(telefono):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT estado, nombre, fecha FROM estados WHERE telefono = ?", (telefono,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"estado": row[0], "nombre": row[1], "fecha": row[2]}
    return {"estado": "MENU", "nombre": None, "fecha": None}

def set_estado(telefono, estado, nombre=None, fecha=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO estados (telefono, estado, nombre, fecha, updated_at)
        VALUES (?, ?, ?, ?, ?)
    """, (telefono, estado, nombre, fecha, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def guardar_turno(telefono, nombre, fecha):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO turnos (telefono, nombre, fecha, created_at)
        VALUES (?, ?, ?, ?)
    """, (telefono, nombre, fecha, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ─── Lógica del bot ────────────────────────────────────────────
def procesar_mensaje(telefono, mensaje, nombre_perfil=""):
    msg = mensaje.strip()
    datos = get_estado(telefono)
    estado = datos["estado"]

    if msg in ("0", "menu", "menú", "inicio") or msg.lower() == "hola":
        set_estado(telefono, "ESPERANDO_OPCION")
        return MENU

    if estado == "MENU":
        set_estado(telefono, "ESPERANDO_OPCION")
        saludo = f"Hola *{nombre_perfil}*! " if nombre_perfil else "¡Hola! "
        return saludo + "\n\n" + MENU

    elif estado == "ESPERANDO_OPCION":
        if msg == "1":
            set_estado(telefono, "ESPERANDO_NOMBRE")
            return (
                "📝 Vamos a reservar tu turno para *Compostura Express*.\n\n"
                "¿Cuál es tu nombre y apellido?"
            )
        elif msg == "2":
            return PRECIOS
        elif msg == "3":
            return (
                f"🕐 *Horarios de atención:*\n\n"
                f"{HORARIOS}\n\n"
                "Respondé *0* para volver al menú."
            )
        else:
            return (
                "No entendí tu opción 😅 Por favor elegí:\n\n"
                "1️⃣  Sacar turno\n"
                "2️⃣  Ver precios\n"
                "3️⃣  Consultar horarios"
            )

    elif estado == "ESPERANDO_NOMBRE":
        set_estado(telefono, "ESPERANDO_FECHA", nombre=msg)
        return (
            f"Perfecto, *{msg}*! 📅\n\n"
            "¿Qué día y horario te queda bien?\n"
            f"Atendemos: {HORARIOS}\n\n"
            "_Ejemplo: martes 10 de junio por la mañana_"
        )

    elif estado == "ESPERANDO_FECHA":
        nombre = datos["nombre"]
        set_estado(telefono, "CONFIRMANDO", nombre=nombre, fecha=msg)
        return (
            "✅ *Confirmá tu turno:*\n\n"
            f"👤 Nombre: {nombre}\n"
            f"📅 Fecha preferida: {msg}\n"
            f"🔧 Servicio: Compostura Express\n\n"
            "Respondé *SI* para confirmar o *NO* para cancelar."
        )

    elif estado == "CONFIRMANDO":
        if msg.upper() in ("SI", "SÍ", "S", "YES", "OK"):
            nombre = datos["nombre"]
            fecha  = datos["fecha"]
            guardar_turno(telefono, nombre, fecha)
            set_estado(telefono, "ESPERANDO_OPCION")
            return (
                f"🎉 *¡Turno confirmado!*\n\n"
                f"👤 {nombre}\n"
                f"📅 {fecha}\n"
                f"🔧 Compostura Express\n\n"
                f"📍 Te esperamos. Horarios: {HORARIOS}\n\n"
                "Respondé *0* si necesitás algo más 😊"
            )
        elif msg.upper() in ("NO", "N", "CANCELAR"):
            set_estado(telefono, "ESPERANDO_OPCION")
            return "Turno cancelado. ¿En qué más te puedo ayudar?\n\n" + MENU
        else:
            return "Por favor respondé *SI* para confirmar o *NO* para cancelar."

    else:
        set_estado(telefono, "ESPERANDO_OPCION")
        return MENU


# ─── Endpoints ─────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    telefono      = request.form.get("From", "")
    mensaje       = request.form.get("Body", "")
    nombre_perfil = request.form.get("ProfileName", "")

    respuesta = procesar_mensaje(telefono, mensaje, nombre_perfil)

    twiml = MessagingResponse()
    twiml.message(respuesta)
    return Response(str(twiml), mimetype="text/xml", status=200)


@app.route("/turnos")
def ver_turnos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nombre, telefono, fecha, servicio, estado, created_at FROM turnos ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()

    html = "<h2>📅 Turnos - Botas de Equitación</h2><table border='1' cellpadding='8'>"
    html += "<tr><th>Nombre</th><th>Teléfono</th><th>Fecha</th><th>Servicio</th><th>Estado</th><th>Registrado</th></tr>"
    for r in rows:
        html += f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td><td>{r[4]}</td><td>{r[5][:16]}</td></tr>"
    html += "</table>"
    return html


@app.route("/")
def home():
    return f"Bot {NEGOCIO} activo", 200


# ─── Inicio ────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
