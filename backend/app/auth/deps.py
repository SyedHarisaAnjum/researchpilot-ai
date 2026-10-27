"""FastAPI dependencies for the signed-in user."""

from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import token_hash
from app.db.databases import get_session
from app.db.models import User, UserSession, utcnow

SESSION_COOKIE = "research_session"
# Refresh last_seen_at at most this often, so reads don't each cause a write.
TOUCH_INTERVAL = timedelta(minutes=5)


def session_token(request: Request) -> str | None:
    """The session token from the cookie, or from `Authorization: Bearer` for scripts."""
    if token := request.cookies.get(SESSION_COOKIE):
        return token
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    token = session_token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    row = (
        await session.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == token_hash(token), UserSession.expires_at > utcnow(), User.is_active.is_(True))
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired. Sign in again")
    user_session, user = row
    last_seen = user_session.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=utcnow().tzinfo)
    if utcnow() - last_seen > TOUCH_INTERVAL:
        user_session.last_seen_at = utcnow()
        await session.commit()
    request.state.user = user
    return user


async def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can do this")
    return user
