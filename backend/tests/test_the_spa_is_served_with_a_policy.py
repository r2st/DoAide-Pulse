"""The document an XSS would actually run in is the one nothing was protecting.

Herald serves two things on one origin: the API on :3006, which returns JSON,
and the built SPA on :3007, which returns the only HTML document in the system.
A Content-Security-Policy on a JSON response is close to inert — there is no
document for it to govern — so the policy that would actually stop injected
script from executing is the one on the SPA, and until now nothing set it.

The gap was easy to miss because both halves looked covered. The API sets a CSP
and always has. Caddy's shared ``header`` block sets nosniff, frame options and
HSTS for the whole vhost. Neither of them sets a CSP on the SPA: Caddy's block
has no CSP directive at all, and ``static-server.mjs`` sent exactly one security
header, ``X-Content-Type-Options``.

These tests run the real server — the same file the unit starts, with node, on a
port — rather than reading it as text, because what is being asserted is what a
browser receives. A grep for the header name would have passed on a policy
attached to only the happy path, which is the specific mistake worth catching
here: the SPA fallback and the 404 are responses too, and the fallback is the
one every real page load goes through.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

_REPO = Path(__file__).resolve().parents[2]
_SERVER = _REPO / "deploy" / "static-server.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    """A minimal dist tree, so the test does not depend on a built frontend.

    The server cares about file existence and extensions, not contents, and
    building the SPA to assert a header would tie this to npm.
    """
    directory = tmp_path_factory.mktemp("dist")
    (directory / "index.html").write_text("<!doctype html><title>Herald</title>", "utf-8")
    (directory / "assets").mkdir()
    (directory / "assets" / "index-abc123.js").write_text("export default 1;", "utf-8")
    return directory


@pytest.fixture(scope="module")
def base_url(root: Path):
    port = _free_port()
    process = subprocess.Popen(
        ["node", str(_SERVER), "--root", str(root), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = (process.stdout.read() or b"").decode(errors="replace")
                pytest.fail(f"static server exited early:\n{output}")
            try:
                httpx.get(f"{url}/index.html", timeout=0.5)
                break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            pytest.fail("static server did not start within 15s")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only if node hangs
            process.kill()
            process.wait(timeout=10)
        # Explicitly, and not left to the garbage collector: the suite runs with
        # ``filterwarnings = ["error"]``, and a pipe closed during collection
        # surfaces as an unraisable ResourceWarning attributed to whichever
        # test happened to be running at the time.
        if process.stdout is not None:
            process.stdout.close()


#: Every path a browser can land on, including the two that are not files: the
#: history-API fallback that serves every real route, and a missing asset.
_PATHS = ["/index.html", "/assets/index-abc123.js", "/calendar", "/assets/missing.js"]


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize(
    "header",
    [
        "content-security-policy",
        "x-content-type-options",
        "x-frame-options",
        "referrer-policy",
        "permissions-policy",
    ],
)
def test_the_header_is_on_every_response(base_url: str, path: str, header: str):
    assert header in httpx.get(f"{base_url}{path}").headers, f"{header} missing on {path}"


def test_the_spa_fallback_still_serves_the_shell(base_url: str):
    """The policy must not have cost the routing behaviour it wraps."""
    resp = httpx.get(f"{base_url}/calendar")
    assert resp.status_code == 200
    assert "<title>Herald</title>" in resp.text


def test_a_missing_asset_is_still_a_404(base_url: str):
    assert httpx.get(f"{base_url}/assets/missing.js").status_code == 404


def test_script_may_only_come_from_this_origin(base_url: str):
    """The directive doing the real work.

    Vite emits one external module and no inline script, so this needs no
    escape hatch — and without one, an injected ``<script>`` or a smuggled
    ``onclick`` has nowhere to run.
    """
    csp = httpx.get(f"{base_url}/index.html").headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "'unsafe-eval'" not in csp


def test_the_fonts_the_build_actually_loads_are_allowed(base_url: str):
    """frontend/index.html pulls its stylesheet and font files from Google.

    A policy derived from a template rather than from the build would blank the
    site's typography on deploy — the failure mode that gets a CSP reverted
    instead of fixed.
    """
    csp = httpx.get(f"{base_url}/index.html").headers["content-security-policy"]
    assert "https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in csp


@pytest.mark.parametrize(
    "directive",
    ["default-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'"],
)
def test_the_rest_of_the_policy(base_url: str, directive: str):
    csp = httpx.get(f"{base_url}/index.html").headers["content-security-policy"]
    assert directive in csp


def test_asset_caching_survived_the_change(base_url: str):
    """Fingerprinted assets stay immutable and the shell stays uncached.

    Spreading a shared header object into ``writeHead`` is exactly the edit
    that can overwrite a per-response ``Cache-Control``, and a shell pinned for
    a year would leave every client on stale JS after a deploy.
    """
    assert "immutable" in httpx.get(f"{base_url}/assets/index-abc123.js").headers["cache-control"]
    assert httpx.get(f"{base_url}/index.html").headers["cache-control"] == "no-cache"


def test_the_traversal_guard_still_holds(base_url: str):
    """Unrelated to this round, and the sort of thing a refactor breaks."""
    resp = httpx.get(f"{base_url}/../../etc/passwd")
    assert resp.status_code in {200, 404}
    assert "root:" not in resp.text
