import os
import logging
import json
import sqlite3
import tempfile
import subprocess
import requests
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]    # xoxb-...
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]    # xapp-...
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]  # sk-ant-...
# ID del canal donde vive el bot (ej: C0123456789)
CANAL_ID        = os.environ.get("SLACK_CHANNEL_ID", "")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app    = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─────────────────────────────────────────────
# BASE DE DATOS
# ─────────────────────────────────────────────
DB = "familia.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo         TEXT    NOT NULL,
            descripcion  TEXT    NOT NULL,
            fecha        TEXT,
            completado   INTEGER DEFAULT 0,
            creado_por   TEXT,
            creado_en    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()

def guardar(tipo, descripcion, fecha, usuario):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO items (tipo, descripcion, fecha, creado_por) VALUES (?,?,?,?)",
        (tipo, descripcion, fecha, usuario),
    )
    con.commit()
    con.close()

def obtener_activos():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, tipo, descripcion, fecha, creado_por "
        "FROM items WHERE completado=0 ORDER BY tipo, fecha NULLS LAST"
    ).fetchall()
    con.close()
    return rows

def marcar_listo(item_id: int):
    con = sqlite3.connect(DB)
    con.execute("UPDATE items SET completado=1 WHERE id=?", (item_id,))
    con.commit()
    con.close()

# ─────────────────────────────────────────────
# CLAUDE – EXTRACCIÓN Y RESUMEN
# ─────────────────────────────────────────────
EMOJIS = {"PENDIENTE": "📌", "EVENTO": "📅", "COMPRA": "🛒", "AGENDA": "🚗"}

def analizar_con_claude(texto: str) -> dict:
    prompt = f"""Eres el asistente de coordinación de una familia. Analiza el siguiente mensaje y determina si contiene:
- PENDIENTE : tarea o cosa por hacer   (ej: arreglar el techo, llamar al plomero)
- EVENTO    : compromiso con fecha     (ej: junta con amigos el viernes 4, cumpleaños el 15)
- COMPRA    : algo que hay que comprar (ej: leche, detergente, medicamentos)
- AGENDA    : coordinación de horarios (ej: recoger a los niños a las 5)

Si detectas algo relevante, responde ÚNICAMENTE con este JSON (sin texto extra):
{{
  "tipo": "PENDIENTE|EVENTO|COMPRA|AGENDA",
  "descripcion": "descripción breve",
  "fecha": "fecha o plazo mencionado, null si no hay",
  "confirmacion": "mensaje de confirmación amigable en ≤15 palabras"
}}

Si el mensaje es solo plática o saludos, responde ÚNICAMENTE con:
{{"tipo": "NINGUNO"}}

Mensaje: "{texto}"
"""
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    return json.loads(raw)

def generar_resumen(items: list) -> str:
    if not items:
        return "✅ No hay pendientes activos. ¡Todo al día!"

    lista = "\n".join(
        f"[{r[1]}] #{r[0]} {r[2]}" + (f"  ⏰ {r[3]}" if r[3] else "") + f"  (por {r[4]})"
        for r in items
    )
    prompt = f"""Genera un resumen claro y organizado de los pendientes familiares, agrupado por categorías con emojis.
Usa formato de texto plano adecuado para Slack. Sé conciso. Termina con una frase motivadora corta.

Pendientes:
{lista}"""
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

# ─────────────────────────────────────────────
# TRANSCRIPCIÓN DE AUDIO
# ─────────────────────────────────────────────
def descargar_archivo_slack(url: str, destino: str):
    """Descarga un archivo de Slack usando el token del bot."""
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    r = requests.get(url, headers=headers, stream=True)
    r.raise_for_status()
    with open(destino, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def transcribir_audio(audio_path: str) -> str | None:
    try:
        import speech_recognition as sr

        wav = audio_path + ".wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav],
            capture_output=True, check=True,
        )
        r = sr.Recognizer()
        with sr.AudioFile(wav) as src:
            audio = r.record(src)
        texto = r.recognize_google(audio, language="es-MX")
        os.remove(wav)
        return texto
    except Exception as e:
        logger.warning(f"No se pudo transcribir el audio: {e}")
        return None

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def nombre_usuario(user_id: str) -> str:
    try:
        info = app.client.users_info(user=user_id)
        return info["user"]["profile"].get("first_name") or info["user"]["real_name"] or "Alguien"
    except Exception:
        return "Alguien"

def procesar_texto(texto: str, usuario: str, say):
    try:
        resultado = analizar_con_claude(texto)
        if resultado["tipo"] != "NINGUNO":
            guardar(resultado["tipo"], resultado["descripcion"], resultado.get("fecha"), usuario)
            emoji = EMOJIS.get(resultado["tipo"], "✅")
            say(f"{emoji} {resultado['confirmacion']}")
    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")

# ─────────────────────────────────────────────
# EVENTOS DE SLACK
# ─────────────────────────────────────────────
@app.message()
def handle_mensaje(message, say):
    # Ignorar mensajes del propio bot y mensajes sin texto
    if message.get("bot_id"):
        return
    if message.get("subtype"):
        return

    texto   = message.get("text", "").strip()
    user_id = message.get("user", "")

    # Manejo de archivos de audio adjuntos
    archivos = message.get("files", [])
    for archivo in archivos:
        mimetype = archivo.get("mimetype", "")
        if "audio" in mimetype or "video" in mimetype:
            _procesar_audio_slack(archivo, user_id, say)
            return

    if not texto:
        return

    # Ignorar comandos (los maneja el handler de slash commands)
    if texto.startswith("/"):
        return

    usuario = nombre_usuario(user_id)
    procesar_texto(texto, usuario, say)


def _procesar_audio_slack(archivo: dict, user_id: str, say):
    url_privada = archivo.get("url_private_download") or archivo.get("url_private")
    if not url_privada:
        say("🎤 No pude acceder al audio. ¿Puedes escribirlo?")
        return

    usuario = nombre_usuario(user_id)
    ext     = archivo.get("filetype", "mp4")

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        descargar_archivo_slack(url_privada, tmp_path)
        texto = transcribir_audio(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if texto:
        say(f"🎤 _Escuché: {texto}_")
        procesar_texto(texto, usuario, say)
    else:
        say("🎤 No pude transcribir el audio. ¿Puedes escribirlo?")


# ─────────────────────────────────────────────
# SLASH COMMANDS
# ─────────────────────────────────────────────
@app.command("/resumen")
def cmd_resumen(ack, say):
    ack()
    items   = obtener_activos()
    resumen = generar_resumen(items)
    pie     = "\n\n_Usa /listo [número] para marcar como completado_" if items else ""
    say(f"📋 *PENDIENTES FAMILIARES*\n\n{resumen}{pie}")


@app.command("/listo")
def cmd_listo(ack, say, command):
    ack()
    try:
        item_id = int(command["text"].strip())
        marcar_listo(item_id)
        say(f"✅ ¡Listo! El item #{item_id} queda completado.")
    except (ValueError, KeyError):
        say("Uso: `/listo [número]`\nEjemplo: `/listo 3`")


@app.command("/ayuda")
def cmd_ayuda(ack, say):
    ack()
    say(
        "📖 *Comandos disponibles:*\n\n"
        "`/resumen` — resumen completo de pendientes activos\n"
        "`/listo 3` — marca el item #3 como completado\n"
        "`/ayuda` — esta ayuda\n\n"
        "💡 *Escribe mensajes normales como:*\n"
        "• _hay que arreglar el techo antes de marzo_\n"
        "• _junta con los García el viernes 4_\n"
        "• _comprar leche y detergente_\n"
        "• _recoger a los niños el martes a las 5_\n\n"
        "También puedes mandar notas de voz o audios 🎤"
    )


# ─────────────────────────────────────────────
# RESUMEN AUTOMÁTICO SEMANAL (domingos 8 PM)
# ─────────────────────────────────────────────
def enviar_resumen_automatico():
    if not CANAL_ID:
        return
    items   = obtener_activos()
    resumen = generar_resumen(items)
    app.client.chat_postMessage(
        channel=CANAL_ID,
        text=f"📋 *RESUMEN SEMANAL FAMILIAR* 🗓\n\n{resumen}",
    )
    logger.info("Resumen semanal enviado.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    init_db()

    # Programar resumen automático domingos 20:00
    if CANAL_ID:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            enviar_resumen_automatico,
            trigger="cron",
            day_of_week="sun",
            hour=20,
            minute=0,
        )
        scheduler.start()
        logger.info("Resumen automático programado: domingos 20:00")

    logger.info("Bot de Slack iniciado ✅")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
