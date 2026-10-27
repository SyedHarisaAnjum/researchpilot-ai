"""Account management from the command line, e.g. to create the first admin.

    python -m app.cli create-user --email you@example.com --name "You" --admin
    python -m app.cli set-password --email you@example.com
    python -m app.cli list-users

In Docker:  docker compose run --rm api python -m app.cli create-user --email you@example.com --admin

The password is read without echoing it (or from stdin with --password-stdin, for scripts).
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.auth.users import UserError, create_user, normalize_email, set_password
from app.db.databases import SessionLocal, engine
from app.db.models import User


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise UserError("The passwords did not match")
    return first


async def run(args: argparse.Namespace) -> None:
    async with SessionLocal() as session:
        if args.command == "create-user":
            user = await create_user(session, args.email, read_password(args.password_stdin), args.name, args.admin)
            print(f"Created {'admin' if user.is_admin else 'user'} {user.email} ({user.id})")
        elif args.command == "set-password":
            user = await session.scalar(select(User).where(User.email == normalize_email(args.email)))
            if user is None:
                raise UserError(f"No account for {args.email}")
            await set_password(session, user, read_password(args.password_stdin))
            print(f"Password changed for {user.email}; existing sessions were signed out")
        elif args.command == "list-users":
            for user in await session.scalars(select(User).order_by(User.created_at)):
                flags = ", ".join(f for f, on in (("admin", user.is_admin), ("inactive", not user.is_active)) if on)
                print(f"{user.email:40} {user.name:25} {flags}")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage ResearchLab accounts")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user")
    create.add_argument("--email", required=True)
    create.add_argument("--name", default="")
    create.add_argument("--admin", action="store_true")
    create.add_argument("--password-stdin", action="store_true")
    password = commands.add_parser("set-password")
    password.add_argument("--email", required=True)
    password.add_argument("--password-stdin", action="store_true")
    commands.add_parser("list-users")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except UserError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
