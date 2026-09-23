from datetime import datetime, timezone
from uuid import UUID, uuid4

import jwt
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.audit import write_audit
from app.config import get_settings
from app.database import DatabaseUnavailableError, database_is_available, get_database
from app.models import serialize, utcnow
from app.security import (AUTH_COOKIE_NAME, JWT_ALGORITHM, clear_auth_cookies, create_access_token,
                          get_current_user, set_auth_cookies, user_permissions)

router = APIRouter(prefix="/auth", tags=["authentication"])
settings = get_settings()
oauth = OAuth()
if settings.microsoft_client_id and settings.microsoft_client_secret:
    oauth.register(name="microsoft", client_id=settings.microsoft_client_id,
        client_secret=settings.microsoft_client_secret,
        server_metadata_url=f"https://login.microsoftonline.com/{settings.microsoft_tenant_id}/v2.0/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile User.Read"})


@router.get("/microsoft/login")
async def microsoft_login(request: Request):
    if settings.app_env == "development" and settings.dev_auth_bypass:
        access_token, _ = create_access_token(UUID("00000000-0000-4000-8000-000000000001"), True)
        response = RedirectResponse(f"{settings.frontend_url}/login?status=success")
        set_auth_cookies(response, access_token)
        return response
    if not database_is_available():
        raise DatabaseUnavailableError("MongoDB is unavailable for Microsoft sign-in")
    if not getattr(oauth, "microsoft", None):
        raise HTTPException(status_code=503, detail="Microsoft OAuth is not configured")
    return await oauth.microsoft.authorize_redirect(request, request.url_for("microsoft_callback"))


@router.get("/microsoft/callback", name="microsoft_callback")
async def microsoft_callback(request: Request):
    if not database_is_available():
        raise DatabaseUnavailableError("MongoDB is unavailable for Microsoft sign-in")
    try:
        token = await oauth.microsoft.authorize_access_token(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Microsoft sign-in failed") from exc
    claims = token.get("userinfo") or {}
    email = (claims.get("email") or claims.get("preferred_username") or "").lower().strip()
    email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    if not email or (settings.email_domains and email_domain not in settings.email_domains):
        await write_audit("LOGIN_DENIED", None, "session", metadata={"email": email})
        raise HTTPException(status_code=403, detail="This email domain is not allowed")
    db = get_database()
    user = await db.users.find_one_and_update(
        {"email": email},
        {"$set": {"full_name": claims.get("name") or email, "microsoft_oid": claims.get("oid") or claims.get("sub"),
                  "last_activity_at": utcnow(), "updated_at": utcnow()},
         "$setOnInsert": {"_id": uuid4(), "is_active": False, "is_super_admin": False, "role_ids": [],
                          "auth_provider": "microsoft", "created_at": utcnow(), "deleted_at": None}},
        upsert=True, return_document=True)
    if not user.get("is_active"):
        return RedirectResponse(f"{settings.frontend_url}/login?status=ACCESS_PENDING")
    access_token, _ = create_access_token(user["_id"], bool(user.get("is_super_admin")))
    response = RedirectResponse(f"{settings.frontend_url}/login?status=success")
    set_auth_cookies(response, access_token)
    await write_audit("LOGIN_SUCCESS", user["_id"], "session")
    return response


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
    token = request.cookies.get(AUTH_COOKIE_NAME)
    response = JSONResponse({"status": "success"})
    clear_auth_cookies(response)
    request.session.clear()
    payload = user["token_payload"]
    await get_database().revoked_tokens.update_one({"_id": payload["jti"]},
        {"$set": {"expires_at": datetime.fromtimestamp(payload["exp"], tz=timezone.utc)}}, upsert=True)
    await write_audit("LOGOUT", user["_id"], "session")
    return response
