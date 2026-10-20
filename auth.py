"""Google OIDC login for a private, single-owner dashboard."""
import os
import uuid

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
OWNER_EMAIL = os.getenv("DASHBOARD_OWNER_EMAIL", "").strip().lower()
oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
)


def configured():
    return all(os.getenv(key) for key in (
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SESSION_SECRET", "DASHBOARD_OWNER_EMAIL"
    )) and len(os.getenv("SESSION_SECRET", "")) >= 32


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    try:
        user_id = uuid.UUID(request.session.get("user_id", ""))
    except (ValueError, TypeError):
        raise HTTPException(401, "Please sign in to view your transactions.")
    user = await db.get(User, user_id)
    if not user or not user.is_active or not OWNER_EMAIL or user.email.lower() != OWNER_EMAIL:
        raise HTTPException(401, "Please sign in with your configured owner account.")
    return user


def same_origin(request: Request) -> None:
    """Rejects state-changing requests that don't come from the dashboard itself."""
    if request.headers.get("origin") != FRONTEND_URL:
        raise HTTPException(403, "Invalid request origin.")


@router.get("/config")
async def auth_config():
    return {"google_enabled": configured()}


@router.get("/google")
async def login(request: Request):
    if not configured():
        return RedirectResponse(f"{FRONTEND_URL}/?error=configuration")
    request.session.clear()
    return await oauth.google.authorize_redirect(
        request,
        os.getenv("GOOGLE_REDIRECT_URI", f"{FRONTEND_URL}/api/v1/auth/google/callback"),
        prompt="select_account",
    )


@router.get("/google/callback")
async def callback(request: Request, db: AsyncSession = Depends(get_db)):
    if not configured():
        return RedirectResponse(f"{FRONTEND_URL}/?error=configuration")
    try:
        # Authlib validates state, nonce, signature, audience, issuer and expiry.
        token = await oauth.google.authorize_access_token(request)
        profile = token.get("userinfo", {})
        email = profile.get("email", "").lower()
        if not profile.get("email_verified") or email != OWNER_EMAIL:
            request.session.clear()
            return RedirectResponse(f"{FRONTEND_URL}/?error=account")
        user = await db.scalar(select(User).where(User.email == email))
        if user and (not user.is_active or (user.google_id and user.google_id != profile["sub"])):
            request.session.clear()
            return RedirectResponse(f"{FRONTEND_URL}/?error=account")
        if not user:
            user = User(email=email, google_id=profile["sub"])
            db.add(user)
        user.google_id = profile["sub"]
        user.name = profile.get("name", "")[:255]
        user.picture = profile.get("picture")
        await db.commit()
        request.session.clear()
        request.session["user_id"] = str(user.id)
        return RedirectResponse(FRONTEND_URL, status_code=303)
    except (OAuthError, ValueError, KeyError):
        request.session.clear()
        return RedirectResponse(f"{FRONTEND_URL}/?error=signin")


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"id": str(user.id), "name": user.name, "email": user.email, "picture": user.picture}


@router.post("/logout", dependencies=[Depends(same_origin)])
async def logout(request: Request):
    request.session.clear()
    return {"status": "signed_out"}
