"""Creating and updating accounts, shared by the admin API and the command line."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password, password_problem
from app.db.models import User, UserSession


class UserError(ValueError):
    pass


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise UserError("Enter a valid email address")
    return email


async def create_user(session: AsyncSession, email: str, password: str, name: str = "", is_admin: bool = False) -> User:
    email = normalize_email(email)
    if problem := password_problem(password):
        raise UserError(problem)
    if await session.scalar(select(User.id).where(User.email == email)):
        raise UserError(f"An account for {email} already exists")
    user = User(email=email, name=name.strip(), password_hash=hash_password(password), is_admin=is_admin)
    session.add(user)
    await session.commit()
    return user


async def set_password(session: AsyncSession, user: User, password: str) -> None:
    if problem := password_problem(password):
        raise UserError(problem)
    user.password_hash = hash_password(password)
    await end_sessions(session, user)  # signs the account out everywhere


async def end_sessions(session: AsyncSession, user: User) -> None:
    from sqlalchemy import delete

    await session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    await session.commit()


async def count_admins(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).where(User.is_admin.is_(True), User.is_active.is_(True))) or 0
