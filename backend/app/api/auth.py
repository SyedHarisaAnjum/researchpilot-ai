"""Sign in, sign out, and the current user; plus admin account management."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import SESSION_COOKIE, admin_user, current_user, session_token
from app.auth.security import DUMMY_HASH, new_session_token, token_hash, verify_password
from app.auth.users import UserError, count_admins, create_user, end_sessions, normalize_email, set_password
from app.config import get_settings
from app.db.databases import get_session
from app.db.models import User, UserSession, utcnow
from app.schemas import CreateUserRequest, LoginRequest, UpdateUserRequest, UserOut
from app.services.rate_limit import get_limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/admin/users", tags=["admin"])


def client_ip(request: Request) -> str:
    # Behind Nginx, uvicorn's --proxy-headers puts the real client address here.
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=UserOut)
async def login(body: LoginRequest, request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    try:
        email = normalize_email(body.email)
    except UserError:
        email = body.email.strip().lower()
    # Per address and per account, so one attacker can't lock everyone out and can't spread guesses.
    limiter = get_limiter()
    window = [(settings.login_attempts_per_15_minutes, 900)]
    await limiter.hit(f"login-ip:{client_ip(request)}", [(settings.login_attempts_per_15_minutes * 3, 900)], "sign-in attempts")
    await limiter.hit(f"login-email:{email}", window, "sign-in attempts")

    user = await session.scalar(select(User).where(User.email == email))
    valid = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not (user and valid and user.is_active):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    token, digest = new_session_token()
    expires = utcnow() + timedelta(days=settings.session_days)
    session.add(UserSession(user_id=user.id, token_hash=digest, expires_at=expires,
                            user_agent=(request.headers.get("user-agent") or "")[:255]))
    user.last_login_at = utcnow()
    # Old sessions are cleaned up as people sign in.
    await session.execute(delete(UserSession).where(UserSession.expires_at <= utcnow()))
    await session.commit()
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
        secure=settings.secure_cookies, samesite="strict", path="/",
    )
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    if token := session_token(request):
        await session.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))
        await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=get_settings().secure_cookies, samesite="strict")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user


# --- Admin: accounts are created by admins only ---


@admin_router.get("", response_model=list[UserOut])
async def list_users(_: User = Depends(admin_user), session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(User).order_by(User.created_at))).all()


@admin_router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def add_user(body: CreateUserRequest, _: User = Depends(admin_user), session: AsyncSession = Depends(get_session)):
    try:
        return await create_user(session, body.email, body.password, body.name, body.is_admin)
    except UserError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@admin_router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str, body: UpdateUserRequest, admin: User = Depends(admin_user), session: AsyncSession = Depends(get_session)
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    removing_admin = (body.is_admin is False or body.is_active is False) and user.is_admin
    if removing_admin and await count_admins(session) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "There must be at least one active admin")
    if user.id == admin.id and body.is_active is False:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't deactivate your own account")
    try:
        if body.password is not None:
            await set_password(session, user, body.password)
    except UserError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if body.name is not None:
        user.name = body.name.strip()
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    if body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            await end_sessions(session, user)
    await session.commit()
    await session.refresh(user)
    return user
