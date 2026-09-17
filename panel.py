#!/usr/bin/env python3
"""Local control panel launcher.

Usage: python panel.py [--port 8000] [--no-browser]

Serves the dashboard on 127.0.0.1 only, debug/reloader off. Optionally
opens the system browser. Never starts trading on launch; trading only
runs when you click Start in the panel (simulation or MT5 demo).
"""
import argparse
import os
import sys
import threading
import webbrowser

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from app.dashboard.server import create_app  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="ai-trader local control panel")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"ai-trader panel on {url} (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
