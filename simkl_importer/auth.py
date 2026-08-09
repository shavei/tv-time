"""Obtaining a Simkl user access_token.

Two flows are supported:

  * ``oob``  - the standard authorization-code flow using the
    ``urn:ietf:wg:oauth:2.0:oob`` redirect URI. Simkl shows the code on screen,
    you paste it back here, and we exchange it for a token. Needs the client
    secret.
  * ``pin``  - the device/PIN flow. You type a 5-character code at
    https://simkl.com/pin/ and the script polls until it is approved. No client
    secret involved.

Tokens do not expire; they are only invalidated if you revoke the app under
https://simkl.com/settings/connected-apps/.
"""

from __future__ import annotations

import time
import urllib.parse
import webbrowser
from typing import Optional

from .client import SimklClient, SimklError
from .config import OOB_REDIRECT_URI, WEB_BASE, Credentials, Store


def authorize_url(client_id: str, redirect_uri: str = OOB_REDIRECT_URI) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }
    )
    return f"{WEB_BASE}/oauth/authorize?{query}"


def oob_flow(client: SimklClient, store: Store) -> str:
    """Authorization-code flow with the out-of-band redirect URI."""
    credentials = client.credentials
    if not credentials.client_secret:
        raise SimklError(
            "The out-of-band flow needs the client secret. Re-enter credentials, "
            "or use the PIN flow instead."
        )

    url = authorize_url(credentials.client_id, credentials.redirect_uri)
    print("\n1. Open this URL in a browser and approve the app:\n")
    print(f"   {url}\n")
    print("2. Simkl will show you a code (it is also in the address bar as ?code=...).")
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless box; the printed URL is the fallback

    code = input("\n   Paste the code here: ").strip()
    if code.startswith("http"):  # someone pasted the whole redirect URL
        parsed = urllib.parse.urlparse(code)
        code = urllib.parse.parse_qs(parsed.query).get("code", [""])[0].strip()
    if not code:
        raise SimklError("No code entered.")

    print("   Exchanging the code for an access token...")
    response = client.post(
        "/oauth/token",
        payload={
            "code": code,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "redirect_uri": credentials.redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    token = (response or {}).get("access_token")
    if not token:
        raise SimklError(f"No access_token in the response: {response}")
    return token


def pin_flow(client: SimklClient, store: Store) -> str:
    """Device/PIN flow - no client secret, no copy-pasting long codes."""
    start = client.get("/oauth/pin", params={"client_id": client.credentials.client_id})
    if not start or "user_code" not in start:
        raise SimklError(f"Unexpected /oauth/pin response: {start}")

    user_code = start["user_code"]
    verification_url = start.get("verification_url") or f"{WEB_BASE}/pin/"
    expires_in = int(start.get("expires_in", 900))
    interval = max(5, int(start.get("interval", 5)))

    print(f"\n   Go to {verification_url} and enter this code: {user_code}")
    print(f"   Waiting up to {expires_in // 60} minutes for you to approve it...")
    try:
        webbrowser.open(verification_url)
    except Exception:
        pass

    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        status = client.get(
            f"/oauth/pin/{user_code}", params={"client_id": client.credentials.client_id}
        )
        if not status:
            continue
        if status.get("result") == "OK" and status.get("access_token"):
            return status["access_token"]
        message = (status.get("message") or "").lower()
        if "slow down" in message:
            interval += 2
        elif "authorization pending" not in message:
            print(f"   ...{status.get('message')}")
        print("   ...still waiting", end="\r", flush=True)

    raise SimklError("The PIN expired before it was approved. Start over.")


def ensure_token(
    client: SimklClient,
    store: Store,
    flow: str = "oob",
    force: bool = False,
) -> Credentials:
    """Return credentials that carry a usable access token, saving it to disk."""
    credentials = client.credentials
    if credentials.access_token and not force:
        return credentials

    token = pin_flow(client, store) if flow == "pin" else oob_flow(client, store)
    credentials.access_token = token
    store.save_credentials(credentials)
    client.credentials = credentials
    print(f"\n   Access token saved to {store.credentials_path}")
    return credentials


def verify_token(client: SimklClient) -> Optional[dict]:
    """Cheap authenticated call so a bad token surfaces before a big import.

    /users/settings is a POST on Simkl, not a GET.
    """
    try:
        return client.post("/users/settings", payload={}, authenticated=True)
    except SimklError:
        return None
