// Serves the built Vite SPA (frontend/dist) over HTTP with history-API
// fallback. Zero dependencies on purpose: the production box should not need
// an npm install to serve static files, and `vite preview` is a dev tool that
// binds loosely and prints a banner nobody reads in a journal.
//
//   node deploy/static-server.mjs --root /opt/Herald/frontend/dist \
//                                 --host 172.18.0.1 --port 3007
//
// Anything that isn't an existing file resolves to index.html, because the
// router is client-side: /calendar is a real route to React and a 404 to the
// filesystem.
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const ROOT = resolve(arg("root", "frontend/dist"));
const HOST = arg("host", "127.0.0.1");
const PORT = Number(arg("port", "3007"));

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".map": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".webmanifest": "application/manifest+json",
};

// Vite fingerprints everything under /assets, so those are immutable; the
// entry HTML must never be cached or a deploy leaves clients on stale JS.
function cacheControl(pathname) {
  return pathname.startsWith("/assets/")
    ? "public, max-age=31536000, immutable"
    : "no-cache";
}

// The SPA is the document an XSS would actually execute in — the API only ever
// returns JSON — so the Content-Security-Policy that matters is this one, and
// until now nothing set it: Caddy's shared header block covers nosniff, frame
// options and HSTS for the whole vhost, but no CSP, and this server sent only
// the nosniff.
//
// The policy is derived from what the build actually loads (frontend/index.html
// plus `npm run build` output), not from a template:
//
//   script-src 'self'   Vite emits one external module and no inline script, so
//                       this needs no escape hatch. It is the directive doing
//                       the real work — an injected <script> or a smuggled
//                       onclick has nowhere to run.
//   style-src           Google Fonts serves the stylesheet from googleapis.com.
//                       'unsafe-inline' stays because a stylesheet cannot be
//                       nonced from here and the injection it would permit is
//                       cosmetic once script-src is closed.
//   font-src            ...and the font files themselves from gstatic.com.
//   img-src https:      Post previews, lead images and social cards point at
//                       whatever host the author's image lives on.
//   connect-src 'self'  The API is same-origin behind Caddy (see
//                       Caddyfile.herald), so nothing else needs reaching.
//
// frame-ancestors repeats X-Frame-Options because the header is the legacy
// spelling and the directive is the one modern browsers honour.
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' data: https://fonts.gstatic.com",
  "img-src 'self' data: https:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

// Applied to every response this server makes, including the 404 and the SPA
// fallback — a header that only covers the happy path is not a policy.
const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Content-Security-Policy": CSP,
};

async function resolveFile(pathname) {
  // normalize() collapses `..` before the prefix check, so a crafted
  // /../../etc/passwd cannot escape ROOT.
  const candidate = join(ROOT, normalize(decodeURIComponent(pathname)));
  if (candidate !== ROOT && !candidate.startsWith(ROOT + sep)) return null;
  try {
    const s = await stat(candidate);
    if (s.isFile()) return candidate;
    if (s.isDirectory()) {
      const index = join(candidate, "index.html");
      if ((await stat(index)).isFile()) return index;
    }
  } catch {
    /* fall through to the SPA fallback */
  }
  return null;
}

const server = createServer(async (req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD" }).end("Method Not Allowed");
    return;
  }

  const pathname = new URL(req.url, "http://localhost").pathname;
  let file = await resolveFile(pathname);
  let status = 200;

  if (!file) {
    // A missing asset is a genuine 404 — only unknown *routes* get the shell,
    // otherwise a typo'd script src silently returns HTML and the console
    // fills with "Unexpected token '<'".
    if (extname(pathname)) {
      res
        .writeHead(404, { "Content-Type": "text/plain; charset=utf-8", ...SECURITY_HEADERS })
        .end("Not Found");
      return;
    }
    file = join(ROOT, "index.html");
    status = 200;
  }

  res.writeHead(status, {
    "Content-Type": TYPES[extname(file).toLowerCase()] ?? "application/octet-stream",
    "Cache-Control": cacheControl(pathname),
    ...SECURITY_HEADERS,
  });
  if (req.method === "HEAD") {
    res.end();
    return;
  }
  createReadStream(file)
    .on("error", () => res.destroy())
    .pipe(res);
});

server.listen(PORT, HOST, () => {
  console.log(`herald-web: serving ${ROOT} on http://${HOST}:${PORT}`);
});
