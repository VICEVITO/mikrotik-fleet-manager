import os
import uuid
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
import pynetbox

app = FastAPI(
    title="MikroTik Fleet Manager Backend",
    version="1.0.0",
    description="Orchestrateur MikroTik avec inventaire NetBox et API REST RouterOS v7"
)

# Autoriser les requêtes CORS (Frontend local ou distant)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration & Paramètres système ---
def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default)

NETBOX_URL = get_env("NETBOX_URL")
NETBOX_TOKEN = get_env("NETBOX_TOKEN")
NETBOX_MANUFACTURER = get_env("NETBOX_MANUFACTURER", "mikrotik")
NETBOX_SSL_VERIFY = get_env("NETBOX_SSL_VERIFY", "true").lower() == "true"

MIKROTIK_USER = get_env("MIKROTIK_API_USER", "admin")
MIKROTIK_PASSWORD = get_env("MIKROTIK_API_PASSWORD", "")
MIKROTIK_PORT = get_env("MIKROTIK_API_PORT", "443")
MIKROTIK_USE_HTTPS = get_env("MIKROTIK_USE_HTTPS", "true").lower() == "true"
MIKROTIK_SSL_VERIFY = get_env("MIKROTIK_SSL_VERIFY", "false").lower() == "true"
MIKROTIK_TIMEOUT = float(get_env("MIKROTIK_TIMEOUT_SECONDS", "5.0"))

# --- Modèles de Données Pydantic ---
class CommandExecutionRequest(BaseModel):
    command: str = Field(..., description="Commande CLI RouterOS à exécuter", min_length=1)

class CommandResponse(BaseModel):
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None

class QuickActionRequest(BaseModel):
    action: str = Field(..., description="Action prédéfinie : reboot, check-update, export")

# --- Client HTTP MikroTik ---
def get_mikrotik_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=MIKROTIK_SSL_VERIFY,
        timeout=httpx.Timeout(MIKROTIK_TIMEOUT),
        auth=(MIKROTIK_USER, MIKROTIK_PASSWORD)
    )

def get_base_url(ip: str) -> str:
    proto = "https" if MIKROTIK_USE_HTTPS else "http"
    return f"{proto}://{ip}:{MIKROTIK_PORT}/rest"

# --- Routes NetBox (Source de Vérité) ---
@app.get("/api/health")
def health_check():
    return {"status": "healthy", "service": "mikrotik-manager-backend"}

@app.get("/api/devices")
def list_devices_from_netbox():
    """Récupère tous les routeurs MikroTik actifs depuis NetBox."""
    if not NETBOX_URL or not NETBOX_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Variables NETBOX_URL ou NETBOX_TOKEN manquantes."
        )

    try:
        nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
        nb.http_session.verify = NETBOX_SSL_VERIFY
        
        devices = nb.dcim.devices.filter(
            manufacturer=NETBOX_MANUFACTURER,
            status="active"
        )
        
        device_list = []
        for d in devices:
            ip = str(d.primary_ip).split('/')[0] if d.primary_ip else None
            custom_fields = getattr(d, "custom_fields", {})
            
            device_list.append({
                "id": d.id,
                "name": d.name,
                "ip": ip,
                "site": d.site.name if d.site else "N/A",
                "model": d.device_type.model if d.device_type else "Inconnu",
                "serial": d.serial or "N/A",
                "target_ros_version": custom_fields.get("target_ros_version", None),
                "target_firmware_version": custom_fields.get("target_firmware_version", None)
            })
            
        return {
            "count": len(device_list),
            "devices": device_list
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erreur de communication avec NetBox: {str(exc)}"
        )

# --- Routes Opérationnelles MikroTik REST ---
@app.get("/api/devices/{ip}/status")
async def get_device_status(ip: str):
    """Interroge directement l'API REST d'un MikroTik pour son état et ses firmwares."""
    base_url = get_base_url(ip)
    
    async with get_mikrotik_client() as client:
        try:
            # 1. Ressources système (CPU, ROS version, Uptime)
            res_resource = await client.get(f"{base_url}/system/resource")
            if res_resource.status_code != 200:
                return {
                    "ip": ip,
                    "online": False,
                    "error": f"HTTP {res_resource.status_code}: {res_resource.text}"
                }
            resource_data = res_resource.json()

            # 2. Informations RouterBOARD (firmware actif vs upgrade dispo)
            res_rb = await client.get(f"{base_url}/system/routerboard")
            rb_data = res_rb.json() if res_rb.status_code == 200 else {}

            return {
                "ip": ip,
                "online": True,
                "ros_version": resource_data.get("version"),
                "board_name": resource_data.get("board-name"),
                "cpu_load": resource_data.get("cpu-load"),
                "uptime": resource_data.get("uptime"),
                "free_memory_mb": round(int(resource_data.get("free-memory", 0)) / (1024 * 1024), 2),
                "current_firmware": rb_data.get("current-firmware"),
                "upgrade_firmware": rb_data.get("upgrade-firmware")
            }

        except (httpx.ConnectTimeout, httpx.ReadTimeout):
            return {"ip": ip, "online": False, "error": "Timeout de connexion au routeur"}
        except httpx.ConnectError:
            return {"ip": ip, "online": False, "error": "Connexion refusée ou routeur injoignable"}
        except Exception as exc:
            return {"ip": ip, "online": False, "error": str(exc)}

@app.post("/api/devices/{ip}/execute", response_model=CommandResponse)
async def execute_command(ip: str, payload: CommandExecutionRequest):
    """
    Exécute une commande arbitraire en créant un script éphémère via l'API REST RouterOS v7,
    puis nettoie le script après exécution.
    """
    base_url = get_base_url(ip)
    script_name = f"api_exec_{uuid.uuid4().hex[:8]}"
    
    async with get_mikrotik_client() as client:
        try:
            # 1. Création du script temporaire
            create_resp = await client.put(
                f"{base_url}/system/script",
                json={
                    "name": script_name,
                    "source": payload.command,
                    "dont-require-permissions": "yes"
                }
            )
            
            if create_resp.status_code not in (200, 201):
                return CommandResponse(
                    success=False,
                    error=f"Impossible de créer le script: {create_resp.text}"
                )

            # 2. Exécution du script
            run_resp = await client.post(f"{base_url}/system/script/{script_name}/run")
            
            # 3. Suppression du script pour ne rien laisser sur l'équipement
            await client.delete(f"{base_url}/system/script/{script_name}")

            if run_resp.status_code in (200, 204):
                return CommandResponse(
                    success=True,
                    output="Commande exécutée avec succès sur le routeur."
                )
            else:
                return CommandResponse(
                    success=False,
                    error=f"Erreur à l'exécution: {run_resp.text}"
                )

        except Exception as exc:
            # Tentative de nettoyage si le script avait été créé
            try:
                await client.delete(f"{base_url}/system/script/{script_name}")
            except Exception:
                pass
            return CommandResponse(success=False, error=str(exc))

@app.post("/api/devices/{ip}/action")
async def trigger_quick_action(ip: str, payload: QuickActionRequest):
    """Déclenche une action d'administration prédéfinie."""
    base_url = get_base_url(ip)
    
    async with get_mikrotik_client() as client:
        try:
            if payload.action == "reboot":
                resp = await client.post(f"{base_url}/system/reboot")
                return {"success": resp.status_code in (200, 204), "action": "reboot", "detail": "Reboot déclenché"}
            
            elif payload.action == "check-update":
                resp = await client.post(f"{base_url}/system/package/update/check-for-updates")
                if resp.status_code == 200:
                    status_resp = await client.get(f"{base_url}/system/package/update")
                    return {"success": True, "action": "check-update", "data": status_resp.json()}
                return {"success": False, "error": resp.text}
                
            elif payload.action == "upgrade-firmware":
                # Demande la mise à jour du RouterBOOT
                resp = await client.post(f"{base_url}/system/routerboard/upgrade")
                return {"success": resp.status_code in (200, 204), "action": "upgrade-firmware", "detail": "Firmware upgrade ordonné"}

            else:
                raise HTTPException(status_code=400, detail=f"Action inconnue: {payload.action}")

        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Erreur d'action: {str(exc)}")
