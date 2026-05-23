"""Auth-setup flows keyed by auth_type.

One generic flow per credential class, not per API. The contract:

- `cookie`: open the API's homepage in the user's browser, wait for
  them to log in (in their existing Chrome session, with whatever
  password manager / 2FA they normally use), then read the cookie
  jar from Chrome's local store. The CLI receives `<API>_COOKIE` =
  full Cookie header.
- everything else: prompt the user to paste a token. Written to
  `<API>_TOKEN` (or whatever single env var is conventional).

Per-API specifics that aren't part of the protocol — e.g., a
homepage URL that doesn't match `https://www.<api>.com` — live in
the small override tables in this module, not in a per-API recipe.
The point is that adding a new cookie API should require *no new
code* in cocoon, only a possible one-line override if the
convention breaks for that name.
"""

import sys
import webbrowser

# Per-API homepage override for cases where the `www.<api>.com`
# convention doesn't apply. Start empty; add entries only when a
# user trips over a real mismatch. This is not a recipe — it's a
# hint that the generic flow uses.
_HOMEPAGE_OVERRIDES: dict[str, str] = {}

# Same shape for cookie domain. browser_cookie3 filters by domain;
# the default is the homepage's netloc minus a leading "www.".
_COOKIE_DOMAIN_OVERRIDES: dict[str, str] = {}


def run(api: str, auth_type: str) -> dict[str, str] | None:
    """Dispatch to the generic flow for `auth_type`. Returns the env
    dict to persist, or None if the user aborted / a flow refused."""
    if auth_type == "none":
        print(f"error: '{api}' doesn't require auth", file=sys.stderr)
        return None
    if auth_type == "cookie":
        return cookie_flow(api)
    return token_paste_flow(api, auth_type)


def cookie_flow(api: str) -> dict[str, str] | None:
    """Open the API's homepage in the user's browser; after they
    confirm they're signed in, read cookies for the domain from
    Chrome's local store and serialize as a Cookie header."""
    try:
        import browser_cookie3
    except ImportError:
        print("error: browser_cookie3 not installed; install cocoon-mcp[browser] "
              "or pass --token / --env explicitly", file=sys.stderr)
        return None

    homepage = _homepage_for(api)
    domain = _cookie_domain_for(api)
    print(f"Opening {homepage}")
    print(f"  Sign in to {api} in your browser if you aren't already.")
    print(f"  Cocoon will read cookies for {domain!r} from Chrome after you confirm.")
    webbrowser.open(homepage)
    print()
    try:
        input("Press Enter when signed in (or Ctrl-C to abort): ")
    except EOFError:
        return None

    try:
        jar = browser_cookie3.chrome(domain_name=domain)
    except Exception as exc:
        print(f"error: couldn't read Chrome cookies for {domain!r}: {exc}",
              file=sys.stderr)
        print("  (cocoon needs Chrome and keychain access; on macOS you may "
              "be prompted to allow access)", file=sys.stderr)
        return None

    cookies = list(jar)
    if not cookies:
        print(f"error: no cookies found for {domain!r} in Chrome — make sure "
              f"you're signed in and try again", file=sys.stderr)
        return None

    header = "; ".join(f"{c.name}={c.value}" for c in cookies)
    env_var = _env_var_for(api, suffix="COOKIE")
    print(f"got {len(cookies)} cookies for {domain}; writing {env_var}")
    return {env_var: header}


def token_paste_flow(api: str, auth_type: str) -> dict[str, str] | None:
    """Prompt for a single token value. Used for api_key, bearer_token,
    and any other manual-paste credential class. Cocoon doesn't know
    the signup URL — that's per-API documentation the user already
    has open or finds via web search."""
    env_var = _env_var_for(api, suffix="TOKEN")
    print(f"Setup for `{api}` ({auth_type}):")
    print(f"  Paste your {auth_type} value below. It'll be saved as {env_var}.")
    try:
        value = input("> ").strip()
    except EOFError:
        value = ""
    if not value:
        print("error: empty input; not writing", file=sys.stderr)
        return None
    return {env_var: value}


def _homepage_for(api: str) -> str:
    if api in _HOMEPAGE_OVERRIDES:
        return _HOMEPAGE_OVERRIDES[api]
    return f"https://www.{api}.com"


def _cookie_domain_for(api: str) -> str:
    if api in _COOKIE_DOMAIN_OVERRIDES:
        return _COOKIE_DOMAIN_OVERRIDES[api]
    return f"{api}.com"


def _env_var_for(api: str, *, suffix: str) -> str:
    """Conventional env var: <API>_<SUFFIX> with hyphens turned into
    underscores. The downstream CLI is expected to read from this
    name; CLIs that diverge from the convention need a per-binary
    metadata mapping once upstream exposes it."""
    return f"{api.upper().replace('-', '_')}_{suffix}"
