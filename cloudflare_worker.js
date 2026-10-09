const CACHE_SECONDS = 86400;
const FETCH_TIMEOUT_MS = 15000;
const IMAGE_KEYS = {
  "/2hV7gTP.png": "gembox-top-right.png",
  "/yqbcCrb.png": "gembox-top-left.png",
  "/qju8XK9.png": "gembox-bottom-right.png",
  "/vVCWo8l.png": "gembox-bottom-left.png",
  "/Ft9zPsM.png": "goblin-punched.png",
};
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Max-Age": "86400",
};

function failure(message, status) {
  console.error(message);
  return new Response(message, { status, headers: { ...CORS_HEADERS, "Cache-Control": "no-store" } });
}

export default {
  async fetch(request, env = {}) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS_HEADERS });
    if (!["GET", "HEAD"].includes(request.method)) return failure("Method not allowed", 405);
    const raw = new URL(request.url).searchParams.get("url");
    if (!raw) return failure("Missing url parameter", 400);
    let target;
    try {
      target = new URL(raw);
      if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) throw new Error("Invalid image URL");
      if (target.host === new URL(request.url).host) throw new Error("Recursive proxy URL");
    } catch {
      return failure("Invalid image URL", 400);
    }
    const host = target.hostname.toLowerCase();
    if (host === "static.wikia.nocookie.net" || host.endsWith(".wikia.nocookie.net")) {
      target.pathname = target.pathname.split("/revision/")[0].replace(/\/$/, "") + "/revision/latest/scale-to-width-down/100";
      target.search = "";
    }
    try {
      const key = host === "i.imgur.com" ? IMAGE_KEYS[target.pathname] : null;
      if (key && env.GEMBOX_IMAGES) {
        const object = await env.GEMBOX_IMAGES.get(key);
        if (object) {
          const headers = new Headers(CORS_HEADERS);
          object.writeHttpMetadata(headers);
          headers.set("Content-Type", "image/png");
          headers.set("ETag", object.httpEtag);
          headers.set("Cache-Control", `public, max-age=${CACHE_SECONDS}`);
          return new Response(request.method === "HEAD" ? null : object.body, { headers });
        }
        console.warn(`Missing R2 image: ${key}`);
      }
      console.log(`Fetching image: ${target.href}`);
      const upstream = await fetch(target.href, {
        headers: { "Accept": "image/*", "User-Agent": "Chester-Image-Proxy/1.0" },
        redirect: "follow",
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!upstream.ok) return failure(`Image source ${host} returned HTTP ${upstream.status} for ${target.href}`, upstream.status);
      const contentType = upstream.headers.get("content-type") || "";
      if (!contentType.toLowerCase().startsWith("image/")) return failure(`Image source returned ${contentType || "no content type"} for ${target.href}`, 415);
      return new Response(request.method === "HEAD" ? null : upstream.body, {
        headers: { ...CORS_HEADERS, "Content-Type": contentType, "Cache-Control": `public, max-age=${CACHE_SECONDS}` },
      });
    } catch (error) {
      return failure(`Image fetch failed for ${target.href}: ${error.message}`, 502);
    }
  },
};
