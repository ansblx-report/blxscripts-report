import os
import sys
import asyncio
import sqlite3
import logging
import importlib.util
import shutil
import time
import aiohttp
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn
import discord
from discord.ext import commands

# ============================================================
#              CARGA MANUAL DE VARIABLES DE ENTORNO
# ============================================================
def load_env():
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

load_env()

USER_TOKEN = os.getenv("USER_TOKEN", "TU_TOKEN_DE_DISCORD_AQUI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", 8000)))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Configuración del log
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("Launcher")

# ============================================================
#       IMPORTACIÓN DINÁMICA DE COG CON PARÉNTESIS EN NOMBRE
# ============================================================
spec = importlib.util.spec_from_file_location("scan_all", "scan_all (1).py")
scan_all = importlib.util.module_from_spec(spec)
sys.modules["scan_all"] = scan_all
spec.loader.exec_module(scan_all)

# ============================================================
#             CONFIGURACIÓN DE DISCORD Y FASTAPI
# ============================================================
bot = commands.Bot(command_prefix=".", self_bot=True)
app = FastAPI(title="Anti-Script Control Center")

# ============================================================
#                      RUTAS DE FASTAPI
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Sirve el archivo HTML del dashboard."""
    if os.path.exists("dashboard.html"):
        with open("dashboard.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Error: dashboard.html no encontrado</h1>", status_code=404)

@app.get("/healthz")
async def healthz():
    """Endpoint liviano para monitores externos y health checks de Render."""
    return {
        "status": "ok",
        "bot_ready": bot.is_ready(),
        "storage_bucket": scan_all.SUPABASE_STORAGE_BUCKET
    }

@app.get("/evidencia/{filename}")
async def serve_evidence(filename: str):
    """Sirve los archivos de evidencia descargados de forma local."""
    file_path = os.path.join(scan_all.EVIDENCE_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="Evidencia no encontrada")

@app.get("/api/reports")
async def get_reports(role: str = "user"):
    """Devuelve la lista de reportes según el rol del solicitante desde Supabase."""
    reports = await scan_all.get_reports_role(role)
    return reports

@app.get("/api/stats")
async def get_stats(role: str = "user"):
    """Devuelve estadísticas consolidadas adaptadas al rol del solicitante desde Supabase."""
    stats = await scan_all.get_stats_role(role)
    return stats

@app.get("/api/config")
async def get_config():
    """Devuelve los canales configurados para el escaneo."""
    channels = await scan_all.get_monitored_channels_all()
    return {"channels": channels}

@app.post("/api/config/channels")
async def api_add_channel(data: dict):
    """Añade un canal de reportes mediante su ID."""
    channel_id = data.get("channel_id")
    if not channel_id:
        raise HTTPException(status_code=400, detail="ID de canal requerido")
    
    try:
        ch_id_int = int(channel_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de canal inválido (debe ser numérico)")
    
    try:
        channel = bot.get_channel(ch_id_int)
        if not channel:
            channel = await bot.fetch_channel(ch_id_int)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo encontrar el canal en Discord: {e}")
        
    if not channel:
        raise HTTPException(status_code=404, detail="El canal no existe o el bot no tiene acceso a él")
        
    server_id = str(channel.guild.id) if channel.guild else "0"
    server_name = channel.guild.name if channel.guild else "Privado"
    
    await scan_all.add_monitored_channel(str(channel.id), channel.name, server_id, server_name)
    log.info(f"Canal configurado vía API web: #{channel.name} ({server_name})")
    
    return {"status": "success", "detail": f"Canal #{channel.name} de {server_name} añadido correctamente."}

@app.delete("/api/config/channels/{channel_id}")
async def api_delete_channel(channel_id: str):
    """Elimina un canal de los escaneos."""
    await scan_all.remove_monitored_channel(channel_id)
    log.info(f"Canal {channel_id} eliminado de monitoreo vía API web")
    return {"status": "success", "detail": "Canal eliminado de la lista de monitoreo."}

@app.delete("/api/reports/{report_id}")
async def delete_report(report_id: int):
    """Elimina un reporte de la base de datos."""
    try:
        success = await scan_all.delete_report_by_id(report_id)
        if success:
            log.info(f"Reporte con ID {report_id} descartado vía API web.")
            return {"status": "success", "detail": f"Reporte #{report_id} eliminado correctamente."}
        raise HTTPException(status_code=404, detail="Reporte no encontrado")
    except Exception as e:
        log.error(f"Error eliminando reporte {report_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/login")
async def api_login(data: dict):
    """Valida credenciales contra la tabla de usuarios en Supabase."""
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Usuario y contraseña requeridos")
        
    auth = await scan_all.supabase_request("GET", "users", params={"username": f"eq.{username}"})
    if auth and len(auth) > 0:
        db_user = auth[0]
        if db_user.get("password") == password:
            return {
                "status": "success", 
                "role": db_user.get("role", "user"), 
                "username": db_user.get("username"),
                "token": "supabase_session_active"
            }
    raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

@app.post("/api/auth/register")
async def api_register(data: dict):
    """Registra un nuevo usuario en la tabla de Supabase."""
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Usuario y contraseña requeridos")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="El usuario debe tener al menos 3 caracteres")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 4 caracteres")
    
    # Verificar si el usuario ya existe
    existing = await scan_all.supabase_request("GET", "users", params={"username": f"eq.{username}"})
    if existing and len(existing) > 0:
        raise HTTPException(status_code=409, detail="Ese nombre de usuario ya está registrado")
    
    # Crear usuario con rol 'user' por defecto
    new_user = {
        "username": username,
        "password": password,
        "role": "user"
    }
    result = await scan_all.supabase_request("POST", "users", json_data=new_user, headers_extra={"Prefer": "return=representation"})
    if result is not None:
        return {"status": "success", "role": "user", "username": username}
    raise HTTPException(status_code=500, detail="Error al crear la cuenta")

@app.post("/api/reports/manual")
async def create_manual_report(
    username: str = Form(...),
    cheat_category: str = Form("Otros"),
    cheat_details: str = Form(""),
    file: UploadFile = File(None),
    evidence_url: str = Form(None)
):
    """Permite a los usuarios comunes o administradores registrar un reporte de forma manual subiendo fotos/videos o ingresando una URL de evidencia."""
    if not file and not evidence_url:
        raise HTTPException(status_code=400, detail="Debes subir una imagen/video o ingresar una URL de evidencia válida.")

    message_id = f"manual_{int(time.time() * 1000)}"
    proof_local_path = None
    proof_type = None
    proof_url = None
    
    if file:
        ext = os.path.splitext(file.filename)[1].lower()
        clean_filename = f"{message_id}{ext}"
        data = await file.read()
        proof_local_path = await scan_all.upload_evidence_bytes(
            data,
            clean_filename,
            file.content_type or "application/octet-stream"
        )
        if not proof_local_path:
            raise HTTPException(
                status_code=500,
                detail="No se pudo subir la evidencia a Supabase Storage. Revisa el bucket y las policies."
            )
        proof_type = "image"
        if ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
            proof_type = "video"
    else:
        # Descargar desde URL
        log.info(f"Descargando evidencia manual desde URL: {evidence_url}")
        p_local, p_type = await scan_all.download_video_from_url(evidence_url, message_id)
        if not p_local:
            raise HTTPException(
                status_code=400, 
                detail="No se pudo descargar la evidencia desde la URL ingresada. Asegúrate de usar un enlace válido de YouTube, Streamable o un link directo de video/imagen."
            )
        proof_local_path = p_local
        proof_type = p_type
        proof_url = evidence_url
        
    # Verificar en Roblox
    roblox_user_id = None
    roblox_username_verified = None
    roblox_profile_url = None
    roblox_avatar_url = None
    is_verified = 0
    
    async with aiohttp.ClientSession() as session:
        roblox_info = await scan_all.verify_roblox_username(username, session)
        if roblox_info:
            roblox_user_id = roblox_info["id"]
            roblox_username_verified = roblox_info["name"]
            roblox_profile_url = f"https://www.roblox.com/users/{roblox_user_id}/profile"
            is_verified = 1
            
            # Obtener avatar headshot
            avatar_api = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={roblox_user_id}&size=48x48&format=Png&isCircular=true"
            try:
                async with session.get(avatar_api, timeout=5.0) as avatar_resp:
                    if avatar_resp.status == 200:
                        avatar_data = await avatar_resp.json()
                        data_list = avatar_data.get("data", [])
                        if data_list:
                            roblox_avatar_url = data_list[0].get("imageUrl")
            except Exception as e:
                log.error(f"Error cargando avatar en reporte manual: {e}")

    report_data = {
        "message_id": message_id,
        "server_name": "Reporte Manual Web",
        "server_id": "0",
        "channel_name": "Web",
        "channel_id": "0",
        "reporter_name": "Usuario Web",
        "reporter_id": "0",
        "raw_content": f"Manual Report: User={username}, Cheat={cheat_category}, Details={cheat_details}",
        "roblox_username_extracted": username,
        "roblox_nickname_extracted": username if not is_verified else None,
        "roblox_user_id": roblox_user_id,
        "roblox_username_verified": roblox_username_verified,
        "roblox_profile_url": roblox_profile_url,
        "roblox_avatar_url": roblox_avatar_url,
        "is_verified": is_verified,
        "cheat_category": cheat_category,
        "cheat_details": cheat_details,
        "proof_url": proof_url,
        "proof_local_path": proof_local_path,
        "proof_type": proof_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    success = await scan_all.insert_report(report_data)
    if success:
        return {"status": "success", "detail": "Reporte manual registrado correctamente.", "report": report_data}
    raise HTTPException(status_code=500, detail="No se pudo registrar el reporte manual en la base de datos.")

@app.post("/api/config/scan")
async def trigger_scan(limit: int = 100):
    """Lanza un escaneo histórico en segundo plano para no bloquear el panel web."""
    cog = bot.get_cog("ReportScanner")
    if not cog:
        raise HTTPException(status_code=500, detail="El bot de Discord no se encuentra listo aún")
    
    asyncio.create_task(run_background_scan(cog, limit))
    return {"status": "success", "detail": "Escaneo histórico masivo lanzado en segundo plano."}

async def run_background_scan(cog, limit: int):
    log.info(f"Lanzando escaneo histórico de hasta {limit} mensajes vía panel web...")
    monitored = await scan_all.get_monitored_channels()
    
    for ch_id in monitored:
        channel = bot.get_channel(int(ch_id))
        if not channel:
            try:
                channel = await bot.fetch_channel(int(ch_id))
            except Exception:
                continue
        if channel:
            try:
                async for message in channel.history(limit=limit):
                    if message.author.id == bot.user.id:
                        continue

                    # Comprobar si ya está indexado en Supabase
                    exists = await scan_all.check_report_exists(str(message.id))
                    if exists:
                        continue
                    
                    await scan_all.process_report_message(message, cog.session)
                    # Dormimos 1.5 segundos entre mensajes para no saturar la cuota gratis de la API de Gemini
                    await asyncio.sleep(1.5)
            except Exception as e:
                log.error(f"Error procesando historial de #{channel.name} en segundo plano: {e}")
    log.info("Escaneo de historial en segundo plano finalizado.")

# ============================================================
#             INICIO CONCURRENTE DEL BOT Y SERVIDOR
# ============================================================

@bot.event
async def on_connect():
    log.info("Bot conectado a Discord!")
    try:
        if not bot.get_cog("ReportScanner"):
            await bot.add_cog(scan_all.ReportScanner(bot))
    except Exception as e:
        log.error(f"Error cargando el Cog: {e}")

async def start_services():
    if USER_TOKEN == "TU_TOKEN_DE_DISCORD_AQUI" or not USER_TOKEN:
        log.error("❌ ERROR CRÍTICO: Debes configurar tu token de Discord en el archivo '.env'.")
        log.error("Edita el archivo '.env' en la raíz e introduce tu token personal de Discord.")
        return

    config = uvicorn.Config(app=app, host=WEB_HOST, port=WEB_PORT, log_level="warning")
    server = uvicorn.Server(config)
    
    log.info(f"🚀 Iniciando Panel Web en http://{WEB_HOST}:{WEB_PORT}")
    log.info("🔌 Iniciando sesión en Discord...")

    await asyncio.gather(
        server.serve(),
        bot.start(USER_TOKEN, reconnect=True)
    )

if __name__ == "__main__":
    try:
        asyncio.run(start_services())
    except KeyboardInterrupt:
        log.info("Apagando servicios...")
