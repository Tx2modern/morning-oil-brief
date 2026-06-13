/**
 * MOB Maintenance Gate — Vercel Edge Middleware
 *
 * Public visitors → maintenance.html
 * Admin with secret cookie or ?unlock=<SECRET> → live dashboard
 *
 * To unlock: visit  https://www.morningoilbrief.com/?unlock=MOB_BARREL_7x9k2p
 * To re-lock: visit https://www.morningoilbrief.com/?lock=true
 */

const SECRET      = "MOB_BARREL_7x9k2p";   // ← change this if you ever want a new key
const COOKIE_NAME = "mob_admin";
const COOKIE_TTL  = 60 * 60 * 24 * 30;     // 30 days

export default async function middleware(request) {
  const url    = new URL(request.url);
  const params = url.searchParams;
  const path   = url.pathname;

  // ── 1. Always pass through: maintenance page, Vercel internals, static assets ──
  if (
    path === "/maintenance.html" ||
    path.startsWith("/_vercel") ||
    path.startsWith("/favicon") ||
    path.match(/\.(ico|png|jpg|svg|webp|woff2?|css|js)$/)
  ) {
    return; // pass through — undefined means "serve normally"
  }

  // ── 2. Handle ?lock=true  (clears the bypass cookie) ──
  if (params.get("lock") === "true") {
    const headers = new Headers({ Location: "/" });
    headers.append(
      "Set-Cookie",
      `${COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`
    );
    return new Response(null, { status: 302, headers });
  }

  // ── 3. Handle ?unlock=SECRET  (sets the bypass cookie) ──
  if (params.get("unlock") === SECRET) {
    params.delete("unlock");
    const dest = path + (params.toString() ? "?" + params.toString() : "");
    const headers = new Headers({ Location: dest });
    headers.append(
      "Set-Cookie",
      `${COOKIE_NAME}=${SECRET}; Path=/; Max-Age=${COOKIE_TTL}; HttpOnly; Secure; SameSite=Lax`
    );
    return new Response(null, { status: 302, headers });
  }

  // ── 4. Check for valid bypass cookie ──
  const cookieHeader = request.headers.get("Cookie") || "";
  const cookies = Object.fromEntries(
    cookieHeader.split(";").map((c) => {
      const [k, ...v] = c.trim().split("=");
      return [k.trim(), v.join("=").trim()];
    })
  );

  if (cookies[COOKIE_NAME] === SECRET) {
    return; // Admin — pass through to live site
  }

  // ── 5. Everyone else → fetch and serve maintenance page ──
  const maintenanceUrl = new URL("/maintenance.html", request.url);
  const page = await fetch(maintenanceUrl.toString());
  return new Response(page.body, {
    status: 503,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Retry-After": "3600",
      "Cache-Control": "no-store",
    },
  });
}

export const config = {
  matcher: ["/((?!_vercel|favicon).*)"],
};
