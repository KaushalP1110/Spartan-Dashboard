"""Sign in with Google, limited to the company domain (GOOGLE_ALLOWED_DOMAIN,
default adit.com). Standard library only.

Two flows, picked by settings:

- "web"    - normal redirect sign-in. Google only allows https:// redirect
             URLs (or localhost), so it needs GOOGLE_PUBLIC_URL, e.g.
             https://spartan.adit.com. OAuth client type: Web application,
             redirect URI <GOOGLE_PUBLIC_URL>/auth/callback.
- "device" - works on a plain http://<LAN-IP> address: the page shows a
             short code, the user approves it at google.com/device with their
             Google account. OAuth client type: TVs and Limited Input devices.

The ID token comes straight from Google's token endpoint over TLS (with our
client secret), so per OpenID Connect its signature need not be re-verified;
issuer, audience, expiry, verified email and domain are still checked.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
SCOPES = "openid email profile"
TIMEOUT = 20

# Filled by server.py from the dashboard .env.
CONFIG: dict = {}


class GoogleAuthError(Exception):
    pass


def client_id() -> str | None:
    return CONFIG.get("GOOGLE_CLIENT_ID")


def is_configured() -> bool:
    return bool(CONFIG.get("GOOGLE_CLIENT_ID") and CONFIG.get("GOOGLE_CLIENT_SECRET"))


def public_url() -> str | None:
    url = CONFIG.get("GOOGLE_PUBLIC_URL")
    return url.rstrip("/") if url else None


def mode() -> str:
    return "web" if public_url() else "device"


def allowed_domains() -> list[str]:
    return [d.strip().lower() for d in CONFIG.get("GOOGLE_ALLOWED_DOMAIN", "adit.com").split(",") if d.strip()]


def redirect_uri() -> str:
    return public_url() + "/auth/callback"


def _post(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Google returns OAuth errors (authorization_pending, invalid_grant...) as 4xx JSON.
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            raise GoogleAuthError(f"Google returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GoogleAuthError(f"Google not reachable: {exc.reason}") from exc


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as exc:
        raise GoogleAuthError("Malformed ID token from Google") from exc


def identity(token_response: dict) -> dict:
    """{"email", "name"} of the signed-in user, or GoogleAuthError if the
    token isn't valid for us or the account isn't in an allowed domain."""
    if "id_token" not in token_response:
        raise GoogleAuthError(token_response.get("error_description") or token_response.get("error") or "No ID token")
    claims = _decode_jwt_payload(token_response["id_token"])
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise GoogleAuthError("ID token not issued by Google")
    if claims.get("aud") != client_id():
        raise GoogleAuthError("ID token is for a different app")
    if float(claims.get("exp", 0)) < time.time():
        raise GoogleAuthError("ID token expired")
    email = str(claims.get("email", "")).lower()
    if not email or claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("Google account email is not verified")
    domain = email.rsplit("@", 1)[-1]
    # "hd" is only set for Google Workspace accounts - a gmail.com account can't fake it.
    if domain not in allowed_domains() or str(claims.get("hd", "")).lower() != domain:
        raise GoogleAuthError(f"Only @{' / @'.join(allowed_domains())} accounts can sign in ({email} is not allowed)")
    return {"email": email, "name": claims.get("name") or email.split("@")[0]}


# --- web (redirect) flow ---------------------------------------------------------

def authorize_url(state: str) -> str:
    params = {
        "client_id": client_id(), "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": SCOPES, "state": state, "prompt": "select_account",
    }
    domains = allowed_domains()
    if len(domains) == 1:
        params["hd"] = domains[0]  # only a hint for the account chooser - identity() enforces it
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict:
    return identity(_post(TOKEN_URL, {
        "code": code, "client_id": client_id(), "client_secret": CONFIG["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
    }))


# --- device flow -------------------------------------------------------------------

def device_start() -> dict:
    """{"device_code", "user_code", "verification_url", "interval", "expires_in"}"""
    data = _post(DEVICE_URL, {"client_id": client_id(), "scope": SCOPES})
    if "device_code" not in data:
        raise GoogleAuthError(data.get("error_description") or data.get("error") or "Could not start Google sign-in")
    data.setdefault("verification_url", data.get("verification_uri", "https://www.google.com/device"))
    return data


def device_poll(device_code: str) -> tuple[str, dict | None]:
    """("pending" | "slow_down" | "denied" | "expired" | "ok", identity or None)"""
    data = _post(TOKEN_URL, {
        "client_id": client_id(), "client_secret": CONFIG["GOOGLE_CLIENT_SECRET"], "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    error = data.get("error")
    if error == "authorization_pending":
        return "pending", None
    if error == "slow_down":
        return "slow_down", None
    if error == "access_denied":
        return "denied", None
    if error == "expired_token":
        return "expired", None
    if error:
        raise GoogleAuthError(data.get("error_description") or error)
    return "ok", identity(data)
