"""Command-line entry: python -m app <command>"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="command-center")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the dashboard server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-scheduler", action="store_true")
    y = sub.add_parser("sync", help="run a sync now")
    y.add_argument("--only", nargs="*", help="canvas gmail calendar plan notes")
    sub.add_parser("google-auth", help="connect Google Calendar + Gmail (opens a browser)")
    sub.add_parser("brief", help="print today's brief")
    sub.add_parser("check", help="check configuration and connections")
    d = sub.add_parser("demo", help="load sample data (for trying it out)")
    d.add_argument("--db", help="database path to write demo data into")
    args = p.parse_args(argv)

    from .config import get_config

    cfg = get_config()

    if args.cmd == "serve":
        import uvicorn

        from .main import create_app

        app = create_app(scheduler=not args.no_scheduler)
        uvicorn.run(app, host=args.host or cfg.get("server", "host", "0.0.0.0"),
                    port=args.port or int(cfg.get("server", "port", 8765)), log_level="info")
        return 0

    from .db import get_db

    if args.cmd == "sync":
        from .sync.runner import run_all

        res = run_all(get_db(), cfg, only=set(args.only) if args.only else None)
        for r in res:
            print(("OK  " if r["ok"] else "ERR ") + r["source"] + ": " + json.dumps(r["result"], default=str)[:400])
        return 0 if all(r["ok"] for r in res) else 1

    if args.cmd == "google-auth":
        from .sync.google import run_auth_flow

        print("Saved Google token to", run_auth_flow(cfg))
        return 0

    if args.cmd == "brief":
        from fastapi.testclient import TestClient

        from .main import create_app

        print(TestClient(create_app(scheduler=False)).get("/api/brief").text)
        return 0

    if args.cmd == "check":
        from . import llm

        print("Config file:", cfg.path or "(none — copy config.example.toml to config.toml)")
        print("Database:   ", cfg.db_path)
        print("Canvas:     ", "token" if cfg.get("canvas", "token") else "ics feed" if cfg.get("canvas", "ics_url")
              else "NOT CONFIGURED")
        print("Google:     ", "connected" if (cfg.data_dir / "google_token.json").exists()
              else "not connected (run: python -m app google-auth)")
        for f in cfg.notes_folders:
            print("Notes:      ", f.path, "OK" if cfg.resolve(f.path).exists() else "MISSING")
        print("Local AI:   ", json.dumps(llm.status()))
        print("Passcode:   ", "set" if cfg.passcode and cfg.passcode != "change-me" else "NOT SET — change it in config.toml")
        return 0

    if args.cmd == "demo":
        from .demo import load_demo

        load_demo(get_db(), cfg)
        print("Demo data loaded.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
