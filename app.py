import os
import json
import threading
import time
import requests as req_lib
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from pyairtable import Api

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

conversation_history = {}

BASE_GITHUB = "https://raw.githubusercontent.com/carlosalegre203-oss/whatsapp-bot/main/medidas/"
INSTRUCTIVO_MEDIDAS = [
    BASE_GITHUB + "medida_1_.jpg",
    BASE_GITHUB + "medida_2.jpg",
    BASE_GITHUB + "medida_3.jpg",
    BASE_GITHUB + "medida_4.jpg",
    BASE_GITHUB + "medida_5.jpg",
    BASE_GITHUB + "medida_6.jpg",
    BASE_GITHUB + "medida_7.jpg",
    BASE_GITHUB + "altura_.jpg",
    BASE_GITHUB + "largo_de_pie.jpg",
    BASE_GITHUB + "guia_10.jpg",
]

# ─────────────────────────────────────────
# AIRTABLE
# ─────────────────────────────────────────
def crear_turno_airtable(nombre, telefono_chat, servicio, fecha, hora, es_urgente=False, telefono_cliente=None):
    api_token  = os.environ.get("AIRTABLE_API_TOKEN")
    base_id    = os.environ.get("AIRTABLE_BASE_ID")
    table_name = os.environ.get("AIRTABLE_TABLE_NAME", "Turnos")

    if not api_token or not base_id:
        print("⚠️  Airtable no configurado")
        return {"ok": False, "error": "Airtable no configurado"}

    try:
        api   = Api(api_token)
        table = api.table(base_id, table_name)

        tel_notificacion = telefono_cliente if telefono_cliente else telefono_chat

        record = table.create({
            "Cliente":  nombre,
            "Teléfono": tel_notificacion,
            "Servicio": servicio,
            "Fecha":    fecha,
            "Hora":     hora,
            "Urgente":  es_urgente,
            "Estado":   "Pendiente",
            "Pago":     "100% al reservar" if es_urgente else "50% al reservar",
        })

        print(f"✅ Turno creado en Airtable: {record['id']}")
        return {"ok": True, "record_id": record["id"]}

    except Exception as e:
        print(f"Error Airtable: {e}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────
# ENVIAR FOTOS DEL CATÁLOGO DE BOTAS
# ─────────────────────────────────────────
def enviar_catalogo_botas(telefono_destino, categoria=None):
    try:
        api_token = os.environ.get("AIRTABLE_API_TOKEN")
        base_id   = os.environ.get("AIRTABLE_BASE_ID")
        api = Api(api_token)
        tabla = api.table(base_id, "Catálogo de Botas")

        if categoria:
            registros = tabla.all(formula=f"{{Categoría}} = '{categoria}'")
        else:
            registros = tabla.all()

        if not registros:
            return {"ok": False, "error": "Sin registros"}

        twilio = TwilioClient(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        to = telefono_destino if telefono_destino.startswith("whatsapp:") else f"whatsapp:{telefono_destino}"

        for r in registros:
            f = r["fields"]
            nombre  = f.get("Nombre", "Modelo")
            precio  = f.get("Precio", "")
            foto_url = f.get("Foto URL", "")
            if not foto_url:
                continue
            caption = f"🥾 *{nombre}*\n💰 ${precio:,.0f}" if isinstance(precio, (int, float)) else f"🥾 *{nombre}*\n💰 {precio}"
            twilio.messages.create(from_=whatsapp_from, to=to, body=caption, media_url=[foto_url])

        print(f"📸 Catálogo enviado a {telefono_destino} ({len(registros)} modelos)")
        return {"ok": True, "enviados": len(registros)}

    except Exception as e:
        print(f"Error enviando catálogo: {e}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────
# ENVIAR INSTRUCTIVO DE MEDIDAS POR WHATSAPP
# ─────────────────────────────────────────
def enviar_instructivo(telefono_destino):
    try:
        twilio = TwilioClient(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        to = telefono_destino if telefono_destino.startswith("whatsapp:") else f"whatsapp:{telefono_destino}"

        for url in INSTRUCTIVO_MEDIDAS:
            twilio.messages.create(from_=whatsapp_from, to=to, media_url=[url])

        print(f"📏 Instructivo de medidas enviado a {telefono_destino}")
        return {"ok": True}
    except Exception as e:
        print(f"Error enviando instructivo: {e}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────
# GUARDAR FOTOS EN AIRTABLE
# Descarga desde Twilio con auth y sube directo a Airtable
# ─────────────────────────────────────────
def guardar_fotos_airtable(telefono, media_urls):
    api_token    = os.environ.get("AIRTABLE_API_TOKEN")
    base_id      = os.environ.get("AIRTABLE_BASE_ID")
    twilio_sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")

    if not api_token or not base_id:
        return False, "error"

    try:
        # Descargar imágenes desde Twilio (requieren autenticación)
        imagenes = []
        for url in media_urls:
            r = req_lib.get(url, auth=(twilio_sid, twilio_token), timeout=15)
            if r.ok:
                content_type = r.headers.get("Content-Type", "image/jpeg")
                imagenes.append((r.content, content_type))
            else:
                print(f"Error descargando imagen de Twilio: {r.status_code}")

        if not imagenes:
            return False, "error"

        api = Api(api_token)

        # Determinar destino: turno activo o tabla Medidas
        turnos_table = api.table(base_id, os.environ.get("AIRTABLE_TABLE_NAME", "Turnos"))
        turnos = turnos_table.all(
            formula=f"AND({{Teléfono}} = '{telefono}', OR({{Estado}} = 'Pendiente', {{Estado}} = 'En taller'))"
        )

        if turnos:
            record_id  = turnos[0]["id"]
            table_name = os.environ.get("AIRTABLE_TABLE_NAME", "Turnos")
            field_name = "Foto"
            destino    = "compostura"
        else:
            medidas_table = api.table(base_id, os.environ.get("AIRTABLE_MEDIDAS_TABLE", "Medidas"))
            registros = medidas_table.all(formula=f"{{Teléfono}} = '{telefono}'")
            if registros:
                record_id = registros[0]["id"]
            else:
                nuevo = medidas_table.create({"Teléfono": telefono})
                record_id = nuevo["id"]
            table_name = os.environ.get("AIRTABLE_MEDIDAS_TABLE", "Medidas")
            field_name = "Fotos"
            destino    = "medidas"

        # Subir cada imagen a Airtable usando su API de adjuntos
        upload_url = f"https://content.airtable.com/v0/{base_id}/{record_id}/uploadAttachment"
        headers = {"Authorization": f"Bearer {api_token}"}

        for i, (img_bytes, content_type) in enumerate(imagenes):
            ext      = "jpg" if "jpeg" in content_type else content_type.split("/")[-1]
            filename = f"foto_{i + 1}.{ext}"
            resp = req_lib.post(
                upload_url,
                headers=headers,
                files={"file": (filename, img_bytes, content_type)},
                data={"fieldName": field_name},
                timeout=30
            )
            if not resp.ok:
                print(f"Error subiendo foto a Airtable: {resp.status_code} {resp.text}")

        print(f"📸 Fotos guardadas en {destino} para {telefono}")
        return True, destino

    except Exception as e:
        print(f"Error guardando fotos: {e}")
        return False, "error"


# ─────────────────────────────────────────
# FUNCTION CALLING — definición para OpenAI
# ─────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "crear_turno",
            "description": (
                "Crea un turno cuando el cliente confirma explícitamente la reserva "
                "y se tienen todos los datos. NO llamar si falta nombre, fecha, hora o servicio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre_cliente": {
                        "type": "string",
                        "description": "Nombre completo del cliente"
                    },
                    "servicio": {
                        "type": "string",
                        "description": "Tipo de reparación. Ej: 'Cambio de cierre - unidad', 'Cambio de fondo', etc."
                    },
                    "fecha": {
                        "type": "string",
                        "description": "Fecha del turno en formato YYYY-MM-DD"
                    },
                    "hora": {
                        "type": "string",
                        "description": "Hora del turno en formato HH:MM (entre 10:00 y 12:00)"
                    },
                    "es_urgente": {
                        "type": "boolean",
                        "description": "True si el cliente pidió urgente (pago 100%), False si es normal (pago 50%)"
                    },
                    "telefono_cliente": {
                        "type": "string",
                        "description": "Número de WhatsApp del cliente (solo cuando el staff agenda a un cliente del local). Formato: +549XXXXXXXXXX. Si el cliente se está agendando solo, omitir este campo."
                    }
                },
                "required": ["nombre_cliente", "servicio", "fecha", "hora", "es_urgente"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "enviar_catalogo_botas",
            "description": (
                "Envía al cliente las fotos de los modelos de botas disponibles con nombre y precio. "
                "Llamar cuando el cliente pida ver modelos, fotos de botas, o quiera saber qué modelos tienen. "
                "Si el cliente especifica una categoría (Mujer, Hombre, Militar, Unisex), pasarla como parámetro."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria": {
                        "type": "string",
                        "description": "Categoría a filtrar: Mujer, Hombre, Militar, Unisex. Omitir para mostrar todos los modelos."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "enviar_instructivo_medidas",
            "description": (
                "Envía al cliente las fotos del instructivo de medidas para fabricar botas nuevas a medida. "
                "Llamar cuando el cliente quiere botas nuevas y necesita saber cómo tomarse las medidas, "
                "o cuando pide el instructivo o las fotos de medidas."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]


# ─────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────
SYSTEM_PROMPT = """Sos Carmelo, el asistente de WhatsApp de Carmelo Riding Boots — una talabartería artesanal especializada en botas de equitación, ubicada en Buenos Aires, Argentina.

Carmelo Riding Boots ES tu marca. Cuando alguien pregunta por botas Carmelo, estás hablando de las botas que fabricamos nosotros.

---

SERVICIOS

Ofrecemos dos servicios principales:

1. Composturas (reparaciones)
Hacemos composturas de CUALQUIER marca de bota de equitación — no solo botas Carmelo. Cualquier bota que el cliente traiga, la arreglamos.

2. Botas nuevas a medida
Fabricamos botas de cuero artesanales a medida.

---

PRECIOS DE COMPOSTURAS

Estos son los precios estándar para botas de cualquier marca:

- Cambio de cierre — unidad: $72.000
- Cambio de cierre — par: $144.000
- Colocar parche en caña — unidad: $96.000
- Colocar parche en caña — par: $192.000
- Cambio de fondo (base + taco): $192.000
- Cambio de cordones / elásticos: $24.000
- Par de cueritos para broche: $36.000
- Lustrado: $18.000
- Costuras chicas / arreglo mínimo: $12.000
- Agrandar caña (horma de madera, mín 2 días): $96.000
- Colocar banda elástica: $192.000
- Otros arreglos: A cotizar — el cliente deja la consulta

DESCUENTO CLIENTES CARMELO (20% off)

Si el cliente trae botas Carmelo (nuestra propia marca), aplicá un 20% de descuento sobre el precio estándar.

Precios con descuento Carmelo (20% off):
- Cambio de cierre — unidad: $57.600
- Cambio de cierre — par: $115.200
- Colocar parche en caña — unidad: $76.800
- Colocar parche en caña — par: $153.600
- Cambio de fondo (base + taco): $153.600
- Cambio de cordones / elásticos: $19.200
- Par de cueritos para broche: $28.800
- Lustrado: $14.400
- Costuras chicas / arreglo mínimo: $9.600
- Agrandar caña (horma de madera, mín 2 días): $76.800
- Colocar banda elástica: $153.600

FLUJO PARA COMPOSTURAS

Cuando un cliente consulta por una compostura, preguntá siempre:
"¿Son botas Carmelo o de otra marca?"
- Si son Carmelo → aplicá los precios con descuento y mencioná el beneficio
- Si son de otra marca → aplicá los precios estándar

SIEMPRE informá el precio exacto cuando lo tenés.

---

CROSS-SELL

Cuando el cliente trae botas de OTRA marca para reparar, una vez confirmado el turno:
"Por cierto, si en algún momento querés hacerte de un par de botas Carmelo, todas las reparaciones futuras te salen con un 20% de descuento permanente. Son botas artesanales a medida — si te interesa te cuento más 🥾"

---

UBICACIÓN Y HORARIOS

Local Palermo:
- Dirección: Dorrego 4045, Club Alemán de Equitación
- Acceso: Sector Caballerizas, ingreso por Julio Argentino Roca y Ramón J. Cárcano
- Horarios: Martes a Viernes 10:00 a 18:00hs | Sábados 10:00 a 17:00hs

---

SISTEMA DE TURNOS

Franjas horarias: Recepción 10:00 a 12:00hs | Retiro desde las 18:00hs

Modalidades de pago:
- Urgente: 100% al reservar — alias carmelo.palermo
- Normal: 50% al reservar + 50% al retirar — alias carmelo.palermo
- Presencial: pago al retirar, sin seña

FLUJO PARA SACAR TURNO (seguí este orden):

Paso 1 — Preguntá el nombre: "¿Me decís tu nombre para registrar el turno?"
Paso 2 — Preguntá si es urgente: "¿Lo necesitás urgente o puede esperar el tiempo normal?"
Paso 3 — Pedí día y hora: "¿Qué día y hora preferís? Recibimos botas de 10 a 12hs."
Paso 4 — SOLO si el mensaje viene del staff agendando a un cliente del local (se nota porque dicen "agendo para", "el cliente es", "te paso el número", etc.): preguntá el número de WhatsApp del cliente: "¿Cuál es el número de WhatsApp del cliente para avisarle cuando esté lista la reparación?"
Paso 5 — Confirmá todo antes de registrar:
  "Perfecto [Nombre], te anoto:
  📋 Servicio: [servicio]
  📅 Fecha y hora: [día] a las [hora]
  ⚡ Modalidad: [urgente / normal]
  ¿Todo correcto?"
Paso 6 — Cuando el cliente confirme → llamá a la función crear_turno INMEDIATAMENTE (incluí telefono_cliente si lo tenés). NO esperes ningún comprobante para crear el turno. El turno se registra en el momento que el cliente dice "sí".
Paso 7 — Después de que crear_turno se ejecute, informá el pago:
  - Urgente: "¡Turno registrado! ✅ Para completar la reserva transferí $[monto] al alias carmelo.palermo y mandanos el comprobante."
  - Normal: "¡Turno registrado! ✅ Para completar la reserva transferí $[50%] al alias carmelo.palermo. El resto lo abonás al retirar."

IMPORTANTE: El orden es SIEMPRE: 1) cliente confirma → 2) llamás a crear_turno → 3) pedís el pago. NUNCA pidas el comprobante antes de registrar el turno.

---

BOTAS NUEVAS A MEDIDA — FLUJO COMPLETO

Seguí SIEMPRE este orden. No saltees pasos.

PASO 1 — PRECIOS GENERALES
Cuando el cliente pregunta por botas nuevas o precios, respondé con esto:

"🥾 Fabricamos botas de cuero artesanales a medida. Los precios son:

📌 Algo Clásico: $520.000
📌 Modelos intermedios: desde $540.000 hasta $650.000 según modelo y terminación

⏱ Tiempos de confección:
• Express (15 días): 100% al encargar — solo efectivo o transferencia
• Estándar (35 días): 50% al encargar + 50% al retirar — efectivo o transferencia

¿Querés ver los modelos disponibles?"

PASO 2 — MOSTRAR MODELOS (solo si el cliente quiere seguir)
Si el cliente dice que sí quiere ver modelos, llamá a enviar_catalogo_botas para enviarle las fotos con nombre y precio. Si mencionó una categoría específica (Mujer, Hombre, Militar, Unisex), pasala como parámetro. Avisale que le vas a mandar las fotos ahora.

Referencia de modelos disponibles:

Modelos disponibles:
1. Clásico Marrón Militar → $480.000
2. Algo Clásico → $520.000
3. Zapabotas (cierre atrás o costado) → $540.000 - $560.000
4. Cierre al costado, punta redonda, cordones elásticos → $560.000
5. Cierre atrás, punta redonda, cordones elásticos, aplique visera charol → $590.000
6. Cierre atrás, punta redonda, aplique charol con vivos → $620.000
7. Cierre atrás, punta redonda, banda elástica en caña, aplique charol con strass → $620.000
8. Cierre al costado, punta redonda, cordones elásticos, aplique glitter y strass → $650.000

Preguntá: "¿Alguno te llama la atención o querés que te cuente más de alguno?"

PASO 3 — CERRAR LA VENTA
Cuando el cliente elige un modelo, confirmá:
- Modelo elegido
- Modalidad de entrega (Express 15 días o Estándar 35 días)
- Forma de pago (efectivo o transferencia)
- Monto a abonar al encargar

Preguntá: "¿Lo confirmamos?"

PASO 4 — INSTRUCTIVO DE MEDIDAS (solo después de que confirme)
Cuando el cliente confirme el encargo → llamá a enviar_instructivo_medidas y decile:
"¡Perfecto! Te mando ahora el instructivo de medidas. Son unas fotos con las instrucciones para tomarte las medidas vos mismo/a. Cuando las tengas, mandámelas por acá 📏"

REGLA CRÍTICA: NUNCA mandés el instructivo antes de que el cliente elija modelo y confirme el encargo.

---

PERSONALIDAD Y TONO

- Idioma: Detectá el idioma y respondé siempre en ese idioma.
- Tono: Cálido, cercano, directo. Español rioplatense (vos, podés, querés).
- Emojis: Con moderación.
- Mensajes cortos: WhatsApp, no ensayos.

---

REGLAS CRÍTICAS

1. Aceptamos composturas de cualquier marca.
2. Carmelo ES nuestra marca propia.
3. Siempre informá el precio exacto de la lista.
4. Siempre dá la dirección completa cuando te la pidan.
5. No inventés información.
6. NUNCA llamés a crear_turno sin tener nombre, servicio, fecha y hora confirmados por el cliente.
7. NUNCA llamés a enviar_instructivo_medidas sin que el cliente haya elegido un modelo y confirmado el encargo.
"""


# ─────────────────────────────────────────
# GPT — con soporte de function calling
# ─────────────────────────────────────────
def get_gpt_response(user_phone: str, user_message: str) -> str:
    if user_phone not in conversation_history:
        conversation_history[user_phone] = []

    conversation_history[user_phone].append({
        "role": "user",
        "content": user_message
    })

    if len(conversation_history[user_phone]) > 20:
        conversation_history[user_phone] = conversation_history[user_phone][-20:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history[user_phone],
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=400,
            temperature=0.7
        )

        message = response.choices[0].message
        assistant_message = None

        if message.tool_calls:
            tool_call = message.tool_calls[0]
            tool_name = tool_call.function.name

            if tool_name == "crear_turno":
                args = json.loads(tool_call.function.arguments)
                print(f"📅 Creando turno: {args}")
                result = crear_turno_airtable(
                    nombre=args["nombre_cliente"],
                    telefono_chat=user_phone,
                    servicio=args["servicio"],
                    fecha=args["fecha"],
                    hora=args["hora"],
                    es_urgente=args.get("es_urgente", False),
                    telefono_cliente=args.get("telefono_cliente")
                )

            elif tool_name == "enviar_catalogo_botas":
                args = json.loads(tool_call.function.arguments)
                categoria = args.get("categoria")
                print(f"📸 Enviando catálogo a {user_phone} (categoría: {categoria})")
                result = enviar_catalogo_botas(user_phone, categoria)

            elif tool_name == "enviar_instructivo_medidas":
                print(f"📏 Enviando instructivo a {user_phone}")
                result = enviar_instructivo(user_phone)

            else:
                result = {"ok": False, "error": "función desconocida"}

            conversation_history[user_phone].append({
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call.model_dump()]
            })
            conversation_history[user_phone].append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result)
            })

            final = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history[user_phone],
                max_tokens=400,
                temperature=0.7
            )
            assistant_message = final.choices[0].message.content

        else:
            assistant_message = message.content

        conversation_history[user_phone].append({
            "role": "assistant",
            "content": assistant_message
        })

        return assistant_message

    except Exception as e:
        print(f"Error OpenAI: {e}")
        return "Hola! En este momento tenemos una dificultad técnica. Por favor escribinos en unos minutos o llamanos directamente. Disculpá las molestias 🙏"


# ─────────────────────────────────────────
# NOTIFICACIÓN: reparación lista
# Llamado por la automatización de Airtable
# ─────────────────────────────────────────
@app.route("/notificar", methods=["POST"])
def notificar():
    data = request.get_json()

    if data.get("secret") != os.environ.get("NOTIFY_SECRET"):
        return {"error": "Unauthorized"}, 401

    phone    = data.get("phone", "").strip()
    nombre   = data.get("nombre", "cliente").strip()
    servicio = data.get("servicio", "tu reparación").strip()

    if not phone:
        return {"error": "Falta el teléfono"}, 400

    msg = (
        f"¡Hola {nombre}! 🥾 Tu reparación ({servicio}) ya está lista para retirar.\n\n"
        f"Podés pasar a partir de las 18:00hs 📍 Dorrego 4045, Club Alemán de Equitación.\n\n"
        f"¡Gracias por elegirnos!"
    )

    try:
        twilio = TwilioClient(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        to = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"

        twilio.messages.create(body=msg, from_=whatsapp_from, to=to)
        print(f"✅ Notificación enviada a {phone}")
        return {"ok": True}

    except Exception as e:
        print(f"Error enviando notificación: {e}")
        return {"ok": False, "error": str(e)}, 500


# ─────────────────────────────────────────
# WEBHOOK PRINCIPAL
# ─────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    from_number  = request.values.get("From", "")
    num_media    = int(request.values.get("NumMedia", 0))

    print(f"Mensaje de {from_number}: {incoming_msg} | Fotos: {num_media}")

    if num_media > 0:
        media_urls = [
            request.values.get(f"MediaUrl{i}")
            for i in range(num_media)
        ]
        guardado, destino = guardar_fotos_airtable(from_number, media_urls)

        resp = MessagingResponse()
        if guardado and destino == "compostura":
            resp.message("¡Recibí la foto de tu bota! 📸 La guardé en tu ficha de reparación. Quedás tranquilo/a que no se pierde.")
        elif guardado and destino == "medidas":
            resp.message("¡Recibí tus fotos de medidas! 📸 Las guardé en tu ficha. Enseguida las revisamos y te contactamos.")
        else:
            resp.message("Recibí tus fotos, gracias. En breve te contactamos.")
        return str(resp)

    if not incoming_msg:
        return str(MessagingResponse())

    reply = get_gpt_response(from_number, incoming_msg)
    print(f"Respuesta: {reply}")

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/", methods=["GET"])
def health():
    return "Bot de Botas de Equitacion - OK 🥾", 200


# ─────────────────────────────────────────
# POLLING: detecta turnos con Estado=Lista
# ─────────────────────────────────────────
def notificar_turnos_listos():
    api_token = os.environ.get("AIRTABLE_API_TOKEN")
    base_id   = os.environ.get("AIRTABLE_BASE_ID")

    if not api_token or not base_id:
        print("⚠️  Polling desactivado: faltan vars de Airtable")
        return

    try:
        api   = Api(api_token)
        table = api.table(base_id, os.environ.get("AIRTABLE_TABLE_NAME", "Turnos"))

        pendientes = table.all(
            formula="AND({Estado} = 'Lista', NOT({Notificado}))"
        )

        if not pendientes:
            return

        twilio_client = TwilioClient(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

        for record in pendientes:
            fields   = record["fields"]
            phone    = fields.get("Teléfono", "").strip()
            nombre   = fields.get("Cliente", "cliente").strip()
            servicio = fields.get("Servicio", "tu reparación").strip()

            if not phone:
                print(f"⚠️  Turno {record['id']} sin teléfono, saltando")
                continue

            table.update(record["id"], {"Notificado": True})

            msg = (
                f"¡Hola {nombre}! 🥾 Tu reparación ({servicio}) ya está lista para retirar.\n\n"
                f"Podés pasar a partir de las 18:00hs 📍 Dorrego 4045, Club Alemán de Equitación.\n\n"
                f"¡Gracias por elegirnos! — Carmelo Riding Boots"
            )

            to = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
            twilio_client.messages.create(body=msg, from_=whatsapp_from, to=to)
            print(f"✅ Notificación enviada a {phone} ({nombre})")

    except Exception as e:
        print(f"Error en polling de notificaciones: {e}")


def polling_loop():
    while True:
        time.sleep(300)
        notificar_turnos_listos()


_polling_thread = threading.Thread(target=polling_loop, daemon=True)
_polling_thread.start()
print("🔄 Polling de notificaciones iniciado (cada 5 min)")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
