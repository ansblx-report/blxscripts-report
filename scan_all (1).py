import discord
from discord.ext import commands
import asyncio
import sqlite3
import logging
import os
import re
import aiohttp
import tempfile
from datetime import datetime, timezone
from google import genai
from google.genai import types
from pydantic import BaseModel

# ============================================================
#                     CONFIGURACIÓN DE ENTORNOS
# ============================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "evidencia")
SUPABASE_STORAGE_SIGNED_URL_TTL = int(os.getenv("SUPABASE_STORAGE_SIGNED_URL_TTL", "2592000"))
EVIDENCE_DIR = "resultados/evidencia"

# Clave pública reportada como filtrada para validación preventiva
LEAKED_DEFAULT_KEY = "AIzaSyAcd0v4QHUIjp_SwHrXatFhzyTLAkpH5ss"

os.makedirs("resultados", exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)

log = logging.getLogger("RobloxReportScanner")

def _storage_key() -> str:
    return SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY

def _storage_enabled() -> bool:
    return bool(SUPABASE_URL and _storage_key() and SUPABASE_STORAGE_BUCKET)

def _sanitize_storage_name(filename: str) -> str:
    name = os.path.basename(filename or "evidencia.bin")
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)

def _storage_public_url(object_path: str) -> str:
    return f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/{object_path}"

async def create_signed_storage_url(object_path: str, session: aiohttp.ClientSession = None) -> str | None:
    if not _storage_enabled():
        return None

    url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/sign/{SUPABASE_STORAGE_BUCKET}/{object_path}"
    headers = {
        "apikey": _storage_key(),
        "Authorization": f"Bearer {_storage_key()}",
        "Content-Type": "application/json"
    }

    async def _request(active_session):
        try:
            async with active_session.post(
                url,
                headers=headers,
                json={"expiresIn": SUPABASE_STORAGE_SIGNED_URL_TTL},
                timeout=15.0
            ) as resp:
                if resp.status in [200, 201]:
                    data = await resp.json()
                    signed_url = data.get("signedURL") or data.get("signedUrl") or data.get("url")
                    if signed_url:
                        if signed_url.startswith("http"):
                            return signed_url
                        return f"{SUPABASE_URL.rstrip('/')}/storage/v1{signed_url}"
                text = await resp.text()
                log.warning(f"No se pudo crear signed URL de Supabase Storage: status={resp.status} response={text}")
        except Exception as e:
            log.error(f"Error creando signed URL de Supabase Storage: {e}")
        return None

    if session:
        return await _request(session)
    async with aiohttp.ClientSession() as new_session:
        return await _request(new_session)

async def upload_evidence_bytes(
    data: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
    session: aiohttp.ClientSession = None
) -> str | None:
    if not data:
        return None
    if not _storage_enabled():
        log.warning("Supabase Storage no configurado. No se pudo subir evidencia remota.")
        return None

    clean_filename = _sanitize_storage_name(filename)
    object_path = f"evidencia/{clean_filename}"
    url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{object_path}"
    headers = {
        "apikey": _storage_key(),
        "Authorization": f"Bearer {_storage_key()}",
        "Content-Type": content_type or "application/octet-stream",
        "x-upsert": "true"
    }

    async def _request(active_session):
        try:
            async with active_session.post(url, headers=headers, data=data, timeout=180.0) as resp:
                if resp.status in [200, 201]:
                    return _storage_public_url(object_path)
                text = await resp.text()
                log.error(f"Supabase Storage upload error: status={resp.status} response={text}")
        except Exception as e:
            log.error(f"Error subiendo evidencia a Supabase Storage: {e}")
        return None

    if session:
        return await _request(session)
    async with aiohttp.ClientSession() as new_session:
        return await _request(new_session)

async def upload_evidence_file(
    local_path: str,
    filename: str,
    content_type: str = "application/octet-stream",
    session: aiohttp.ClientSession = None
) -> str | None:
    try:
        with open(local_path, "rb") as f:
            return await upload_evidence_bytes(f.read(), filename, content_type, session=session)
    except Exception as e:
        log.error(f"Error leyendo evidencia temporal para subirla a Supabase Storage: {e}")
        return None

# ============================================================
#          MODELO PYDANTIC — RESPUESTA ESTRUCTURADA DE GEMINI
# ============================================================
class ReportExtraction(BaseModel):
    suspect_roblox_username: str | None  # Nombre de usuario de Roblox sospechoso
    suspect_nickname: str | None          # Apodo o apodo/displayname si no es un username claro
    cheat_category: str                  # Categoría del hack (ej: 'Auto Clicker M1', 'Fly Hack', etc.)
    cheat_details: str                   # Breve descripción del comportamiento reportado
    confidence: float                    # Confianza de la IA en la extracción (0.0 a 1.0)

# ============================================================
#        INTEGRACIÓN CON LA API OFICIAL DE ROBLOX
# ============================================================
roblox_cache = {}

async def verify_roblox_username(username: str, session: aiohttp.ClientSession) -> dict | None:
    """
    Verifica un nombre de usuario contra la API oficial de Roblox reutilizando la sesión aiohttp.
    Retorna un diccionario con 'id', 'name', 'displayName' o None si no existe.
    """
    username = username.strip()
    if not username or not re.match(r"^[a-zA-Z0-9_]{3,20}$", username):
        return None

    if username.lower() in roblox_cache:
        return roblox_cache[username.lower()]

    url = "https://users.roblox.com/v1/usernames/users"
    payload = {
        "usernames": [username],
        "excludeBannedUsers": False
    }
    try:
        async with session.post(url, json=payload, timeout=5.0) as response:
            if response.status == 200:
                data = await response.json()
                users = data.get("data", [])
                if users:
                    res = {
                        "id": users[0]["id"],
                        "name": users[0]["name"],
                        "displayName": users[0]["displayName"]
                    }
                    roblox_cache[username.lower()] = res
                    return res
    except Exception as e:
        log.error(f"Error llamando a la API de Roblox para '{username}': {e}")
    return None

# ============================================================
#        SISTEMA DE RETRY & CONTROL DE EXCEPCIONES GEMINI
# ============================================================
async def call_gemini_with_retry(client, content: str, system_prompt: str) -> ReportExtraction:
    """
    Realiza la llamada a la API de Gemini implementando reintentos con retraso
    exponencial ante cuotas superadas (429) y alerta de error crítico ante claves filtradas (403).
    """
    max_retries = 5
    base_delay = 6.0

    for attempt in range(max_retries):
        try:
            response = await client.aio.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=content,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=1.0,
                    thinking_config=types.ThinkingConfig(
                        thinking_level="MEDIUM",
                    ),
                    response_mime_type="application/json",
                    response_schema=ReportExtraction,
                )
            )
            return ReportExtraction.model_validate_json(response.text)
        except Exception as e:
            err_str = str(e)
            
            # Error 403: API Key reportada como filtrada (Leaked Key)
            if "leaked" in err_str.lower() or "403" in err_str or "permission_denied" in err_str.lower():
                log.error("❌ ERROR CRÍTICO DE GEMINI: Tu clave de API ha sido bloqueada por Google por estar FILTRADA.")
                log.error("Por favor, ve a Google AI Studio, genera una clave NUEVA y actualiza tu archivo '.env'.")
                raise Exception("API_KEY_BLOCKED_LEAKED")

            # Error 429: Cuota de peticiones superada (RESOURCE_EXHAUSTED)
            elif "429" in err_str or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower():
                wait_time = base_delay * (2.0 ** attempt)
                match = re.search(r"Please retry in ([\d\.]+)s", err_str)
                if match:
                    wait_time = float(match.group(1)) + 1.5

                log.warning(
                    f"⚠️ [LÍMITE DE CUOTA DE GEMINI] Esperando {wait_time:.1f}s antes de reintentar "
                    f"(Intento {attempt + 1}/{max_retries})..."
                )
                await asyncio.sleep(wait_time)
            else:
                log.error(f"Error inesperado en llamada de Gemini: {e}")
                raise e

    raise Exception("Límite de reintentos de Gemini superado debido a cuotas de API.")

async def classify_report_with_gemini(content: str) -> ReportExtraction:
    """
    Usa la API de Gemini para analizar y categorizar el texto del reporte.
    Realiza comprobaciones previas para evitar llamadas con claves bloqueadas.
    """
    # 1. Comprobación de API Key vacía o filtrada por defecto
    if not GEMINI_API_KEY or GEMINI_API_KEY == LEAKED_DEFAULT_KEY:
        log.warning("⚠️ Clave de Gemini no configurada o es la clave filtrada por defecto. Usando clasificación básica local.")
        return run_local_fallback_classification(content, "Clave de API filtrada/inválida. Configura tu propia clave en .env.")

    system_prompt = (
        "Eres un analista experto en reportes de trampas y hacks de Roblox.\n"
        "Analiza el texto del reporte enviado por un usuario y extrae la información requerida de acuerdo al esquema JSON.\n"
        "Debes extraer:\n"
        "1. El nombre de usuario de Roblox exacto si se indica (los nombres de Roblox no tienen espacios y pueden tener guiones bajos o números).\n"
        "2. El apodo o nickname (si solo dejaron apodo, colócalo en 'suspect_nickname' y deja 'suspect_roblox_username' como null).\n"
        "3. La categoría del hack o cheat. Debes clasificarlo estrictamente en una de las siguientes opciones:\n"
        "   - 'Auto Clicker M1'\n"
        "   - 'Aimbot'\n"
        "   - 'Fly Hack'\n"
        "   - 'Speed Hack'\n"
        "   - 'Noclip'\n"
        "   - 'Script Executor'\n"
        "   - 'Exploit/Bug Abuse'\n"
        "   - 'Otros'\n"
        "4. Detalles breves: Un resumen muy corto (una oración) de lo que dice el reporte.\n"
        "5. Confianza de la extracción (entre 0.0 y 1.0).\n\n"
        "Devuelve únicamente el objeto JSON que cumpla el esquema."
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        return await call_gemini_with_retry(client, content, system_prompt)
    except Exception as e:
        log.error(f"Error clasificando reporte. Activando fallback local. Motivo: {e}")
        return run_local_fallback_classification(content, f"Clasificador local por error en Gemini: {e}")

def run_local_fallback_classification(content: str, reason: str) -> ReportExtraction:
    """Extrae datos básicos del mensaje usando regex cuando la API de Gemini falla o está bloqueada."""
    match = re.search(r'(?:user|usuario|suspect|sospechoso|reporto a):\s*([a-zA-Z0-9_]{3,20})', content, re.IGNORECASE)
    username = match.group(1) if match else None
    
    # Detección rústica de trampas
    category = "Otros"
    content_lower = content.lower()
    if "auto" in content_lower and ("click" in content_lower or "m1" in content_lower):
        category = "Auto Clicker M1"
    elif "aim" in content_lower or "headshot" in content_lower:
        category = "Aimbot"
    elif "vola" in content_lower or "fly" in content_lower:
        category = "Fly Hack"
    elif "speed" in content_lower or "velocidad" in content_lower or "rapido" in content_lower:
        category = "Speed Hack"
    elif "noclip" in content_lower or "pared" in content_lower:
        category = "Noclip"
    elif "exec" in content_lower or "script" in content_lower or "hack" in content_lower:
        category = "Script Executor"
        
    return ReportExtraction(
        suspect_roblox_username=username,
        suspect_nickname=None,
        cheat_category=category,
        cheat_details=f"Extracción local (Fallback). Motivo: {reason}",
        confidence=0.4
    )

# ============================================================
#           DESCARGADOR DE EVIDENCIA MULTIMEDIA REMOTA
# ============================================================
async def download_evidence(message: discord.Message) -> tuple[str | None, str | None]:
    """
    Sube el primer adjunto (imagen o video) a Supabase Storage.
    Retorna una tupla (url_remota, tipo_evidencia) o (None, None).
    """
    if not message.attachments:
        return None, None

    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        is_image = content_type.startswith("image/")
        is_video = content_type.startswith("video/")

        ext = os.path.splitext(attachment.filename)[1].lower()
        if not content_type:
            if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                is_image = True
            elif ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
                is_video = True

        if is_image or is_video:
            proof_type = "image" if is_image else "video"
            clean_filename = f"{message.id}{ext}"
            try:
                data = await attachment.read()
                remote_url = await upload_evidence_bytes(data, clean_filename, content_type, session=None)
                if remote_url:
                    return remote_url, proof_type
            except Exception as e:
                log.error(f"Error subiendo archivo de evidencia {attachment.filename}: {e}")

    return None, None

async def download_video_from_url(url: str, output_filename: str) -> tuple[str | None, str | None]:
    """
    Descarga videos o imágenes desde una URL (YouTube, Streamable, Google Drive, enlaces directos)
    y los sube a Supabase Storage.
    Retorna una tupla (url_remota, tipo_evidencia) o (None, None).
    """
    import yt_dlp
    
    # 1. Comprobar si es un enlace de Google Drive
    drive_match = re.search(r'(?:drive\.google\.com/(?:file/d/|open\?id=)|docs\.google\.com/file/d/)([a-zA-Z0-9_-]{25,50})', url)
    if drive_match:
        file_id = drive_match.group(1)
        direct_url = f"https://docs.google.com/uc?export=download&id={file_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(direct_url, timeout=30.0) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        if "text/html" in content_type:
                            body_text = (await resp.read()).decode("utf-8", errors="ignore")
                            if "Virus scan warning" in body_text or 'id="download-form"' in body_text:
                                form_action = "https://drive.usercontent.google.com/download"
                                action_match = re.search(r'action="([^"]+)"', body_text)
                                if action_match:
                                    form_action = action_match.group(1)
                                
                                inputs = re.findall(r'name="([^"]+)"\s+value="([^"]+)"', body_text)
                                params = {name: val for name, val in inputs}
                                if "id" not in params:
                                    params["id"] = file_id
                                if "export" not in params:
                                    params["export"] = "download"
                                if "confirm" not in params:
                                    params["confirm"] = "t"
                                
                                async with session.get(form_action, params=params, timeout=180.0) as final_resp:
                                    if final_resp.status == 200:
                                        data = await final_resp.read()
                                        remote_url = await upload_evidence_bytes(
                                            data,
                                            f"{output_filename}.mp4",
                                            final_resp.headers.get("Content-Type", "video/mp4"),
                                            session=session
                                        )
                                        if remote_url:
                                            return remote_url, "video"
                            return None, None
                        else:
                            data = await resp.read()
                            remote_url = await upload_evidence_bytes(
                                data,
                                f"{output_filename}.mp4",
                                content_type or "video/mp4",
                                session=session
                            )
                            if remote_url:
                                return remote_url, "video"
        except Exception as e:
            log.error(f"Error descargando Google Drive ID {file_id}: {e}")
            
    # 2. Comprobar si es enlace directo con extensión de video o imagen común
    lower_url = url.lower().split("?")[0]
    is_direct_video = any(lower_url.endswith(ext) for ext in [".mp4", ".webm", ".mov", ".avi", ".mkv"])
    is_direct_image = any(lower_url.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"])
    
    if is_direct_video or is_direct_image:
        ext = os.path.splitext(lower_url)[1] or (".mp4" if is_direct_video else ".png")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30.0) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        proof_type = "video" if is_direct_video else "image"
                        remote_url = await upload_evidence_bytes(
                            data,
                            f"{output_filename}{ext}",
                            resp.headers.get("Content-Type", "video/mp4" if is_direct_video else "image/png"),
                            session=session
                        )
                        if remote_url:
                            return remote_url, proof_type
        except Exception as e:
            log.error(f"Error descargando enlace directo {url}: {e}")
            
    # 3. Descarga genérica con yt-dlp (YouTube, Streamable, etc.)
    def _sync_ytdlp_download(target_url, out_path):
        ydl_opts = {
            'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
            'outtmpl': out_path,
            'quiet': True,
            'no_warnings': True,
            'max_filesize': 120 * 1024 * 1024, # 120MB máximo
            'nocheckcertificate': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([target_url])

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_tmpl = os.path.join(tmpdir, f"{output_filename}.%(ext)s")
            await asyncio.to_thread(_sync_ytdlp_download, url, output_tmpl)
        
        # Buscar el archivo resultante con su respectiva extensión
        for ext in [".mp4", ".mkv", ".webm", ".mov", ".3gp", ".flv", ".avi"]:
            possible_path = os.path.join(EVIDENCE_DIR, f"{output_filename}{ext}")
            if os.path.exists(possible_path):
                return f"evidencia/{output_filename}{ext}", "video"
    except Exception as e:
        log.error(f"Error descargando URL {url} mediante yt-dlp: {e}")
        
    return None, None

async def download_video_from_url(url: str, output_filename: str) -> tuple[str | None, str | None]:
    """
    Version para Render: descarga evidencia de URLs usando memoria o disco temporal
    y la sube a Supabase Storage.
    """
    import yt_dlp

    drive_match = re.search(r'(?:drive\.google\.com/(?:file/d/|open\?id=)|docs\.google\.com/file/d/)([a-zA-Z0-9_-]{25,50})', url)
    if drive_match:
        file_id = drive_match.group(1)
        direct_url = f"https://docs.google.com/uc?export=download&id={file_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(direct_url, timeout=30.0) as resp:
                    if resp.status != 200:
                        return None, None

                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" not in content_type:
                        data = await resp.read()
                        remote_url = await upload_evidence_bytes(
                            data,
                            f"{output_filename}.mp4",
                            content_type or "video/mp4",
                            session=session
                        )
                        if remote_url:
                            return remote_url, "video"
                        return None, None

                    body_text = (await resp.read()).decode("utf-8", errors="ignore")
                    if "Virus scan warning" not in body_text and 'id="download-form"' not in body_text:
                        return None, None

                    form_action = "https://drive.usercontent.google.com/download"
                    action_match = re.search(r'action="([^"]+)"', body_text)
                    if action_match:
                        form_action = action_match.group(1)

                    inputs = re.findall(r'name="([^"]+)"\s+value="([^"]+)"', body_text)
                    params = {name: val for name, val in inputs}
                    params.setdefault("id", file_id)
                    params.setdefault("export", "download")
                    params.setdefault("confirm", "t")

                    async with session.get(form_action, params=params, timeout=180.0) as final_resp:
                        if final_resp.status == 200:
                            data = await final_resp.read()
                            remote_url = await upload_evidence_bytes(
                                data,
                                f"{output_filename}.mp4",
                                final_resp.headers.get("Content-Type", "video/mp4"),
                                session=session
                            )
                            if remote_url:
                                return remote_url, "video"
        except Exception as e:
            log.error(f"Error descargando Google Drive ID {file_id}: {e}")

    lower_url = url.lower().split("?")[0]
    is_direct_video = any(lower_url.endswith(ext) for ext in [".mp4", ".webm", ".mov", ".avi", ".mkv"])
    is_direct_image = any(lower_url.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"])

    if is_direct_video or is_direct_image:
        ext = os.path.splitext(lower_url)[1] or (".mp4" if is_direct_video else ".png")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30.0) as resp:
                    if resp.status == 200:
                        proof_type = "video" if is_direct_video else "image"
                        data = await resp.read()
                        remote_url = await upload_evidence_bytes(
                            data,
                            f"{output_filename}{ext}",
                            resp.headers.get("Content-Type", "video/mp4" if is_direct_video else "image/png"),
                            session=session
                        )
                        if remote_url:
                            return remote_url, proof_type
        except Exception as e:
            log.error(f"Error descargando enlace directo {url}: {e}")

    def _sync_ytdlp_download(target_url, out_path):
        ydl_opts = {
            "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
            "outtmpl": out_path,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": 120 * 1024 * 1024,
            "nocheckcertificate": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([target_url])

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_tmpl = os.path.join(tmpdir, f"{output_filename}.%(ext)s")
            await asyncio.to_thread(_sync_ytdlp_download, url, output_tmpl)

            for ext in [".mp4", ".mkv", ".webm", ".mov", ".3gp", ".flv", ".avi"]:
                possible_path = os.path.join(tmpdir, f"{output_filename}{ext}")
                if os.path.exists(possible_path):
                    remote_url = await upload_evidence_file(
                        possible_path,
                        f"{output_filename}{ext}",
                        "video/mp4",
                        session=None
                    )
                    if remote_url:
                        return remote_url, "video"
    except Exception as e:
        log.error(f"Error descargando URL {url} mediante yt-dlp: {e}")

    return None, None

# ============================================================
#               BASE DE DATOS SUPABASE (REST API)
# ============================================================

def init_db():
    # Placeholder para compatibilidad con la inicialización inicial del Cog
    log.info("🌐 Conexión inicial a Supabase configurada. Las tablas deben estar creadas en Supabase.")

async def supabase_request(method: str, endpoint: str, json_data: dict = None, params: dict = None, headers_extra: dict = None, session: aiohttp.ClientSession = None) -> list | dict | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("Supabase URL o Key no configurados en .env")
        return None
        
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    if headers_extra:
        headers.update(headers_extra)
        
    if session is None:
        async with aiohttp.ClientSession() as new_session:
            return await _supabase_request_impl(new_session, method, url, headers, json_data, params)
    else:
        return await _supabase_request_impl(session, method, url, headers, json_data, params)

async def _supabase_request_impl(session, method, url, headers, json_data, params):
    try:
        async with session.request(method, url, headers=headers, json=json_data, params=params, timeout=10.0) as resp:
            if resp.status in [200, 201]:
                text = await resp.text()
                if not text or not text.strip():
                    return []
                try:
                    import json
                    return json.loads(text)
                except Exception as json_err:
                    log.error(f"Error parseando JSON de Supabase: {json_err}. Respuesta: '{text}'")
                    return None
            elif resp.status == 204:
                return []
            else:
                text = await resp.text()
                log.error(f"Supabase REST error: status={resp.status} response={text}")
    except Exception as e:
        log.error(f"Error de conexión con Supabase: {e}")
    return None

async def get_monitored_channels(session=None) -> list[str]:
    res = await supabase_request("GET", "monitored_channels", params={"active": "eq.1", "select": "channel_id"}, session=session)
    if res:
        return [row["channel_id"] for row in res]
    return []

async def get_monitored_channels_all(session=None) -> list:
    res = await supabase_request("GET", "monitored_channels", params={"active": "eq.1"}, session=session)
    return res or []

async def add_monitored_channel(channel_id: str, channel_name: str, server_id: str, server_name: str, session=None):
    payload = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "server_id": server_id,
        "server_name": server_name,
        "active": 1
    }
    headers_extra = {"Prefer": "resolution=merge-duplicates"}
    await supabase_request("POST", "monitored_channels", json_data=payload, headers_extra=headers_extra, session=session)

async def remove_monitored_channel(channel_id: str, session=None):
    await supabase_request("DELETE", "monitored_channels", params={"channel_id": f"eq.{channel_id}"}, session=session)

async def check_report_exists(message_id: str, session=None) -> bool:
    res = await supabase_request("GET", "roblox_reports", params={"message_id": f"eq.{message_id}", "select": "id"}, session=session)
    return bool(res and len(res) > 0)

async def insert_report(report_data: dict, session=None) -> bool:
    headers_extra = {"Prefer": "resolution=merge-duplicates"}
    res = await supabase_request("POST", "roblox_reports", json_data=report_data, headers_extra=headers_extra, session=session)
    return res is not None

async def get_reports_role(role: str = "user", session=None) -> list:
    params = {"order": "timestamp.desc"}
    if role != "admin":
        params["is_verified"] = "eq.1"
    res = await supabase_request("GET", "roblox_reports", params=params, session=session)
    return res or []

async def get_stats_role(role: str = "user", session=None) -> dict:
    total_params = {"select": "id"}
    if role != "admin":
        total_params["is_verified"] = "eq.1"
    total_res = await supabase_request("GET", "roblox_reports", params=total_params, session=session)
    total = len(total_res) if total_res else 0
    
    verified_res = await supabase_request("GET", "roblox_reports", params={"is_verified": "eq.1", "select": "id"}, session=session)
    verified = len(verified_res) if verified_res else 0
    
    nickname_res = await supabase_request("GET", "roblox_reports", params={"is_verified": "eq.0", "select": "id"}, session=session)
    nicknames = len(nickname_res) if nickname_res else 0
    
    channels_res = await supabase_request("GET", "monitored_channels", params={"active": "eq.1", "select": "channel_id"}, session=session)
    channels = len(channels_res) if channels_res else 0
    
    return {
        "total_reports": total,
        "verified_count": verified,
        "nickname_count": nicknames,
        "channels_count": channels
    }

async def delete_report_by_id(report_id: int, session=None) -> bool:
    res = await supabase_request("DELETE", "roblox_reports", params={"id": f"eq.{report_id}"}, session=session)
    return res is not None

async def search_reports(query_str: str, session=None) -> list:
    query_val = f"*{query_str}*"
    params = {
        "or": f"(roblox_username_verified.ilike.{query_val},roblox_nickname_extracted.ilike.{query_val},cheat_category.ilike.{query_val})",
        "order": "timestamp.desc",
        "limit": "5"
    }
    res = await supabase_request("GET", "roblox_reports", params=params, session=session)
    return res or []

# ============================================================
#             LOGICA DE PROCESAMIENTO GENERAL
# ============================================================
async def process_report_message(message: discord.Message, session: aiohttp.ClientSession) -> dict | None:
    """
    Función unificada que procesa un mensaje de reporte.
    Llama a Gemini, comprueba contra la API de Roblox, descarga la evidencia y guarda en DB.
    """
    if not message.content.strip() and not message.attachments:
        return None

    # 1. Llamada a Gemini para clasificar y extraer
    extraction = await classify_report_with_gemini(message.content)
    
    # 2. Verificación de Roblox
    roblox_user_id = None
    roblox_username_verified = None
    roblox_profile_url = None
    roblox_avatar_url = None
    is_verified = 0

    if extraction.suspect_roblox_username:
        roblox_info = await verify_roblox_username(extraction.suspect_roblox_username, session)
        if roblox_info:
            roblox_user_id = roblox_info["id"]
            roblox_username_verified = roblox_info["name"]
            roblox_profile_url = f"https://www.roblox.com/users/{roblox_user_id}/profile"
            is_verified = 1

            # Obtener avatar headshot oficial desde la API de Roblox
            avatar_api = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={roblox_user_id}&size=48x48&format=Png&isCircular=true"
            try:
                async with session.get(avatar_api, timeout=5.0) as avatar_resp:
                    if avatar_resp.status == 200:
                        avatar_data = await avatar_resp.json()
                        data_list = avatar_data.get("data", [])
                        if data_list:
                            roblox_avatar_url = data_list[0].get("imageUrl")
            except Exception as e:
                log.error(f"Error obteniendo avatar de Roblox para ID {roblox_user_id}: {e}")

    # 3. Descarga de evidencias locales
    proof_url = message.attachments[0].url if message.attachments else None
    proof_local_path, proof_type = await download_evidence(message)

    if not proof_local_path and message.content:
        # No hay adjuntos, buscar enlaces de video/imagen en el texto
        urls = re.findall(r'(https?://[^\s\)\(]+)', message.content)
        for url in urls:
            # Omitir enlaces obvios de Roblox
            if "roblox.com" in url or "roblox-avatar" in url:
                continue
            p_local, p_type = await download_video_from_url(url, str(message.id))
            if p_local:
                proof_local_path = p_local
                proof_type = p_type
                proof_url = url
                break

    report_data = {
        "message_id": str(message.id),
        "server_name": message.guild.name if message.guild else "Mensajes Privados",
        "server_id": str(message.guild.id) if message.guild else "0",
        "channel_name": message.channel.name if hasattr(message.channel, "name") else "Privado",
        "channel_id": str(message.channel.id),
        "reporter_name": str(message.author),
        "reporter_id": str(message.author.id),
        "raw_content": message.content,
        "roblox_username_extracted": extraction.suspect_roblox_username,
        "roblox_nickname_extracted": extraction.suspect_nickname,
        "roblox_user_id": roblox_user_id,
        "roblox_username_verified": roblox_username_verified,
        "roblox_profile_url": roblox_profile_url,
        "roblox_avatar_url": roblox_avatar_url,
        "is_verified": is_verified,
        "cheat_category": extraction.cheat_category,
        "cheat_details": extraction.cheat_details,
        "proof_url": proof_url,
        "proof_local_path": proof_local_path,
        "proof_type": proof_type,
        "timestamp": message.created_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    success = await insert_report(report_data, session)
    if success:
        return report_data
    return None

# ============================================================
#          COG PRINCIPAL DE DISCORD (REPORT SCANNER)
# ============================================================
class ReportScanner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        init_db()

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        log.info("🌐 Sesión aiohttp global inicializada para el Cog.")

    async def cog_unload(self):
        if self.session:
            await self.session.close()
            log.info("🌐 Sesión aiohttp global cerrada correctamente.")

    @commands.Cog.listener()
    async def on_ready(self):
        log.info(f"✅ Selfbot iniciado como @{self.bot.user}!")
        active_channels = await get_monitored_channels(self.session)
        log.info(f"Canales de reporte cargados en el inicio: {len(active_channels)}")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id == self.bot.user.id:
            return

        monitored = await get_monitored_channels(self.session)
        if str(message.channel.id) in monitored:
            log.info(f"📩 Nuevo reporte entrante en #{message.channel.name} ({message.guild.name})")
            report = await process_report_message(message, self.session)
            if report:
                status = "✅ Verificado en Roblox" if report["is_verified"] else "⚠️ Apodo/No verificado"
                log.info(f"Reporte procesado exitosamente: Suspect={report['roblox_username_verified'] or report['roblox_nickname_extracted']} | Cheat={report['cheat_category']} | Status={status}")

    # ─────────── COMANDOS DE CONFIGURACIÓN ───────────

    @commands.command(name="add_channel", aliases=["config_channel"])
    async def add_channel(self, ctx, channel: discord.TextChannel = None):
        """Configura un canal de reportes para que el bot lo escanee en tiempo real."""
        target_channel = channel or ctx.channel
        await add_monitored_channel(
            channel_id=str(target_channel.id),
            channel_name=target_channel.name,
            server_id=str(ctx.guild.id) if ctx.guild else "0",
            server_name=ctx.guild.name if ctx.guild else "Privado",
            session=self.session
        )
        await ctx.send(f"✅ Canal de reportes configurado y activo: **#{target_channel.name}** en **{ctx.guild.name}**.", delete_after=10)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @commands.command(name="remove_channel")
    async def remove_channel(self, ctx, channel: discord.TextChannel = None):
        """Elimina un canal de la lista de escaneos automáticos."""
        target_channel = channel or ctx.channel
        await remove_monitored_channel(str(target_channel.id), session=self.session)
        await ctx.send(f"❌ Canal de reportes eliminado de la lista: **#{target_channel.name}**.", delete_after=10)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @commands.command(name="list_channels")
    async def list_channels(self, ctx):
        """Lista todos los canales configurados para el escaneo de reportes."""
        rows = await get_monitored_channels_all(session=self.session)

        if not rows:
            await ctx.send("⚠️ No hay canales configurados para escanear en este momento. Usa `.add_channel`.")
            return

        embed_content = "📋 **Canales de Reportes Monitoreados:**\n"
        for idx, ch in enumerate(rows, 1):
            embed_content += f"{idx}. **#{ch['channel_name']}** en *{ch['server_name']}* (ID: `{ch['channel_id']}`)\n"

        await ctx.send(embed_content)

    # ─────────── ESCANEO HISTÓRICO MASIVO ───────────

    @commands.command(name="scan_history")
    async def scan_history(self, ctx, limit: int = 100):
        """Realiza un escaneo histórico de mensajes anteriores en todos los canales configurados."""
        monitored = await get_monitored_channels(self.session)
        if not monitored:
            await ctx.send("❌ No hay canales de reportes configurados para escanear. Usa `.add_channel` primero.")
            return

        status_msg = await ctx.send(f"⏳ **Iniciando escaneo histórico...** Procesando hasta {limit} mensajes por canal...")
        
        total_procesados = 0
        total_guardados = 0

        for ch_id in monitored:
            channel = self.bot.get_channel(int(ch_id))
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(int(ch_id))
                except Exception:
                    log.warning(f"No se pudo acceder al canal con ID {ch_id}")
                    continue

            await status_msg.edit(content=f"⚙️ Escaneando historial de **#{channel.name}** en **{channel.guild.name}**...")
            
            try:
                async for message in channel.history(limit=limit):
                    if message.author.id == self.bot.user.id:
                        continue

                    exists = await check_report_exists(str(message.id), session=self.session)
                    if exists:
                        continue

                    total_procesados += 1
                    report = await process_report_message(message, self.session)
                    if report:
                        total_guardados += 1

                    await asyncio.sleep(1.2)
            except Exception as e:
                log.error(f"Error leyendo historial de #{channel.name}: {e}")

        await status_msg.edit(content=(
            f"✅ **Escaneo de Historial Completado!**\n"
            f"📥 Mensajes analizados nuevos: `{total_procesados}`\n"
            f"🗂️ Reportes clasificados y guardados en Supabase: `{total_guardados}`"
        ))

    # ─────────── BUSCADOR INTEGRADO EN DISCORD ───────────

    @commands.command(name="buscar")
    async def buscar(self, ctx, *, busqueda: str):
        """Busca reportes en la base de datos por usuario de Roblox o categoría de trampa."""
        rows = await search_reports(busqueda, session=self.session)

        if not rows:
            await ctx.send(f"🔍 No se encontraron reportes que coincidan con: `{busqueda}`")
            return

        response = f"🔍 **Resultados de Búsqueda para '{busqueda}':**\n\n"
        for r in rows:
            user = r.get("roblox_username_verified") or f"{r.get('roblox_nickname_extracted')} (Apodo)"
            link = f"\n🔗 **Perfil Roblox:** {r.get('roblox_profile_url')}" if r.get('roblox_profile_url') else ""
            avatar_txt = f"\n🖼️ **Avatar Headshot:** {r.get('roblox_avatar_url')}" if r.get('roblox_avatar_url') else ""
            evidencia = f"Sí ([Ver local: {r.get('proof_local_path')}])" if r.get('proof_local_path') else "No"
            response += (
                f"👤 **Sospechoso:** `{user}`{link}{avatar_txt}\n"
                f"🚨 **Cheat:** `{r.get('cheat_category')}`\n"
                f"🏢 **Servidor:** `{r.get('server_name')}`\n"
                f"📅 **Fecha:** `{r.get('timestamp')[:19]}`\n"
                f"📁 **Prueba:** `{evidencia}`\n"
                f"───────────────────\n"
            )
        await ctx.send(response)

async def setup(bot):
    await bot.add_cog(ReportScanner(bot))
