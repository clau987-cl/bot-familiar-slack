import os
import logging
import json
import tempfile
import subprocess
import requests
from datetime import datetime

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN  = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
CANAL_ID         = os.environ.get("SLACK_CHANNEL_ID", "")
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_SERVICE_KEY"]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app      = App(token=SLACK_BOT_TOKEN)
claude   = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─────────────────────────────────────────────
# BASE DE DATOS — Supabase
# ─────────────────────────────────────────────
def guardar(tipo, descripcion, fecha, usuario):
    supabase.table("items").insert({
        "tipo": tipo, "descripcion": descripcion,
        "fecha": fecha, "creado_por": usuario,
        "responsable": usuario, "completado": False,
    }).execute()

def obtener_activos():
    resp = (supabase.table("items")
        .select("id, tipo, descripcion, fecha, creado_por, responsable")
        .eq("completado", False).order("tipo").execute())
    return resp.data or []

def marcar_listo(item_id):
    supabase.table("items").update({"completado": True}).eq("id", item_id).execute()

# ─────────────────────────────────────────────
# CLAUDE — EXTRACCIÓN Y RESUMEN
# ─────────────────────────────────────────────
EMOJIS = {"PENDIENTE": "📌", "EVENTO": "📅", "COMPRA": "🛒", "AGENDA": "🚗"}

def analizar_con_claude(texto):
    prompt = f"""Eres el asistente de coordinación de una familia. Analiza el siguiente mensaje y determina si contiene:
- PENDIENTE : tarea o cosa por hacer
- EVENTO    : compromiso con fecha
- COMPRA    : algo que hay que comprar
- AGENDA    : coordinación de horarios

Si detectas algo relevante, responde ÚNICAMENTE con este JSON:
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
    resp = claude.messages.create(model="claude-haiku-4-5-20251001", max_tokens=300,
        messages=[{"role": "user", "content": prompt}])
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    return json.loads(raw)

def generar_resumen(items):
    if not items:
        return "✅ No hay pendientes activos. ¡Todo al día!"
    lista = "\n".join(
        f"[{r['tipo']}] #{r['id']} {r['descripcion']}"
        + (f"  ⏰ {r['fecha']}" if r.get("fecha") else "")
        + f"  (por {r.get('creado_por','?')})" for r in items)
    resp = claude.messages.create(model="claude-haiku-4-5-20251001", max_tokens=700,
        messages=[{"role": "user", "content": f"Genera un resumen claro y organizado de los pendientes familiares, agrupado por categorías con emojis. Usa formato Slack. Sé conciso.\n\nPendientes:\n{lista}"}])
    return resp.content[0].text

# ─────────────────────────────────────────────
# TRANSCRIPCIÓN DE AUDIO
# ─────────────────────────────────────────────
def descargar_archivo_slack(url, destino):
    r = requests.get(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, stream=True)
    r.raise_for_status()
    with open(destino, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def transcribir_audio(audio_path):
    try:
        import speech_recognition as sr
        wav = audio_path + ".wav"
        subprocess.run(["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav],
            capture_output=True, check=True)
        r = sr.Recognizer()
        with sr.AudioFile(wav) as src:
            audio = r.record(src)
        texto = r.recognize_google(audio, language="es-MX")
        os.remove(wav)
        return texto
    except Exception as e:
        logger.warning(f"No se pudo transcribir: {e}")
        return None

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def nombre_usuario(user_id):
    try:
        info = app.client.users_info(user=user_id)
        return info["user"]["profile"].get("first_name") or info["user"]["real_name"] or "Alguien"
    except Exception:
        return "Alguien"

def procesar_texto(texto, usuario, say):
    try:
        resultado = analizar_con_claude(texto)
        if resultado["tipo"] != "NINGUNO":
            guardar(resultado["tipo"], resultado["descripcion"], resultado.get("fecha"), usuario)
            say(f"{EMOJIS.get(resultado['tipo'], '✅')} {resultado['confirmacion']}")
    except Exception as e:
        logger.error(f"Error: {e}")

# ─────────────────────────────────────────────
# EVENTOS DE SLACK
# ─────────────────────────────────────────────
@app.message()
def handle_mensaje(message, say):
    if message.get("bot_id"):
        return
    user_id = message.get("user", "")
    subtype = message.get("subtype", "")
    if subtype == "file_share":
        for archivo in message.get("files", []):
            if "audio" in archivo.get("mimetype", "") or "video" in archivo.get("mimetype", ""):
                _procesar_audio_slack(archivo, user_id, say)
                return
        return
    if subtype:
        return
    for archivo in message.get("files", []):
        if "audio" in archivo.get("mimetype", "") or "video" in archivo.get("mimetype", ""):
            _procesar_audio_slack(archivo, user_id, say)
            return
    texto = message.get("text", "").strip()
    if not texto or texto.startswith("/"):
        return
    procesar_texto(texto, nombre_usuario(user_id), say)

def _procesar_audio_slack(archivo, user_id, say):
    url = archivo.get("url_private_download") or archivo.get("url_private")
    if not url:
        say("🎤 No pude acceder al audio. ¿Puedes escribirlo?")
        return
    ext = archivo.get("filetype", "mp4")
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        descargar_archivo_slack(url, tmp_path)
        texto = transcribir_audio(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    if texto:
        say(f"🎤 _Escuché: {texto}_")
        procesar_texto(texto, nombre_usuario(user_id), say)
    else:
        say("🎤 No pude transcribir el audio. ¿Puedes escribirlo?")

# ─────────────────────────────────────────────
# SLASH COMMANDS
# ─────────────────────────────────────────────
@app.command("/resumen")
def cmd_resumen(ack, say):
    ack()
    items = obtener_activos()
    resumen = generar_resumen(items)
    pie = "\n\n_Usa /listo [número] para marcar como completado_" if items else ""
    say(f"📋 *PENDIENTES FAMILIARES*\n\n{resumen}{pie}")

@app.command("/listo")
def cmd_listo(ack, say, command):
    ack()
    try:
        marcar_listo(int(command["text"].strip()))
        say(f"✅ ¡Listo! Item #{command['text'].strip()} completado.")
    except (ValueError, KeyError):
        say("Uso: `/listo [número]`  Ejemplo: `/listo 3`")

@app.command("/ayuda")
def cmd_ayuda(ack, say):
    ack()
    say("📖 *Comandos:*\n`/resumen` — ver pendientes\n`/listo 3` — marcar #3 como listo\n`/ayuda` — esta ayuda\n\n💡 Escribe en lenguaje natural o manda audios 🎤")

# ─────────────────────────────────────────────
# RESUMEN AUTOMÁTICO SEMANAL
# ─────────────────────────────────────────────
def enviar_resumen_automatico():
    if not CANAL_ID:
        return
    items = obtener_activos()
    app.client.chat_postMessage(channel=CANAL_ID,
        text=f"📋 *RESUMEN SEMANAL FAMILIAR* 🗓\n\n{generar_resumen(items)}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    if CANAL_ID:
        scheduler = BackgroundScheduler()
        scheduler.add_job(enviar_resumen_automatico, trigger="cron", day_of_week="sun", hour=20)
        scheduler.start()
    logger.info("Bot de Slack iniciado ✅")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()

if __name__ == "__main__":
    main()
