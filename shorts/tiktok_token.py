#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from shorts.publisher import exchange_tiktok_auth_code, refresh_tiktok_access_token, tiktok_client_settings

DEFAULT_TIKTOK_REDIRECT_URI = "http://localhost:6583/callback/"
DEFAULT_OAUTH_TIMEOUT_SECONDS = 300


def print_env_bundle(bundle):
    env_lines = []
    for key, env_name in (
        ("access_token", "TIKTOK_ACCESS_TOKEN"),
        ("refresh_token", "TIKTOK_REFRESH_TOKEN"),
        ("open_id", "TIKTOK_OPEN_ID"),
        ("scope", "TIKTOK_TOKEN_SCOPE"),
    ):
        value = bundle.get(key)
        if value:
            env_lines.append(f"{env_name}={value}")
    print("\n".join(env_lines))


def make_code_verifier():
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(64))


def make_code_challenge(code_verifier):
    return hashlib.sha256(code_verifier.encode("utf-8")).hexdigest()


def resolve_redirect_uri(raw_value):
    redirect_uri = (raw_value or os.environ.get("TIKTOK_REDIRECT_URI") or DEFAULT_TIKTOK_REDIRECT_URI).strip()
    parsed = urlsplit(redirect_uri)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("TikTok redirect URI must start with http:// or https://.")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("This local OAuth helper only supports localhost or 127.0.0.1 redirect URIs.")
    if parsed.port is None:
        raise RuntimeError("TikTok redirect URI must include an explicit port number.")
    callback_path = parsed.path or "/"
    if not callback_path.startswith("/"):
        callback_path = f"/{callback_path}"
    return {
        "redirect_uri": redirect_uri,
        "host": parsed.hostname,
        "port": parsed.port,
        "path": callback_path,
    }


def build_tiktok_authorize_url(redirect_uri, scope, state, code_challenge, disable_auto_auth=None):
    client = tiktok_client_settings()
    params = {
        "client_key": client["client_key"],
        "response_type": "code",
        "scope": scope,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if disable_auto_auth is not None:
        params["disable_auto_auth"] = int(disable_auto_auth)
    return f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}"


def update_env_file(path, updates):
    env_path = Path(path).expanduser()
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    seen = set()
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            key = key.strip()
            if key in updates and updates[key] is not None:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(line)

    for key, value in updates.items():
        if value is None or key in seen:
            continue
        output.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    return str(env_path.resolve())


def wait_for_tiktok_callback(host, port, callback_path, timeout_seconds):
    callback_event = threading.Event()
    callback_data = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format_text, *args):
            return

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != callback_path:
                self.send_error(404)
                return

            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            callback_data.update(query)

            if query.get("error"):
                body = (
                    "<h1>TikTok Authorization Failed</h1>"
                    f"<p>{query.get('error_description') or query.get('error')}</p>"
                    "<p>You can close this tab.</p>"
                )
            else:
                body = (
                    "<h1>TikTok Authorization Received</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                )

            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            callback_event.set()

    server = ThreadingHTTPServer((host, port), CallbackHandler)
    server.daemon_threads = True
    server.timeout = 0.5

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        if not callback_event.wait(timeout_seconds):
            raise RuntimeError(
                f"Timed out waiting for TikTok OAuth callback on http://{host}:{port}{callback_path}"
            )
        return callback_data
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_local_oauth_flow(
    redirect_uri="",
    *,
    scope="user.info.basic,video.publish,video.upload",
    timeout_seconds=DEFAULT_OAUTH_TIMEOUT_SECONDS,
    open_browser=False,
    disable_auto_auth=None,
    env_file="",
    write_env=True,
):
    redirect = resolve_redirect_uri(redirect_uri)
    code_verifier = make_code_verifier()
    code_challenge = make_code_challenge(code_verifier)
    state = secrets.token_urlsafe(24)

    authorize_url = build_tiktok_authorize_url(
        redirect["redirect_uri"],
        scope,
        state,
        code_challenge,
        disable_auto_auth,
    )

    print("Open this TikTok authorize URL in your browser:\n")
    print(authorize_url)
    print("")

    if open_browser:
        opened = webbrowser.open(authorize_url)
        if not opened:
            print("Browser launch failed. Open the URL above manually.\n")

    callback = wait_for_tiktok_callback(
        redirect["host"],
        redirect["port"],
        redirect["path"],
        timeout_seconds,
    )

    callback_error = (callback.get("error") or "").strip()
    if callback_error:
        description = (callback.get("error_description") or "").strip()
        raise RuntimeError(f"TikTok OAuth failed: {callback_error} | {description}".strip(" |"))

    returned_state = (callback.get("state") or "").strip()
    if returned_state != state:
        raise RuntimeError("TikTok OAuth state mismatch. Refusing to exchange the authorization code.")

    code = (callback.get("code") or "").strip()
    if not code:
        raise RuntimeError("TikTok callback did not include an authorization code.")

    bundle = exchange_tiktok_auth_code(
        code,
        redirect["redirect_uri"],
        code_verifier=code_verifier,
    )

    env_path = None
    if write_env:
        target_env_file = env_file or os.environ.get("TIKTOK_ENV_FILE") or str(Path(SCRIPT_DIR) / "tiktok.env")
        env_path = update_env_file(
            target_env_file,
            {
                "TIKTOK_CLIENT_KEY": tiktok_client_settings()["client_key"],
                "TIKTOK_CLIENT_SECRET": tiktok_client_settings()["client_secret"],
                "TIKTOK_REDIRECT_URI": redirect["redirect_uri"],
                "TIKTOK_ACCESS_TOKEN": bundle.get("access_token"),
                "TIKTOK_REFRESH_TOKEN": bundle.get("refresh_token"),
                "TIKTOK_OPEN_ID": bundle.get("open_id"),
                "TIKTOK_TOKEN_SCOPE": bundle.get("scope"),
            },
        )

    return {
        **bundle,
        "redirect_uri": redirect["redirect_uri"],
        "authorize_url": authorize_url,
        "env_file": env_path,
    }


def main():
    parser = argparse.ArgumentParser(description="Exchange, refresh, or capture TikTok OAuth user tokens.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    exchange_parser = subparsers.add_parser(
        "exchange-code",
        help="Exchange a TikTok authorization code for a user access token and refresh token.",
    )
    exchange_parser.add_argument("--code", required=True, help="Authorization code returned by TikTok OAuth.")
    exchange_parser.add_argument(
        "--redirect-uri",
        required=True,
        help="Redirect URI used when the authorization code was requested.",
    )
    exchange_parser.add_argument(
        "--code-verifier",
        default="",
        help="Optional PKCE code verifier for mobile/desktop flows.",
    )

    local_parser = subparsers.add_parser(
        "local-oauth",
        help="Run a local 127.0.0.1 callback server, wait for TikTok OAuth, and exchange the code automatically.",
    )
    local_parser.add_argument(
        "--redirect-uri",
        default="",
        help="Registered TikTok redirect URI. Defaults to TIKTOK_REDIRECT_URI or the built-in localhost callback.",
    )
    local_parser.add_argument(
        "--scope",
        default="user.info.basic,video.publish,video.upload",
        help="OAuth scope string to request. Defaults to user.info.basic,video.publish,video.upload.",
    )
    local_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_OAUTH_TIMEOUT_SECONDS,
        help="How long to wait for TikTok to call back to the local server.",
    )
    local_parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Try to open the TikTok authorize URL in your default browser.",
    )
    local_parser.add_argument(
        "--disable-auto-auth",
        type=int,
        default=None,
        help="Optional TikTok disable_auto_auth flag. Omit it to match the default Desktop Login Kit flow.",
    )
    local_parser.add_argument(
        "--env-file",
        default="",
        help="Env file to update with the returned TikTok tokens. Defaults to shorts/tiktok.env.",
    )
    local_parser.add_argument(
        "--no-write-env",
        action="store_true",
        help="Do not write the returned token bundle into the local env file.",
    )

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Refresh a TikTok user access token using a refresh token.",
    )
    refresh_parser.add_argument(
        "--refresh-token",
        default="",
        help="Optional refresh token override. Defaults to TIKTOK_REFRESH_TOKEN from the env file.",
    )

    parser.add_argument(
        "--print-env",
        action="store_true",
        help="Print shell-style env lines instead of JSON.",
    )

    args = parser.parse_args()

    if args.command == "exchange-code":
        result = exchange_tiktok_auth_code(
            args.code,
            args.redirect_uri,
            code_verifier=args.code_verifier,
        )
    elif args.command == "local-oauth":
        result = run_local_oauth_flow(
            args.redirect_uri,
            scope=args.scope,
            timeout_seconds=args.timeout_seconds,
            open_browser=args.open_browser,
            disable_auto_auth=args.disable_auto_auth,
            env_file=args.env_file,
            write_env=not args.no_write_env,
        )
    else:
        result = refresh_tiktok_access_token(args.refresh_token)

    if args.print_env:
        print_env_bundle(result)
        return

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
