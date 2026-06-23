# Deploy en Render

Este proyecto ya queda preparado para correr como un Web Service de Render con FastAPI y el selfbot en el mismo proceso.

## Variables necesarias

Configura estas variables en Render:

```env
USER_TOKEN=token_de_discord_self
GEMINI_API_KEY=clave_de_gemini
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_KEY=anon_key_o_service_key
SUPABASE_SERVICE_ROLE_KEY=service_role_key_para_subir_evidencia_desde_el_servidor
SUPABASE_STORAGE_BUCKET=evidencia
ADMIN_PASSWORD=una_clave_fuerte
WEB_HOST=0.0.0.0
```

Render entrega el puerto en `PORT`, asi que no necesitas configurar `WEB_PORT`.

## Supabase Storage

Crea un bucket llamado `evidencia` y marcala como publico si quieres que el dashboard reproduzca imagenes y videos directamente.

La app sube archivos a:

```txt
storage/evidencia/evidencia/<archivo>
```

Usa `SUPABASE_SERVICE_ROLE_KEY` en Render para que el backend pueda subir evidencia sin abrir permisos publicos de escritura. No la pongas en el dashboard ni en codigo frontend.

El archivo `supabase_storage_setup.sql` crea el bucket publico y la lectura publica para que el dashboard pueda reproducir las evidencias.

## Migrar evidencias antiguas

Primero prueba en modo lectura:

```bash
python migrate_evidence_to_supabase.py
```

Para subir los archivos existentes y actualizar `proof_local_path` en Supabase:

```bash
MIGRATE_EVIDENCE_APPLY=1 python migrate_evidence_to_supabase.py
```

En PowerShell:

```powershell
$env:MIGRATE_EVIDENCE_APPLY="1"; python migrate_evidence_to_supabase.py
```

## Mantenerlo activo

En Render Free el servicio puede dormir si no recibe trafico. Usa un monitor externo que haga ping a:

```txt
/healthz
```

cada 10 a 14 minutos.
