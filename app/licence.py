import requests
import logging
from . import db

logger = logging.getLogger(__name__)

LEMON_BASE = "https://api.lemonsqueezy.com/v1/licenses"

def _instance_id():
    return db.get_setting("license_instance_id")

def activate(key: str) -> dict:
    existing = _instance_id()
    if existing:
        result = validate(key)
        if result["valid"]:
            return result
        db.set_setting("license_instance_id", "")
    try:
        resp = requests.post(
            f"{LEMON_BASE}/activate",
            json={"license_key": key, "instance_name": "bridge-bank"},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("activated"):
            instance_id = data["instance"]["id"]
            db.set_setting("license_instance_id", instance_id)
            return {"valid": True, "error": None}
        else:
            msg = data.get("error") or data.get("message") or "Invalid license key."
            return {"valid": False, "error": msg}
    except requests.RequestException as e:
        logger.warning("License activate failed (network): %s", e)
        return {"valid": True, "error": None, "offline": True}

def validate(key: str = None) -> dict:
    from . import config
    key = key or config.LICENCE_KEY
    if not key:
        return {"valid": False, "error": "No license key configured."}
    instance_id = _instance_id()
    if not instance_id:
        return activate(key)
    try:
        resp = requests.post(
            f"{LEMON_BASE}/validate",
            json={"license_key": key, "instance_id": instance_id},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("valid"):
            return {"valid": True, "error": None}
        else:
            msg = data.get("error") or data.get("message") or "Invalid license key."
            return {"valid": False, "error": msg}
    except requests.RequestException as e:
        logger.warning("License check failed (network): %s", e)
        return {"valid": True, "error": None, "offline": True}

def deactivate() -> dict:
    from . import config
    key         = config.LICENCE_KEY
    instance_id = _instance_id()
    if not key or not instance_id:
        return {"success": False, "error": "No active license to deactivate."}
    try:
        resp = requests.post(
            f"{LEMON_BASE}/deactivate",
            json={"license_key": key, "instance_id": instance_id},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("deactivated"):
            db.set_setting("license_instance_id", "")
            return {"success": True, "error": None}
        else:
            msg = data.get("error") or data.get("message") or "Deactivation failed."
            return {"success": False, "error": msg}
    except requests.RequestException as e:
        logger.warning("License deactivate failed (network): %s", e)
        return {"success": False, "error": str(e)}

def get_activation_info() -> dict:
    from . import config
    key         = config.LICENCE_KEY
    instance_id = _instance_id()
    if not key or not instance_id:
        return {"usage": 0, "limit": 2}
    try:
        resp = requests.post(
            f"{LEMON_BASE}/validate",
            json={"license_key": key, "instance_id": instance_id},
            timeout=5,
        )
        if resp.status_code == 200:
            lk = resp.json().get("license_key", {})
            return {
                "usage": lk.get("activation_usage", 0),
                "limit": lk.get("activation_limit", 2),
            }
    except Exception:
        pass
    return {"usage": 0, "limit": 2}
