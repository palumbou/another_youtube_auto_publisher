"""One-time local OAuth flow: obtain the YouTube refresh token for the publisher.

Prerequisites: in Google Cloud console create an OAuth client of type
"Desktop app" and download it as client_secret.json next to this script.

Usage:  python scripts/authorize.py [path/to/client_secret.json]

Opens the browser for consent, catches the redirect on localhost, then writes
oauth_token.json (gitignored) with exactly the JSON to store in Secrets Manager:
  {"client_id": ..., "client_secret": ..., "refresh_token": ...}
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/youtube.upload"
PORT = 8765


def main() -> None:
    secret_path = Path(sys.argv[1] if len(sys.argv) > 1 else "client_secret.json")
    conf = json.loads(secret_path.read_text())
    conf = conf.get("installed") or conf.get("web") or conf
    client_id, client_secret = conf["client_id"], conf["client_secret"]
    redirect_uri = f"http://localhost:{PORT}/"

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # force a fresh refresh_token even if already granted
    })
    url = f"{AUTH_URL}?{params}"
    print(f"Opening browser for consent…\n{url}\n")
    webbrowser.open(url)

    code_holder: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code_holder.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done, you can close this tab.")

        def log_message(self, *args):
            pass

    with HTTPServer(("localhost", PORT), Handler) as server:
        while "code" not in code_holder and "error" not in code_holder:
            server.handle_request()
    if "error" in code_holder:
        raise SystemExit(f"consent denied: {code_holder['error']}")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code_holder["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as resp:
        tokens = json.loads(resp.read())
    if "refresh_token" not in tokens:
        raise SystemExit(f"no refresh_token in response: {tokens}")

    out = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
    }
    Path("oauth_token.json").write_text(json.dumps(out, indent=2))
    print("Wrote oauth_token.json — store its content in Secrets Manager, e.g.:")
    print("  aws secretsmanager create-secret --name youtube-publisher "
          "--secret-string file://oauth_token.json")


if __name__ == "__main__":
    main()
