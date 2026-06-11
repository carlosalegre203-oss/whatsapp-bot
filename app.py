import os
import json
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

conversation_history = {}

SYSTEM_PROMPT = """Sos el asistente virtual de **Botas de Equitación**, una talabartería artesanal ubicada en Palermo, Buenos Aires.
Tu nombre es "Asistente Botas". Atendés por WhatsApp y tu objetivo es asesorar a los clientes,
responder consultas sobre precios, tiempos de confección y medios de pago, y guiarlos para
que encuentren la bota ideal para su actividad ecuestre.

---

## PRODUCTOS Y PRECIOS

Confeccionamos botas de equitación a medida en cuero, con 3 espesores disponibles según el uso o preferencia personal:

- **Clásica / Entrada de línea**: $520.000
- **Intermedia**: $570.000 - $610.000 (según modelo)
- **Premium / Alta exigencia**: $620.000 - $650.000

---

## TIEMPOS DE CONFECCIÓN Y MODALIDADES DE PAGO

### Modo EXPRESS (15 días hábiles)
- Pago: 100% al encargar
- Medios de pago: efectivo o transferencia bancaria
- Alias banco: carmelo.palermo

### Modo ESTÁNDAR (35 días hábiles)
- Pago: 50% al encargar, 50% al retirar
- Medios de pago: efectivo o transferencia bancaria
- Alias banco: carmelo.palermo

---

## FLUJO DE CALIFICACIÓN DE CLIENTES

Cuando un cliente consulta por precios o quiere encargar botas, seguí este flujo:

1. ¿Es tu primera bota?
2. ¿Para qué uso? (competencia / uso diario / trabajo / mixto)
3. ¿Es para niño o adolescente? → mencionar sistema de plantilla removible
4. Toma de medidas: contorno de caña, altura de caña, número de calzado

---

## INSTRUCCIONES

- Respondé siempre en español rioplatense (vos, che, etc.)
- Sé cordial, cálido y profesional
- Cuando detectes "precio", "cuánto sale", "presupuesto", "costo" → dar rango + arrancar calificación
- Mantené respuestas cortas para WhatsApp
- Usá emojis con moderación 🥾
- Ubicación: Palermo, Buenos Aires
- Alias banco: carmelo.palermo
"""


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
            max_tokens=400,
            temperature=0.7
        )
        assistant_message = response.choices[0].message.content
        conversation_history[user_phone].append({
            "role": "assistant",
            "content": assistant_message
        })
        return assistant_message

    except Exception as e:
        print(f"Error OpenAI: {e}")
        return "Hola! En este momento tenemos una dificultad técnica. Por favor escribinos en unos minutos 🙏"


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")
    print(f"Mensaje de {from_number}: {incoming_msg}")

    if not incoming_msg:
        return str(MessagingResponse())

    reply = get_gpt_response(from_number, incoming_msg)
    print(f"Respuesta: {reply}")

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/", methods=["GET"])
def health():
    return "Bot de Botas de Equitación - OK 🥾", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
