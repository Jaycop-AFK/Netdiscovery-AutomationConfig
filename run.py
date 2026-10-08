import argparse
import os
import threading
import webbrowser
from pathlib import Path

from netscope.envfile import load_dotenv

load_dotenv()          # .env -> OPENROUTER_API_KEY, NETSCOPE_PASSWORD ... (must happen before the server modules read the environment)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="NetScope - network discovery & configuration for Cisco IOS / EVE-NG")
    ap.add_argument("--host", default=None, help="bind address (default 127.0.0.1; with --share: all interfaces, IPv4+IPv6)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--share", action="store_true",
                    help="safe mode for letting friends try it through a public port/tunnel: access code required, "
                         "simulated devices only (no real devices / COM / EVE-NG / API-key changes), AI quota, separate data folder")
    ap.add_argument("--ngrok", action="store_true", help="with --share: start an ngrok tunnel and print the public link (needs ngrok installed + authtoken)")
    a = ap.parse_args()
    if a.ngrok:
        a.share = True

    if a.share:
        os.environ["NETSCOPE_SHARE"] = "1"
        os.environ.setdefault("NETSCOPE_DATA", str(Path(__file__).resolve().parent / "data_share"))
    from netscope import auth
    code = auth.configure_share() if a.share else None
    from netscope.server import serve

    host = a.host or ("::" if a.share else "127.0.0.1")
    if a.share:
        print("=" * 64)
        print(" NetScope - SHARE MODE (simulated lab only)")
        print(f" Access code for your friends:  {code}")
        if a.ngrok:
            print(" ngrok: the public link will be printed below in a few seconds")
        else:
            print(f" 1) Run in another terminal:  ngrok http {a.port}   (or: python run.py --ngrok, or VS Code Ports > Public)")
            print(" 2) Send the public https link + the access code")
        print(" Friends press the Demo button, then try Initial Config / Discovery / Prompt / Ping")
        print(" Real devices, COM ports, EVE-NG and your API key settings are NOT reachable in this mode.")
        print("=" * 64)
    elif not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{a.port}")).start()
    if a.ngrok:
        from netscope import tunnel
        tunnel.start_in_background(a.port, code)
    serve(host, a.port)
