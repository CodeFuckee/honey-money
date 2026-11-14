// Zero-dependency static file server for ZCode Sites packages.
// Usage: node static-server.mjs <site-dir>
// Env: PORT (default 3000), SPA_FALLBACK=1 to answer unknown extensionless paths with index.html.
// Listens on 0.0.0.0 so the runtime health check can reach it.
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { gzipSync } from "node:zlib";
import { extname, join, normalize, resolve, sep } from "node:path";

const root = resolve(process.argv[2] ?? "dist");
const port = Number(process.env.PORT ?? 3000);
const spaFallback = process.env.SPA_FALLBACK === "1";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".htm": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json",
  ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".otf": "font/otf",
  ".wasm": "application/wasm",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".pdf": "application/pdf",
};

async function fileAt(path) {
  try {
    const info = await stat(path);
    return info.isFile() ? { path, size: info.size } : undefined;
  } catch {
    return undefined;
  }
}

// /about → about, about.html, about/index.html (covers static exports of Next, Astro, Hugo…).
async function resolveFile(urlPath) {
  const rel = normalize(decodeURIComponent(urlPath)).replace(/^([/\\])+/, "");
  const base = join(root, rel);
  if (base !== root && !base.startsWith(root + sep)) return undefined;
  return (
    (await fileAt(base)) ??
    (await fileAt(`${base}.html`)) ??
    (await fileAt(join(base, "index.html")))
  );
}

function send(req, res, status, file) {
  const type = MIME[extname(file.path).toLowerCase()] ?? "application/octet-stream";
  // Only the hashed-asset directories of common bundlers are cached forever (Vite/Rollup `assets/`,
  // Astro `_astro/`, Next `_next/static/`, CRA `static/{js,css,media}/`). Everything else, HTML
  // in particular, is revalidated so a redeploy shows up immediately.
  const rel = file.path.slice(root.length).split(sep).join("/");
  const immutable =
    !type.startsWith("text/html") &&
    /^\/(assets|_astro|_next\/static|static\/(js|css|media))\//.test(rel);
  res.writeHead(status, {
    "Content-Type": type,
    "Content-Length": file.size,
    "Cache-Control": immutable ? "public, max-age=31536000, immutable" : "no-cache",
  });
  if (req.method === "HEAD") return res.end();
  createReadStream(file.path).pipe(res);
}

// 数据透传（可选）：DATA_PROXY_TARGET 配置后，/data/* 实时反代到上游最新
// 快照（如 GitHub Pages），本站数据随上游日更而无需重新部署；上游不可达
// 时回退本地打包快照。200 响应缓存 5 分钟，非 200 不缓存。
const DATA_PROXY = process.env.DATA_PROXY_TARGET?.replace(/\/$/, "");
const dataCache = new Map();   // url → { at, status, type, body }

const FRESH_MS = 60 * 60 * 1000;   // 缓存 1 小时：数据每日一更，长缓存换首屏速度
const CACHE_CONTROL = "public, max-age=600";   // 浏览器端 10 分钟内直接复用

async function fetchUpstream(target) {
  // 回源可能很慢（阿里云→GitHub 国际线路实测 ~60KB/s，大文件需分钟级）：
  // 超时放宽到 60s + 失败重试一次；仍失败由调用方回退本地快照
  let upstream;
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      upstream = await fetch(target, { redirect: "follow",
        signal: AbortSignal.timeout(60000) });
      break;
    } catch (err) {
      if (attempt === 2) throw err;
      console.warn(`[data-proxy] 上游第 ${attempt} 次拉取失败，重试:`,
        String(err).slice(0, 120));
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
  const body = Buffer.from(await upstream.arrayBuffer());
  const type = upstream.headers.get("content-type") ?? "application/json";
  return { status: upstream.status, type, body };
}

function serveGz(req, res, status, type, body, source) {
  const gz = gzipSync(body);
  res.writeHead(status, {
    "Content-Type": type,
    "Content-Encoding": "gzip",
    "Content-Length": gz.length,
    "Cache-Control": CACHE_CONTROL,
    "X-Data-Source": source,
  });
  if (req.method === "HEAD") return res.end();
  res.end(gz);
}

async function proxyData(req, res, pathname) {
  const target = `${DATA_PROXY}${pathname}`;
  const hit = dataCache.get(target);
  if (hit && Date.now() - hit.at < FRESH_MS) {
    serveGz(req, res, hit.status, hit.type, hit.body, "proxy-cache");
    if (req.method === "HEAD") return res.end();
    return;
  }
  if (hit) {
    // 过期：先回旧值秒开，后台静默刷新（数据日更，1h 内旧值无感知）
    serveGz(req, res, hit.status, hit.type, hit.body, "proxy-cache");
    if (!hit.refreshing) {
      hit.refreshing = true;
      fetchUpstream(target).then((r) => {
        if (r.status === 200) {
          if (dataCache.size > 8000) dataCache.clear();
          dataCache.set(target, { at: Date.now(), ...r });
        }
      }).catch(() => {}).finally(() => { hit.refreshing = false; });
    }
    if (req.method === "HEAD") return res.end();
    return;
  }
  try {
    const r = await fetchUpstream(target);
    if (r.status === 200) {
      if (dataCache.size > 8000) dataCache.clear();
      dataCache.set(target, { at: Date.now(), ...r });
    }
    serveGz(req, res, r.status, r.type, r.body, "proxy-upstream");
  } catch {
    const file = await resolveFile(pathname);
    if (file) return send(req, res, 200, file);
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" })
       .end("data upstream unavailable");
  }
}

createServer(async (req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD" }).end();
    return;
  }
  let pathname;
  try {
    pathname = new URL(req.url ?? "/", "http://localhost").pathname;
    decodeURIComponent(pathname);
  } catch {
    res.writeHead(400).end();
    return;
  }
  if (DATA_PROXY && pathname.startsWith("/data/")) {
    return proxyData(req, res, pathname);
  }
  const file = await resolveFile(pathname);
  if (file) return send(req, res, 200, file);
  if (spaFallback && !extname(pathname)) {
    const index = await fileAt(join(root, "index.html"));
    if (index) return send(req, res, 200, index);
  }
  const notFound = await fileAt(join(root, "404.html"));
  if (notFound) return send(req, res, 404, notFound);
  res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }).end("Not Found");
}).listen(port, "0.0.0.0", () => {
  console.log(
    `static-server: serving ${root} on 0.0.0.0:${port}${spaFallback ? " (SPA fallback)" : ""}`,
  );
});
