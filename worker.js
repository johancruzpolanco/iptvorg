/**
 * Proxy HLS para Telecentro 13 (Cloudflare Worker).
 *
 * Telemicro exige cabecera Referer en TODAS las peticiones (playlist y
 * segmentos .ts). IPTV Smarters no manda cabeceras propias: ignora las lineas
 * #EXTVLCOPT de la lista y tampoco entiende el sufijo "|Referer=..." en la
 * URL. Resultado: 403 en Smarters aunque en VLC funcione.
 *
 * Este Worker recibe la peticion sin cabeceras, le anade el Referer y la
 * reenvia a Telemicro. La URL que pones en la lista queda limpia y funciona
 * en cualquier reproductor.
 *
 * No hace falta reescribir el contenido de los .m3u8: las URLs que hay dentro
 * son relativas (chunks.m3u8?nimblesessionid=X, l_13_....ts?...), asi que el
 * reproductor las resuelve contra el propio Worker mientras respetemos la
 * misma estructura de rutas.
 *
 * Uso:  https://<tu-worker>.workers.dev/live/13/playlist.m3u8
 */

// live4, NO live2: live2 reparte entre dos backends, la sesion
// (nimblesessionid) se crea en uno y el segmento se pide al otro -> 403/404.
const ORIGIN = "https://live4.telemicro.com.do";

const REFERER = "https://telemicro.com.do/";
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

// Solo dejamos pasar las rutas de streaming. Sin esto el Worker seria un
// proxy abierto que cualquiera podria usar para lo que quisiera a tu costa.
const ALLOWED_PATH = /^\/live\/[A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+$/;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "Range",
  "Access-Control-Expose-Headers": "Content-Length, Content-Range",
};

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: CORS });
    }

    // Pagina de ayuda, para que abrir la raiz no parezca un error.
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(
        "Proxy HLS Telecentro 13\n\n" +
          "Usa: " + url.origin + "/live/13/playlist.m3u8\n",
        { status: 200, headers: { "Content-Type": "text/plain", ...CORS } }
      );
    }

    if (!ALLOWED_PATH.test(url.pathname)) {
      return new Response("Ruta no permitida", { status: 403, headers: CORS });
    }

    // Conserva la query: el nimblesessionid viaja ahi y sin el da 404.
    const target = ORIGIN + url.pathname + url.search;

    const headers = new Headers({
      Referer: REFERER,
      "User-Agent": USER_AGENT,
      Accept: "*/*",
    });

    // Reenvia Range para que el reproductor pueda pedir trozos de segmento.
    const range = request.headers.get("Range");
    if (range) headers.set("Range", range);

    let upstream;
    try {
      upstream = await fetch(target, {
        method: request.method,
        headers,
        redirect: "follow",
      });
    } catch (err) {
      return new Response("Error contactando el origen: " + err, {
        status: 502,
        headers: CORS,
      });
    }

    const out = new Headers(CORS);
    const ctype = upstream.headers.get("Content-Type");
    if (ctype) out.set("Content-Type", ctype);
    for (const h of ["Content-Length", "Content-Range", "Accept-Ranges"]) {
      const v = upstream.headers.get(h);
      if (v) out.set(h, v);
    }

    // Los playlists de directo caducan en segundos; los segmentos son
    // inmutables y conviene cachearlos para no repetir viajes al origen.
    if (url.pathname.endsWith(".m3u8")) {
      out.set("Cache-Control", "no-cache, no-store, must-revalidate");
    } else {
      out.set("Cache-Control", "public, max-age=60");
    }

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: out,
    });
  },
};
