from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.audit import write_audit
from app.config import get_settings
from app.database import get_database
from app.models import serialize, utcnow
from app.security import (clear_auth_cookies, create_access_token,
                          get_current_user, set_auth_cookies, user_permissions)

router = APIRouter(prefix="/auth", tags=["authentication"])
settings = get_settings()


@router.get("/bootstrap")
async def bootstrap(user: dict = Depends(get_current_user)):
    permissions = await user_permissions(user)
    clean = {k: v for k, v in user.items() if k != "token_payload"}
    return {"user": serialize(clean), "permissions": sorted(permissions)}


@router.post("/heartbeat")
async def heartbeat(response: Response, user: dict = Depends(get_current_user)):
    if settings.app_env == "development" and settings.dev_auth_bypass:
        return {"status": "ok", "mode": "localhost_development"}
    await get_database().users.update_one({"_id": user["_id"]}, {"$set": {"last_activity_at": utcnow()}})
    token, _ = create_access_token(user["_id"], bool(user.get("is_super_admin")))
    set_auth_cookies(response, token)
    return {"status": "ok"}


@router.post("/logout")
async def logout(request: Request, user: dict = Depends(get_current_user)):
    if settings.app_env == "development" and settings.dev_auth_bypass:
        response = JSONResponse({"status": "success"})
        clear_auth_cookies(response)
        request.session.clear()
        return response
    response = JSONResponse({"status": "success"})
    clear_auth_cookies(response)
    request.session.clear()
    payload = user["token_payload"]
    await get_database().revoked_tokens.update_one({"_id": payload["jti"]},
        {"$set": {"expires_at": datetime.fromtimestamp(payload["exp"], tz=timezone.utc)}}, upsert=True)
    await write_audit("LOGOUT", user["_id"], "session")
    return response
