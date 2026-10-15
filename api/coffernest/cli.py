"""``coffernest`` command: api | worker | migrate | vapid | seed-demo."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="coffernest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    api = sub.add_parser("api", help="run the HTTP API (migrates first)")
    api.add_argument("--reload", action="store_true")
    api.add_argument("--no-migrate", action="store_true")
    sub.add_parser("worker", help="run the background worker")
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("vapid", help="generate Web Push VAPID keys")
    demo = sub.add_parser("seed-demo", help="create a demo household with sample data")
    demo.add_argument("--email", default="demo@example.com")
    demo.add_argument("--password", default="demo-password-123")
    demo.add_argument("--base-url", help="seed a running server over HTTP instead")
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        _migrate()
    elif args.cmd == "api":
        import uvicorn

        from coffernest.config import get_settings

        if not args.no_migrate:
            _migrate()
        uvicorn.run(
            "coffernest.main:app",
            host="0.0.0.0",
            port=get_settings().port,
            reload=args.reload,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    elif args.cmd == "worker":
        from coffernest.worker import run_forever

        asyncio.run(run_forever())
    elif args.cmd == "vapid":
        from coffernest.services.push import generate_vapid

        public, private = generate_vapid()
        print(f"VAPID_PUBLIC_KEY={public}")
        print("VAPID_PRIVATE_KEY=" + private.strip().replace("\n", "\n"))
    elif args.cmd == "seed-demo":
        from coffernest.demo import seed

        asyncio.run(seed(args.email, args.password, args.base_url))
    else:  # pragma: no cover
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
