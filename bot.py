import os
import logging
import json
import base64
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
SUPABASE_KEY     = os.environ["SUPABASE_SERVICE_KEY"]   # service_role key

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app      = App(token=SLACK_BOT_TOKEN)
claude   = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─────────────────────────────────────────────
# BASE DE DATOS — Supabase
# ─────────────────────────────────────────────
def guardar(tipo: str, descripcion: str, fecha: str | None, usuario: str, imagen_url: str | None = None):
    data = {
        "tipo":        tipo,
        "descripcion": descripcion,
        "fecha":       fecha,
        "creado_por":  usuario,
        "responsable": usuario,
        "completado":  False,
    }
    if imagen_url:
        data["imagen_url"] = imagen_url
    supabase.table("items").insert(data).execute()

def obtener_activos() -> list:
    resp = (
        supabase.table("items")
        .select("id, tipo, descripcion, fecha, creado_por, responsable, imagen_url")
        .eq("completado", False)
        .order("tipo")
        .execute()
    )
    return resp.data or []

def marcar_listo(item_id: int):
    supabase.table("items").update({"completado": True}).eq("id", item_id).execute()

# ─────────────────────────────────────────────
# CLAUDE — EXTRACCIÓN Y RESUMEN
# ─────────────────────────────────────────────
EMOJIS = {"PENDIENTE": "📌", "EVENTO": "📅", "COMPRA": "🛒", "AGENDA": "🚗"}

SYSTEM_PROMPT_ANALISIS = """Eres el asistente de coordinación de una familia. Analiza el contenido y determina si contiene:
- PENDIENTE : tarea o cosa por hacer   (ej: arreglar el techo, llamar al plomero)
- EVENTO    : compromiso con fecha     (ej: junta con amigos el viernes 4, cumpleaños el 15)
- COMPRA    : algo que hay que comprar (ej: leche, detergente, medicamentos)
- AGENDA    : coordinación de horarios (ej: recoger a los niños a las 5)

Si detectas algo relevante, responde ÚNICAMENTE con este JSON (sin texto extra):
{
  "tipo": "PENDIENTE|EVENTO|COMPRA|AGENDA",
  "descripcion": "descripción breve",
  "fecha": "fecha o plazo mencionado, null si no hay",
  "confirmacion": "mensaje de confirmación amigable en ≤15 palabras"
}

Si no hay nada relevante para la familia, responde ÚNICAMENTE con:
{"tipo": "NINGUNO"}"""


def analizar_con_claude(texto: str) -> dict:
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"{SYSTEM_PROMPT_ANALISIS}\n\nMensaje: \"{texto}\""}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    return json.loads(raw)


def analizar_imagen_con_claude(imagen_b64: str, media_type: str, texto_acompanante: str = "") -> dict:
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": imagen_b64,
            },
        },
        {
            "type": "text",
            "text": SYSTEM_PROMPT_ANALISIS + (
                f"\n\nEl usuario también escribió: \"{texto_acompanante}\"" if texto_acompanante else
                "\n\nAnaliza la imagen y determina qué tipo de item familiar es."
            ),
        },
    ]
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    return json.loads(raw)


def generar_resumen(items: list) -> str:
    if not items:
        return "✅ No hay pendientes activos. ¡Todo al día!"
    lista = "\n".join(
        f"[{r['tipo']}] #{r['id']} {r['descripcion']}"
        + (f"  ⏰ {r['fecha']}" if r.get("fecha") else "")
        + f"  (por {r.get('creado_por','?')})"
        + ("  🖼" if r.get("imagen_url") else "")
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
# DESCARGA DE ARCHIVOS SLACK
# ─────────────────────────────────────────────
def descargar_archivo_slack(url: str, destino: str):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    r = requests.get(url, headers=headers, stream=True)
    r.raise_for_status()
    with open(destino, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def descargar_archivo_slack_bytes(url: str) -> bytes:
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.content

# ─────────────────────────────────────────────
# SUBIDA A SUPABASE STORAGE
# ─────────────────────────────────────────────
def subir_imagen_supabase(nombre: str, datos: bytes, content_type: str) -> str:
    supabase.storage.from_("imagenes").upload(
        path=nombre,
        file=datos,
        file_options={"content-type": content_type},
    )
    url = supabase.storage.from_("imagenes").get_public_url(nombre)
    return url

# ─────────────────────────────────────────────
# TRANSCRIPCIÓN DE AUDIO
# ─────────────────────────────────────────────
def transcribir_audio(audio_path: str) -> str | None:
    try:
        import speech_recognition as sr

        wav = audio_path + ".wav"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(f"ffmpeg falló: {result.stderr.decode()}")
            return None

        r = sr.Recognizer()
        with sr.AudioFile(wav) as src:
            audio = r.record(src)
        texto = r.recognize_google(audio, language="es-MX")

        try:
            os.remove(wav)
        except OSError:
            pass

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
# PROCESAMIENTO DE IMÁGENES
# ─────────────────────────────────────────────
def _procesar_imagen_slack(archivo: dict, user_id: str, say, texto_acompanante: str = ""):
    url_privada = archivo.get("url_private_download") or archivo.get("url_private")
    if not url_privada:
        say("🖼 No pude acceder a la imagen.")
        return

    usuario  = nombre_usuario(user_id)
    ext      = archivo.get("filetype", "jpg")
    mimetype = archivo.get("mimetype", "image/jpeg")
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre   = f"{ts}_{user_id}.{ext}"

    try:
        imagen_bytes = descargar_archivo_slack_bytes(url_privada)
        imagen_url = subir_imagen_supabase(nombre, imagen_bytes, mimetype)
        logger.info(f"Imagen subida a Supabase: {imagen_url}")

        imagen_b64 = base64.b64encode(imagen_bytes).decode()
        resultado = analizar_imagen_con_claude(imagen_b64, mimetype, texto_acompanante)

        if resultado["tipo"] != "NINGUNO":
            guardar(resultado["tipo"], resultado["descripcion"], resultado.get("fecha"), usuario, imagen_url)
            emoji = EMOJIS.get(resultado["tipo"], "✅")
            say(f"🖼 {emoji} {resultado['confirmacion']}")
        else:
            guardar("PENDIENTE", f"Imagen de {usuario}", None, usuario, imagen_url)
            say(f"🖼 Imagen guardada en el panel.")

    except Exception as e:
        logger.error(f"Error procesando imagen: {e}")
        say("🖼 Hubo un error procesando la imagen. Intenta de nuevo.")

# ─────────────────────────────────────────────
# PROCESAMIENTO DE AUDIO
# ─────────────────────────────────────────────
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
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if texto:
        say(f"🎤 _Escuché: {texto}_")
        procesar_texto(texto, usuario, say)
    else:
        say("🎤 No pude transcribir el audio. ¿Puedes escribirlo?")

# ─────────────────────────────────────────────
# EVENTOS DE SLACK
# ─────────────────────────────────────────────
@app.message()
def handle_mensaje(message, say):
    if message.get("bot_id"):
        return

    user_id  = message.get("user", "")
    subtype  = message.get("subtype", "")
    texto    = message.get("text", "").strip()
    archivos = message.get("files", [])

    if subtype == "file_share" or archivos:
        for archivo in archivos:
            mimetype = archivo.get("mimetype", "")

            if mimetype.startswith("image/"):
                _procesar_imagen_slack(archivo, user_id, say, texto)
                return

            if "audio" in mimetype or "video" in mimetype:
                _procesar_audio_slack(archivo, user_id, say)
                return

        if not texto:
            return

    if subtype and subtype != "file_share":
        return

    if not texto or texto.startswith("/"):
        return

    usuario = nombre_usuario(user_id)
    procesar_texto(texto, usuario, say)

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
        say("Uso: /listo [número]\nEjemplo: /listo 3")


@app.command("/ayuda")
def cmd_ayuda(ack, say):
    ack()
    say(
        "📖 *Comandos disponibles:*\n\n"
        "/resumen — resumen completo de pendientes activos\n"
        "/listo 3 — marca el item #3 como completado\n"
        "/ayuda — esta ayuda\n\n"
        "💡 *Escribe mensajes normales como:*\n"
        "• _hay que arreglar el techo antes de marzo_\n"
        "• _junta con los García el viernes 4_\n"
        "• _comprar leche y detergente_\n"
        "• _recoger a los niños el martes a las 5_\n\n"
        "También puedes mandar fotos 🖼 o notas de voz 🎤"
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
