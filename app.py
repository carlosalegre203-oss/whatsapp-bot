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
esperando_foto = {}  # phone → record_id: staff esperando adjuntar foto al turno recién creado

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
CODIGOS_STAFF = {
    "LOCAL4045": "Palermo",
    "LOCAL443":  "Alberti",
    "LOCAL1980":  "Producción",
}

def crear_turno_airtable(nombre, telefono_chat, servicio, fecha, hora, es_urgente=False, telefono_cliente=None, local=None, pago=None):
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

        fields = {
            "Cliente":  nombre,
            "Teléfono": tel_notificacion,
            "Servicio": servicio,
            "Fecha":    fecha,
            "Hora":     hora,
            "Urgente":  es_urgente,
            "Estado":   "Pendiente",
            "Pago":     pago if pago else ("100% al reservar" if es_urgente else "50% al reservar"),
        }
        if local:
            fields["Local"] = local

        record = table.create(fields)

        print(f"✅ Turno creado en Airtable: {record['id']}")
        return {"ok": True, "record_id": record["id"]}

    except Exception as e:
        print(f"Error Airtable: {e}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────
# CREAR PEDIDO EN AIRTABLE (botas nuevas)
# ─────────────────────────────────────────
def crear_pedido_airtable(nombre, telefono_chat, modelo, textura, modalidad, precio_total, telefono_cliente=None, local=None, notas=None, fecha_encargo=None, fecha_entrega=None):
    api_token = os.environ.get("AIRTABLE_API_TOKEN")
    base_id   = os.environ.get("AIRTABLE_BASE_ID")

    if not api_token or not base_id:
        return {"ok": False, "error": "Airtable no configurado"}

    try:
        api   = Api(api_token)
        table = api.table(base_id, "Pedidos")

        # Seña: 50% estándar, 100% express
        sena = precio_total if modalidad == "Express" else round(precio_total * 0.5)

        tel = telefono_cliente if telefono_cliente else telefono_chat

        fields = {
            "Cliente":             nombre,
            "Contacto (WhatsApp)": tel,
            "Modelo":              modelo,
            "Precio":              precio_total,
            "Sena cobrada":        sena,
            "Estado":              "Pendiente",
        }
        if fecha_encargo:
            fields["Fecha de encargue"] = fecha_encargo
        if fecha_entrega:
            fields["Fecha de entrega"] = fecha_entrega
        if notas:
            fields["Notas"] = notas

        record = table.create(fields)
        print(f"✅ Pedido creado en Airtable: {record['id']}")
        return {"ok": True, "record_id": record["id"]}

    except Exception as e:
        print(f"Error creando pedido: {e}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────
# REGISTRAR BOTA EN PRODUCCIÓN (ya en curso)
# ─────────────────────────────────────────
def registrar_produccion_airtable(nombre_cliente, modelo, etapa_actual, local=None, telefono_cliente=None, notas=None):
    api_token = os.environ.get("AIRTABLE_API_TOKEN")
    base_id   = os.environ.get("AIRTABLE_BASE_ID")

    if not api_token or not base_id:
        return {"ok": False, "error": "Airtable no configurado"}

    try:
        api = Api(api_token)

        # Orden de etapas — para tildar todas las anteriores a la actual
        ORDEN_ETAPAS = [
            "Corte", "Seleccion de horma", "Costura", "Colocar fondos",
            "Clavar tacos", "Colocar cordones", "Cueritos de broche",
            "Chapita de marca", "Catalonera", "Empopiado de cana",
            "Lustrado", "Control de calidad"
        ]

        # Construir texto de detalle
        detalle_parts = [f"{nombre_cliente} — {modelo}"]
        if local:
            detalle_parts.append(f"Local: {local}")
        if notas:
            detalle_parts.append(notas)
        detalle_texto = " | ".join(detalle_parts)

        # Marcar como True todas las etapas hasta la actual (inclusive)
        fields = {"Detalle": detalle_texto}
        etapa_norm = etapa_actual.strip()
        if etapa_norm in ORDEN_ETAPAS:
            idx = ORDEN_ETAPAS.index(etapa_norm)
            for e in ORDEN_ETAPAS[:idx + 1]:
                fields[e] = True

        # Escribir en Produccion
        prod_table = api.table(base_id, "Produccion")
        try:
            prod = prod_table.create(fields)
        except Exception:
            prod = prod_table.create({"Detalle": detalle_texto})

        print(f"✅ Bota en producción registrada: {prod['id']}")
        return {"ok": True, "prod_id": prod["id"]}

    except Exception as e:
        print(f"Error registrando producción: {e}")
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

        # Si el staff acaba de registrar un turno, adjuntar al turno recién creado
        if telefono in esperando_foto:
            record_id  = esperando_foto.pop(telefono)
            table_name = os.environ.get("AIRTABLE_TABLE_NAME", "Turnos")
            field_name = "Foto"
            upload_url = f"https://content.airtable.com/v0/{base_id}/{record_id}/uploadAttachment"
            headers_at = {"Authorization": f"Bearer {api_token}"}
            for i, (img_bytes, content_type) in enumerate(imagenes):
                ext      = "jpg" if "jpeg" in content_type else content_type.split("/")[-1]
                filename = f"foto_{i + 1}.{ext}"
                resp = req_lib.post(upload_url, headers=headers_at,
                                    files={"file": (filename, img_bytes, content_type)},
                                    data={"fieldName": field_name}, timeout=30)
                if not resp.ok:
                    print(f"Error subiendo foto a Airtable: {resp.status_code} {resp.text}")
            print(f"📸 Foto staff adjuntada al turno {record_id}")
            return True, "compostura"

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
                    },
                    "local": {
                        "type": "string",
                        "description": "Local de origen del turno. 'Palermo' si el staff usó LOCAL4045, 'Alberti' si usó LOCAL443. Omitir si el cliente se agendó solo."
                    },
                    "pago": {
                        "type": "string",
                        "description": "Modalidad de pago. Solo en modo staff: 'Sin cargo', 'Efectivo en local', '50% al reservar', '100% al reservar'. Omitir si el cliente se agendó solo (se calcula automáticamente)."
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
            "name": "crear_pedido",
            "description": (
                "Registra un encargo de botas nuevas en Airtable cuando el cliente confirma el pedido. "
                "Llamar SOLO cuando el cliente ya confirmó: modelo, textura, modalidad y forma de pago."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre_cliente": {
                        "type": "string",
                        "description": "Nombre del cliente"
                    },
                    "modelo": {
                        "type": "string",
                        "description": "Nombre del modelo elegido (ej: 'Algo Clásico', 'Modelo campo')"
                    },
                    "textura": {
                        "type": "string",
                        "description": "Textura de cuero: 'Suave', 'Intermedio' o 'Intermedio grueso'"
                    },
                    "modalidad": {
                        "type": "string",
                        "description": "'Express' (15 días, 100% al encargar) o 'Estándar' (35 días, 50% al encargar)"
                    },
                    "precio_total": {
                        "type": "number",
                        "description": "Precio total en pesos argentinos (número, sin signo $)"
                    },
                    "telefono_cliente": {
                        "type": "string",
                        "description": "Teléfono del cliente. Formato +549XXXXXXXXXX. En modo staff, pasarlo si el staff lo tiene. Si no lo tiene, usar '1111'."
                    },
                    "local": {
                        "type": "string",
                        "description": "Solo en modo staff: 'Palermo' o 'Alberti'. Omitir si el cliente se agendó solo."
                    },
                    "fecha_encargo": {
                        "type": "string",
                        "description": "Fecha en que se encargó la bota. Formato YYYY-MM-DD."
                    },
                    "fecha_entrega": {
                        "type": "string",
                        "description": "Fecha de entrega prometida al cliente. Formato YYYY-MM-DD."
                    },
                    "notas": {
                        "type": "string",
                        "description": "Observaciones adicionales del pedido. Ej: 'seña cobrada en efectivo', 'talla especial', 'pendiente de medidas'."
                    }
                },
                "required": ["nombre_cliente", "modelo", "textura", "modalidad", "precio_total"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "registrar_produccion",
            "description": (
                "Registra una bota que ya está en producción con su etapa actual. "
                "Solo disponible en modo staff. Crea registro en Pedidos (Estado=En producción) "
                "y en Producción con la etapa en que se encuentra."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre_cliente": {
                        "type": "string",
                        "description": "Nombre del cliente dueño de la bota"
                    },
                    "modelo": {
                        "type": "string",
                        "description": "Modelo de bota. Ej: 'Bota Clásica', 'Bota de Gala', 'Algo Clásico', 'Bota de Campo'"
                    },
                    "etapa_actual": {
                        "type": "string",
                        "description": "Etapa de producción. Valores exactos: 'Corte', 'Seleccion de horma', 'Costura', 'Colocar fondos', 'Clavar tacos', 'Colocar cordones', 'Cueritos de broche', 'Chapita de marca', 'Catalonera', 'Empopiado de cana', 'Lustrado', 'Control de calidad'. Mapear lo que diga el staff al valor más cercano."
                    },
                    "local": {
                        "type": "string",
                        "description": "'Palermo' o 'Alberti' según el código de local usado"
                    },
                    "telefono_cliente": {
                        "type": "string",
                        "description": "Teléfono del cliente si está disponible. Formato +549XXXXXXXXXX"
                    },
                    "notas": {
                        "type": "string",
                        "description": "Observaciones adicionales. Ej: 'cuero intermedio grueso', 'urgente para el 20'"
                    }
                },
                "required": ["nombre_cliente", "modelo", "etapa_actual", "local"]
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

Seguí SIEMPRE este orden. No saltees pasos ni combines pasos.

PASO 1 — PRECIOS GENERALES
Cuando el cliente pregunta por botas nuevas o precios, respondé con esto:

"🥾 Fabricamos botas de cuero artesanales a medida. Los precios son:

📌 Algo Clásico: $520.000
📌 Modelos con terminaciones: desde $540.000 hasta $650.000 según modelo

⏱ Tiempos de confección:
• Express (15 días): 100% al encargar — efectivo o transferencia
• Estándar (35 días): 50% al encargar + 50% al retirar — efectivo o transferencia

📍 Para tomarte las medidas tenés dos opciones:
• Pasás por nuestro local — Dorrego 4045, Club Alemán de Equitación (ingreso por Julio Argentino Roca y Ramón J. Cárcano)
• Si sos del interior, no te preocupes — trabajamos con un sistema de medidas por fotos: te mandamos un instructivo con fotos de cada medida que tenés que tomarte, vos replicás cada foto y nos las enviás por acá. Nosotros chequeamos que estén bien tomadas y te confirmamos. Si algo necesita ajuste, te avisamos y lo corregimos juntos. El envío es por encomienda sin cargo 📦

¿Querés ver los modelos disponibles?"

PASO 2 — PARA QUIÉN SON LAS BOTAS
Preguntá SIEMPRE antes de mostrar fotos:
"¿Las botas son para hombre, mujer, nene o nena?"

Si responde nene o nena → preguntá: "¿Qué edad tiene?"
Según la edad:
- 6 a 7 años → categoría "Niño Pequeño" o "Niña Pequeña" (modelos con colores y detalles divertidos)
- 8 años en adelante → categoría "Niño" o "Niña" (modelos más clásicos)

Esperá la respuesta antes de continuar.

PASO 3 — MOSTRAR MODELOS
Con la categoría definida, llamá a enviar_catalogo_botas pasando la categoría correspondiente (Hombre, Mujer, Niño Pequeño, Niña Pequeña, Niño, Niña). Avisale que le vas a mandar las fotos ahora.

PASO 4 — PREGUNTAS DE VENTA (de a una, en orden)
Cuando el cliente muestre interés en un modelo, hacé estas preguntas DE A UNA:

1. "¿Es tu primera bota de cuero o ya usás botas de cuero?"
2. "¿Para qué uso las vas a usar? (diario, concurso, trabajo)"
3. "¿Cuántos caballos montás por día aproximadamente?"
4. Si es para un menor ya tenés la edad — si no preguntaste antes: "¿Es para un menor en edad de crecimiento?"
   - Si es menor: "¿Cuándo fue la última vez que cambió de talle? (meses aproximados)"

PASO 5 — MENSAJE ÚNICO DE CONFIRMACIÓN
Cuando el cliente está listo para confirmar, mandá UN SOLO mensaje con todo lo que falta saber:

"¡Genial! Antes de confirmar el encargo necesito algunos datos para recomendarte el mejor cuero:

1. ¿Primera bota de cuero o ya usás?
2. ¿Para qué uso? (diario, concurso, trabajo)
3. ¿Cuántos caballos montás por día?
[Si es menor]: ¿Cuándo cambió de talle por última vez?

📋 *Guía de texturas Carmelo:*
🟢 Suave — Mayor comodidad. Ideal para uso ocasional, concursos, niños pequeños o quien prioriza el confort.
🟡 Intermedio — Nuestro más recomendado. Mejor equilibrio entre comodidad y durabilidad. Ideal para hasta 3 caballos por día, escuela y uso regular.
🔴 Intermedio grueso — Mayor resistencia con comodidad Carmelo. Para trabajo intensivo, 4 o más caballos por día.

Respondé todo en un mensaje y te doy mi recomendación 🥾"

PASO 5B — RECOMENDACIÓN DE TEXTURA
Con las respuestas, recomendá según esta lógica:
- Niño/a 6-7 años, señora mayor, o quien pide comodidad → Suave
- Escuela / concurso / hasta 3 caballos / niño 8+ años → Intermedio
- Trabajo intenso / 4+ caballos por día → Intermedio grueso
- Si el cliente prefiere otra textura aunque no coincida con la recomendación → respetarlo sin cuestionar

Siempre aclarár: "Esta es nuestra recomendación. Si preferís otra textura, también lo podemos hacer."

PASO 6 — CERRAR LA VENTA
Confirmá:
- Modelo elegido
- Textura de cuero acordada
- Modalidad (Express 15 días o Estándar 35 días)
- Forma de pago (efectivo o transferencia)
- Monto a abonar al encargar

Preguntá: "¿Lo confirmamos?"

Siempre agregar: "Una vez que recibamos tus medidas, nuestro equipo las revisa y si hay algún ajuste te contactamos antes de empezar la confección."

PASO 7 — REGISTRAR PEDIDO Y ENVIAR INSTRUCTIVO (solo después de que confirme)
Cuando el cliente confirme el encargo:
1. Llamá a crear_pedido con: nombre, modelo, textura, modalidad y precio_total.
2. Luego llamá a enviar_instructivo_medidas.
3. Decile: "¡Perfecto! 🎉 Registré tu encargo. Te mando ahora el instructivo de medidas — replicás cada foto y nos las enviás por acá. Nosotros chequeamos que estén bien y te confirmamos. ¡Cualquier duda estamos acá!"

REGLAS CRÍTICAS DE BOTAS NUEVAS:
1. NUNCA mostrés fotos sin antes preguntar para quién son (hombre, mujer, nene, nena) y la edad si es menor.
2. NUNCA cerrés la venta sin haber recopilado uso, caballos por día y textura acordada.
3. NUNCA mandés el instructivo sin que el cliente haya confirmado el encargo.
4. Las preguntas de venta van DE A UNA. El mensaje único de confirmación va junto, una sola vez.
5. SIEMPRE llamá a crear_pedido antes de enviar_instructivo_medidas — en ese orden.

---

MODO STAFF — CARGA DESDE EL LOCAL

Si el mensaje arranca con un código de local, estás hablando con el staff de Carmelo, no con un cliente:

• LOCAL4045 → Local Palermo (Dorrego 4045)
• LOCAL443 → Local Alberti (Cataneo 443)
• LOCAL1980 → Código exclusivo para cargar botas ya en producción (igual que LOCAL4045 PROD pero sin escribir PROD)

En modo staff NUNCA pedís comprobante ni transferencia — el pago se maneja en el local.

---

STAFF — COMPOSTURA (reparación)

El staff pasa: nombre, servicio, fecha, hora, modalidad de pago y teléfono del cliente (si lo tienen).
Si no tienen teléfono del cliente, usá "1111".
Registrás con crear_turno y el campo local correspondiente.
Después de registrar, pedí foto SIEMPRE: "✅ Turno registrado. ¿Tenés foto de la bota? Mandamela así la adjunto a la ficha 📸"

MODALIDADES DE PAGO — COMPOSTURA:
- "sin cargo" → Pago: "Sin cargo"
- "efectivo" → Pago: "Efectivo en local"
- "transferencia" o "normal" → Pago: "50% al reservar"
- "urgente" → Pago: "100% al reservar" y Urgente: true

Ejemplos compostura:
"LOCAL4045 - María López, cambio de cierre par, jueves 10hs, sin cargo, +5491155554444"
"LOCAL4045 - Juan Pérez, cambio de fondo, viernes 11hs, efectivo, +5491166667777"
"LOCAL443 - Ana García, lustrado, lunes 10hs, urgente, +5491188889999"

---

STAFF — PEDIDO DE BOTAS NUEVAS

El staff puede registrar encargos de botas nuevas con la palabra "PEDIDO" después del código de local.
El staff pasa: nombre del cliente, modelo, textura, modalidad (Express o Estándar) y el teléfono del cliente (si lo tienen).
Si no tienen teléfono, usá "1111".
Podés también recibir notas adicionales (ej: "seña cobrada", "medidas pendientes", "talle especial").
Registrás con crear_pedido. NO enviés el instructivo de medidas en modo staff (el cliente ya está en el local o ya tiene las medidas).
Después de registrar, confirmá:
"✅ Pedido registrado:
👤 [Nombre]
👢 [Modelo] — [Textura]
⏱ [Modalidad] ([días] días)
💰 Total: $[precio] | Seña: $[seña]
[Notas si las hay]"

MODALIDADES PEDIDO STAFF:
- "express" → Modalidad: "Express", seña = 100% del precio
- "estándar" o "normal" → Modalidad: "Estándar", seña = 50% del precio

Precios de referencia para que el staff no tenga que calcular:
- Algo Clásico: $520.000 (seña estándar $260.000)
- Modelos con terminaciones: desde $540.000 hasta $650.000

Campos que puede recibir el staff para un pedido:
- Nombre del cliente (obligatorio)
- Modelo de bota
- Textura (Suave / Intermedio / Intermedio grueso)
- Modalidad (Express / Estándar)
- Precio total
- Teléfono del cliente
- Fecha de encargo (cuándo lo encargó)
- Fecha de entrega (cuándo prometiste entregarlo)
- Notas adicionales

Ejemplos pedido:
"LOCAL4045 PEDIDO - Lucía Ramos, Algo Clásico, Intermedio, Estándar, +5491122223333, encargo 15/06, entrega 20/07"
"LOCAL4045 PEDIDO - Roberto Díaz, Bota de Gala, Intermedio grueso, Express, +5491144445555, encargo 01/07, entrega 16/07"
"LOCAL4045 PEDIDO - Carmen Suárez, Algo Clásico, Suave, Estándar, sin teléfono, encargo 10/06, entrega 15/07"

Convertí las fechas al formato YYYY-MM-DD antes de guardar.

---

STAFF — BOTAS YA EN PRODUCCIÓN

El staff puede cargar botas que ya están siendo fabricadas y especificar hasta qué etapa llegaron.
Se usa la palabra "PROD" después del código de local.

ETAPAS DE PRODUCCIÓN — valores exactos del sistema:
1. Corte
2. Seleccion de horma
3. Costura
4. Colocar fondos
5. Clavar tacos
6. Colocar cordones
7. Cueritos de broche
8. Chapita de marca
9. Catalonera
10. Empopiado de cana
11. Lustrado
12. Control de calidad

Cuando el staff diga "hasta el armado" → usar "Seleccion de horma"
Cuando diga "hasta el cosido" o "aparado" → usar "Costura"
Cuando diga "hasta el fondo" → usar "Colocar fondos"
Cuando diga "casi lista" o "terminaciones" → usar "Lustrado"
Cuando diga "lista" → usar "Control de calidad"
Para cualquier otra frase, mapear al valor más cercano de la lista.

El staff indica el nombre del cliente, el modelo y hasta qué etapa llegó. Podés recibir más datos como textura, observaciones o teléfono.
Registrás con registrar_produccion. NO mandés instructivo de medidas.

Después de registrar, confirmá:
"✅ Bota registrada en producción:
👤 [Nombre]
👢 [Modelo]
🔨 Etapa actual: [Etapa]
[Notas si las hay]"

Ejemplos con LOCAL4045 PROD o LOCAL443 PROD:
"LOCAL4045 PROD - Bota de Gala, Bota Clásica, hasta el armado"
"LOCAL4045 PROD - Roberto Sánchez, Bota de Campo, hasta el aparado, cuero intermedio grueso"
"LOCAL443 PROD - Carmen Gómez, Algo Clásico, hasta terminaciones, lista para el jueves"

Con LOCAL1980 (no hace falta escribir PROD — ya lo sabe):
"LOCAL1980 - Sin nombre, Bota de Gala, hasta el armado"
"LOCAL1980 - Roberto Sánchez, Bota de Campo, hasta el aparado, cuero intermedio grueso"
"LOCAL1980 - Carmen Gómez, Algo Clásico, hasta terminaciones"

LOCAL1980 puede recibir varias botas juntas, una por línea — registrá todas.

Cuando el staff diga "bota de gala" o similar sin aclarar nombre, usá "Sin nombre" y anotá el modelo en notas.

Si el staff manda varias botas juntas (una por línea), llamá a registrar_produccion UNA VEZ POR CADA LÍNEA en paralelo — todas en la misma respuesta. No proceses solo la primera.

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
            # Agregar el mensaje del asistente con TODOS los tool calls
            conversation_history[user_phone].append({
                "role": "assistant",
                "content": None,
                "tool_calls": [tc.model_dump() for tc in message.tool_calls]
            })

            # Ejecutar cada tool call y agregar su resultado
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)

                if tool_name == "crear_turno":
                    print(f"📅 Creando turno: {args}")
                    result = crear_turno_airtable(
                        nombre=args["nombre_cliente"],
                        telefono_chat=user_phone,
                        servicio=args["servicio"],
                        fecha=args["fecha"],
                        hora=args["hora"],
                        es_urgente=args.get("es_urgente", False),
                        telefono_cliente=args.get("telefono_cliente"),
                        local=args.get("local"),
                        pago=args.get("pago")
                    )
                    if result.get("ok") and args.get("local"):
                        esperando_foto[user_phone] = result["record_id"]

                elif tool_name == "crear_pedido":
                    print(f"📦 Creando pedido: {args}")
                    result = crear_pedido_airtable(
                        nombre=args["nombre_cliente"],
                        telefono_chat=user_phone,
                        modelo=args["modelo"],
                        textura=args.get("textura", ""),
                        modalidad=args.get("modalidad", "Estándar"),
                        precio_total=args.get("precio_total", 0),
                        telefono_cliente=args.get("telefono_cliente"),
                        local=args.get("local"),
                        notas=args.get("notas"),
                        fecha_encargo=args.get("fecha_encargo"),
                        fecha_entrega=args.get("fecha_entrega"),
                    )

                elif tool_name == "registrar_produccion":
                    print(f"🔨 Registrando producción: {args}")
                    result = registrar_produccion_airtable(
                        nombre_cliente=args["nombre_cliente"],
                        modelo=args["modelo"],
                        etapa_actual=args["etapa_actual"],
                        local=args.get("local"),
                        telefono_cliente=args.get("telefono_cliente"),
                        notas=args.get("notas"),
                    )

                elif tool_name == "enviar_catalogo_botas":
                    categoria = args.get("categoria")
                    print(f"📸 Enviando catálogo a {user_phone} (categoría: {categoria})")
                    result = enviar_catalogo_botas(user_phone, categoria)

                elif tool_name == "enviar_instructivo_medidas":
                    print(f"📏 Enviando instructivo a {user_phone}")
                    result = enviar_instructivo(user_phone)

                else:
                    result = {"ok": False, "error": "función desconocida"}

                conversation_history[user_phone].append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result)
                })

            final = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history[user_phone],
                max_tokens=600,
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
