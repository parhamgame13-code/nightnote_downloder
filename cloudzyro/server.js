/**
 * پروکسی + سروکننده‌ی رابط وب NightNote برای Cloudzyro.
 *
 * Cloudzyro فقط سایت استاتیک و Node.js اجرا می‌کنه و پایتون/yt-dlp/بات
 * روش کار نمی‌کنه؛ برای همین این سرور فقط دو کار می‌کنه (بدون هیچ
 * وابستگی خارجی — فقط ماژول‌های داخلی Node):
 *  ۱) فایل‌های public/ (رابط وب) رو به‌صورت استاتیک سرو می‌کنه.
 *  ۲) درخواست‌های /api/* و /dl/* رو به بک‌اند پایتون (API_BASE_URL)
 *     پروکسی می‌کنه، با حفظ هدرهای Range و Content-Disposition و
 *     استریم کردن بدنه (برای دانلود فایل‌های بزرگ).
 *
 * متغیرهای محیطی:
 *   API_BASE_URL  (اجباری) آدرس بک‌اند، مثلاً https://nightnote-downloader.onrender.com
 *   PORT          (اختیاری، پیش‌فرض 3000 — Cloudzyro خودش PORT رو ست می‌کنه)
 */
"use strict";

const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");

const PORT = parseInt(process.env.PORT || "3000", 10);
const API_BASE_URL = (process.env.API_BASE_URL || "").replace(/\/+$/, "");
const PUBLIC_DIR = path.join(__dirname, "public");

if (!API_BASE_URL) {
  console.error(
    "API_BASE_URL تنظیم نشده. اول بک‌اند پایتون رو روی Render/Railway/Fly/VPS دپلوی کن، " +
      "بعد آدرسش رو تو متغیرهای محیطی این سایت (API_BASE_URL) بذار. راهنما: cloudzyro/README.md"
  );
  process.exit(1);
}

let apiTarget;
try {
  apiTarget = new URL(API_BASE_URL);
} catch (e) {
  console.error("API_BASE_URL معتبر نیست:", API_BASE_URL);
  process.exit(1);
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

function serveStatic(req, res) {
  let urlPath = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (urlPath === "/") urlPath = "/index.html";
  // جلوگیری از path traversal
  const safePath = path.normalize(path.join(PUBLIC_DIR, urlPath));
  if (!safePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(400, { "Content-Type": "text/plain" });
    res.end("Bad request");
    return;
  }
  fs.stat(safePath, (err, stats) => {
    if (err || !stats.isFile()) {
      // SPA fallback: هر مسیر ناشناخته‌ای به index.html بره
      const indexPath = path.join(PUBLIC_DIR, "index.html");
      fs.readFile(indexPath, (readErr, data) => {
        if (readErr) {
          res.writeHead(404, { "Content-Type": "text/plain" });
          res.end("Not found");
          return;
        }
        res.writeHead(200, { "Content-Type": MIME[".html"] });
        res.end(data);
      });
      return;
    }
    const ext = path.extname(safePath).toLowerCase();
    res.writeHead(200, {
      "Content-Type": MIME[ext] || "application/octet-stream",
      "Content-Length": stats.size,
      "Cache-Control": ext === ".html" || ext === ".js" ? "no-cache" : "public, max-age=3600",
    });
    fs.createReadStream(safePath).pipe(res);
  });
}

function proxyToBackend(req, res) {
  const isHttps = apiTarget.protocol === "https:";
  const lib = isHttps ? https : http;
  // هدرها رو عیناً بفرست (مخصوصاً Range و Cookie)، فقط Host رو عوض کن
  const headers = { ...req.headers };
  delete headers.host;
  delete headers.connection;
  const options = {
    protocol: apiTarget.protocol,
    hostname: apiTarget.hostname,
    port: apiTarget.port || (isHttps ? 443 : 80),
    path: apiTarget.pathname.replace(/\/+$/, "") + req.url,
    method: req.method,
    headers,
  };
  const proxyReq = lib.request(options, (proxyRes) => {
    // هدرهای پاسخ (Content-Disposition، Content-Range، Set-Cookie و ...)
    // عیناً برگردونده می‌شن تا دانلود/رزومه و سشن‌ها کار کنن
    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res);
  });
  proxyReq.on("error", (err) => {
    console.error("خطای پروکسی به بک‌اند:", err.message);
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ detail: "بک‌اند در دسترس نیست. چند لحظه دیگر دوباره امتحان کن." }));
    } else {
      res.end();
    }
  });
  // بدنه‌ی درخواست (آپلود فایل و ...) استریم می‌شه، نه یک‌جا تو حافظه
  req.pipe(proxyReq);
}

const server = http.createServer((req, res) => {
  const urlPath = new URL(req.url, "http://x").pathname;
  if (urlPath === "/healthz") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("ok");
    return;
  }
  if (urlPath === "/api" || urlPath.startsWith("/api/") || urlPath === "/dl" || urlPath.startsWith("/dl/") || urlPath === "/docs" || urlPath.startsWith("/docs")) {
    proxyToBackend(req, res);
    return;
  }
  serveStatic(req, res);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`رابط وب NightNote روی پورت ${PORT} بالا آمد؛ بک‌اند: ${API_BASE_URL}`);
});
