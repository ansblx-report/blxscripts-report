import asyncio
import importlib.util
import mimetypes
import os
import sys
from pathlib import Path


def load_env():
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_scan_all():
    spec = importlib.util.spec_from_file_location("scan_all", "scan_all (1).py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_all"] = module
    spec.loader.exec_module(module)
    return module


async def main():
    load_env()
    scan_all = load_scan_all()
    evidence_dir = Path(scan_all.EVIDENCE_DIR)
    apply_updates = os.getenv("MIGRATE_EVIDENCE_APPLY") == "1"

    if not evidence_dir.exists():
        print(f"No existe {evidence_dir}. Nada que migrar.")
        return

    files = [p for p in evidence_dir.iterdir() if p.is_file()]
    print(f"Archivos encontrados: {len(files)}")
    print("Modo:", "APLICAR" if apply_updates else "DRY RUN")

    migrated = 0
    for path in files:
        old_ref = f"evidencia/{path.name}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        print(f"Subiendo {old_ref}...")

        if not apply_updates:
            continue

        remote_url = await scan_all.upload_evidence_file(str(path), path.name, content_type)
        if not remote_url:
            print(f"  Fallo subida: {path.name}")
            continue

        result = await scan_all.supabase_request(
            "PATCH",
            "roblox_reports",
            json_data={"proof_local_path": remote_url},
            params={"proof_local_path": f"eq.{old_ref}"},
            headers_extra={"Prefer": "return=minimal"}
        )
        if result is not None:
            migrated += 1
            print(f"  OK -> {remote_url}")
        else:
            print(f"  Subido, pero no se pudo actualizar DB: {remote_url}")

    print(f"Migrados: {migrated}/{len(files)}")


if __name__ == "__main__":
    asyncio.run(main())
