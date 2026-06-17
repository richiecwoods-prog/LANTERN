from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen


def _base_candidates() -> list[Path]:
    out: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        out.extend([exe_dir, exe_dir / "_internal"])
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            out.append(Path(meipass))
    out.append(Path(__file__).resolve().parent)
    # Deduplicate while preserving order.
    seen = set()
    unique = []
    for p in out:
        key = str(p)
        if key not in seen:
            unique.append(p)
            seen.add(key)
    return unique


def find_project_root() -> Path:
    for base in _base_candidates():
        if (base / "app" / "moth_pi_setup" / "moth_analysis" / "api.py").exists():
            return base
    raise RuntimeError("Cannot locate app/moth_pi_setup/moth_analysis/api.py beside the launcher.")


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def wait_for(url: str, seconds: float = 15.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=1.0) as r:
                if 200 <= r.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def start_server(root: Path, host: str, port: int) -> None:
    app_dir = root / "app" / "moth_pi_setup"
    sys.path.insert(0, str(app_dir))
    os.environ.setdefault("LANTERN_IMPORT_QUALITY_MODE", "standard")

    def run() -> None:
        import uvicorn
        uvicorn.run("moth_analysis.api:app", host=host, port=port, log_level="info", access_log=False)

    thread = threading.Thread(target=run, name="lantern-api", daemon=True)
    thread.start()


def main() -> int:
    parser = argparse.ArgumentParser(description="LANTERN desktop launcher by Eagle Eye Innovations")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/app?v=0122")
    parser.add_argument("--no-browser-fallback", action="store_true")
    args = parser.parse_args()

    root = find_project_root()
    url = f"http://{args.host}:{args.port}{args.path}"
    health_url = f"http://{args.host}:{args.port}/api/platform/health"

    if not port_open(args.host, args.port):
        start_server(root, args.host, args.port)
        if not wait_for(health_url, seconds=20.0):
            raise RuntimeError("LANTERN API did not become ready. Run Start_LANTERN_Local.ps1 -Foreground for the full traceback.")

    try:
        import webview  # type: ignore
        webview.create_window("LANTERN", url, width=1440, height=940, min_size=(1100, 720))
        webview.start()
    except Exception:
        if args.no_browser_fallback:
            raise
        webbrowser.open(url)
        print(f"LANTERN by Eagle Eye Innovations opened in browser: {url}")
        print("Close this window after finishing your session.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
