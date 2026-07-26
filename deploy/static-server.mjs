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
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }).end("Not Found");
      return;
    }
    file = join(ROOT, "index.html");
    status = 200;
  }

  res.writeHead(status, {
    "Content-Type": TYPES[extname(file).toLowerCase()] ?? "application/octet-stream",
    "Cache-Control": cacheControl(pathname),
    "X-Content-Type-Options": "nosniff",
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
