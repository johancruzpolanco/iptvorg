/**
 * Proxy HLS para IPTV (Cloudflare Worker).
 *
 * ---------------------------------------------------------------------------
 * TELEMICRO (Telecentro 13, Telemicro 5, Digital 15)
 * ---------------------------------------------------------------------------
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
 *
 * ---------------------------------------------------------------------------
 * TELEANTILLAS
 * ---------------------------------------------------------------------------
 *
 * teleantillas.com.do emite con un embed de Dailymotion. El enlace HLS de
 * Dailymotion lleva un token sec2(...) que caduca, asi que no se puede fijar
 * en la lista: el Worker lo saca en cada momento haciendo lo mismo que el
 * reproductor de la pagina:
 *
 *   1. Lee la pagina y saca el id del video del embed de Dailymotion.
 *   2. Pide el metadata del video con Referer de teleantillas.com.do. Sin el,
 *      Dailymotion contesta DM016 ("Teleantillas ha limitado su acceso").
 *   3. Pide el master con User-Agent de navegador. Sin el, 403 (E005).
 *
 * Las variantes (480p, 240p) se sirven desde el Worker para poder renovar el
 * token sin que el reproductor se entere. Los segmentos .ts van directos a
 * dmcdn.net, que no pide cabeceras, y no gastan peticiones del Worker.
 *
 * Si Dailymotion no da la senal, /teleantillas/playlist.m3u8 devuelve un
 * master con el enlace de tvabierta para que el canal no se quede en negro.
 * /teleantillas/estado dice cual de los dos se esta usando y por que.
 *
 * Uso:  https://<tu-worker>.workers.dev/teleantillas/playlist.m3u8
 */

// live4, NO live2: live2 reparte entre dos backends, la sesion
// (nimblesessionid) se crea en uno y el segmento se pide al otro -> 403/404.
const ORIGIN = "https://live4.telemicro.com.do";

const REFERER = "https://telemicro.com.do/";

// Fuente de la lista. Se usa raw (no jsDelivr) porque su cache es de 5 minutos
// en vez de 7 dias; lo unico que le falta es el Content-Type, que ponemos aqui.
const LISTA_RAW =
  "https://raw.githubusercontent.com/johancruzpolanco/iptvorg/main/lista.m3u";
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

// Solo dejamos pasar las rutas de streaming. Sin esto el Worker seria un
// proxy abierto que cualquiera podria usar para lo que quisiera a tu costa.
const ALLOWED_PATH = /^\/live\/[A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+$/;

// --- Teleantillas ---

const TA_PAGE = "https://teleantillas.com.do/";

// Id del embed a dia de hoy. Solo se usa si la pagina no responde o cambia de
// estructura y no aparece el id en ella.
const TA_FALLBACK_VIDEO = "x8mwmvs";

// Respaldo si Dailymotion no da la senal: el mismo enlace que da tvabierta.
const TA_FALLBACK_URL = "https://hls.tvabierta.net/hls/010.m3u8";

// El token dura horas; pidiendo un master nuevo cada 10 minutos nunca llega a
// caducar en mitad de una sesion. La pagina cambia mucho menos.
const TA_PAGE_TTL_MS = 60 * 60 * 1000;
const TA_MASTER_TTL_MS = 10 * 60 * 1000;

// Dailymotion devuelve 403 (E005) de vez en cuando a peticiones correctas; con
// un token nuevo suele pasar al siguiente intento.
const TA_ATTEMPTS = 3;

// Tras un fallo no se vuelve a preguntar a Dailymotion durante este tiempo:
// con varios reproductores abiertos cada peticion repetiria los 3 intentos.
const TA_FAILURE_TTL_MS = 60 * 1000;

// false: los .ts van directos a Dailymotion.
// true:  tambien pasan por el Worker. Solo hace falta si el canal aparece en
//        Smarters pero el video no arranca.
const TA_PROXY_SEGMENTS = false;

// Con TA_PROXY_SEGMENTS solo se reenvian segmentos de Dailymotion.
const TA_SEGMENT_HOST = /(^|\.)dmcdn\.net$/;

// /teleantillas/playlist.m3u8 (master) y /teleantillas/v480.m3u8 (variantes).
const TA_ROUTE = /^\/teleantillas\/([A-Za-z0-9_-]+)\.m3u8$/;

// Cache en memoria del isolate. Si Cloudflare lo recicla se vuelve a resolver.
let taVideo = { id: null, at: 0 };
let taMaster = null;
let taFailure = null;

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
        "Proxy IPTV\n\n" +
          "Lista:  " + url.origin + "/lista.m3u\n" +
          "Canal:  " + url.origin + "/live/13/playlist.m3u8\n" +
          "Teleantillas:  " + url.origin + "/teleantillas/playlist.m3u8" +
          "  (estado: " + url.origin + "/teleantillas/estado)\n",
        { status: 200, headers: { "Content-Type": "text/plain", ...CORS } }
      );
    }

    // La lista, servida con el Content-Type correcto.
    //
    // raw.githubusercontent.com la entrega como "text/plain", y con eso IPTV
    // Smarters no la reconoce como playlist (el navegador tampoco: la pinta en
    // pantalla en vez de descargarla). jsDelivr si usa "audio/x-mpegurl", pero
    // cachea hasta 7 dias en el cliente y las purgas acaban en "Throttled".
    // Aqui se coge siempre de raw (la fuente, sin cache largo) y se reetiqueta.
    if (url.pathname === "/lista.m3u") {
      let r;
      try {
        r = await fetch(LISTA_RAW, {
          headers: { "User-Agent": USER_AGENT, Accept: "*/*" },
          cf: { cacheTtl: 300, cacheEverything: true },
        });
      } catch (err) {
        return new Response("Error obteniendo la lista: " + err, {
          status: 502,
          headers: CORS,
        });
      }
      if (!r.ok) {
        return new Response("La lista devolvio HTTP " + r.status, {
          status: 502,
          headers: CORS,
        });
      }
      return new Response(r.body, {
        status: 200,
        headers: {
          "Content-Type": "audio/x-mpegurl",
          "Cache-Control": "public, max-age=300",
          ...CORS,
        },
      });
    }

    if (url.pathname === "/teleantillas/estado") {
      return taStatus();
    }

    const ta = url.pathname.match(TA_ROUTE);
    if (ta) {
      try {
        return await teleantillas(ta[1]);
      } catch (err) {
        // Sin master de Dailymotion no hay variantes que servir: al que pide
        // el master se le da uno con tvabierta como unica variante. No un 302:
        // tras una redireccion hay reproductores (y el --check de
        // build_list.py) que resuelven los .ts relativos contra el Worker.
        if (ta[1] === "playlist") {
          return playlistResponse(
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1500000\n" + TA_FALLBACK_URL + "\n"
          );
        }
        return new Response("Teleantillas: " + err.message, {
          status: 502,
          headers: CORS,
        });
      }
    }

    if (url.pathname === "/teleantillas/seg") {
      let target;
      try {
        target = new URL(url.searchParams.get("u") || "");
      } catch (err) {
        return new Response("Segmento invalido", { status: 400, headers: CORS });
      }
      if (
        !TA_PROXY_SEGMENTS ||
        target.protocol !== "https:" ||
        !TA_SEGMENT_HOST.test(target.hostname)
      ) {
        return new Response("Ruta no permitida", { status: 403, headers: CORS });
      }
      return proxy(request, target.href, {});
    }

    if (!ALLOWED_PATH.test(url.pathname)) {
      return new Response("Ruta no permitida", { status: 403, headers: CORS });
    }

    // Conserva la query: el nimblesessionid viaja ahi y sin el da 404.
    return proxy(request, ORIGIN + url.pathname + url.search, {
      Referer: REFERER,
    });
  },
};

/** Reenvia la peticion al origen con las cabeceras dadas y devuelve el cuerpo tal cual. */
async function proxy(request, target, extraHeaders) {
  const headers = new Headers({
    "User-Agent": USER_AGENT,
    Accept: "*/*",
    ...extraHeaders,
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
  if (new URL(target).pathname.endsWith(".m3u8")) {
    out.set("Cache-Control", "no-cache, no-store, must-revalidate");
  } else {
    out.set("Cache-Control", "public, max-age=60");
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: out,
  });
}

// ---------------------------------------------------------------------------
// Teleantillas
// ---------------------------------------------------------------------------

async function teleantillas(name) {
  if (name === "playlist") {
    return playlistResponse((await taResolveMaster(false)).playlist);
  }

  // Si la variante falla (token caducado, cambio de servidor...) se pide un
  // master nuevo y se reintenta una vez.
  let status = 404;
  for (const force of [false, true]) {
    const src = (await taResolveMaster(force)).variants[name];
    if (!src) break;

    const r = await fetch(src, {
      headers: { "User-Agent": USER_AGENT, Accept: "*/*" },
    });
    if (!r.ok) {
      status = r.status;
      continue;
    }

    // Los segmentos vienen relativos a dmcdn.net: se pasan a absolutos para
    // que el reproductor no los pida al Worker.
    const base = r.url || src;
    const seg = (u) => {
      const abs = new URL(u, base).href;
      return TA_PROXY_SEGMENTS ? "seg?u=" + encodeURIComponent(abs) : abs;
    };
    const body = (await r.text())
      .split(/\r?\n/)
      .map((line) => {
        if (!line) return line;
        if (line.startsWith("#")) {
          return line.replace(/URI="([^"]+)"/, (_, u) => 'URI="' + seg(u) + '"');
        }
        return seg(line);
      })
      .join("\n");
    return playlistResponse(body);
  }

  return new Response("Variante no disponible (HTTP " + status + ")", {
    status: status === 404 ? 404 : 502,
    headers: CORS,
  });
}

/** Texto para comprobar a mano, tras desplegar, de donde sale la senal. */
async function taStatus() {
  let body;
  try {
    const m = await taResolveMaster(false);
    body =
      "OK: la senal sale de teleantillas.com.do (Dailymotion " + m.id + ")\n" +
      "Variantes: " + Object.keys(m.variants).join(", ") + "\n" +
      "Master pedido hace " + Math.round((Date.now() - m.at) / 1000) + " s\n";
  } catch (err) {
    body =
      "FALLO: " + err.message + "\n" +
      "Mientras tanto los reproductores van al respaldo: " + TA_FALLBACK_URL + "\n";
  }
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", ...CORS },
  });
}

/** Id del video de Dailymotion que tiene puesto la pagina de Teleantillas. */
async function taVideoId() {
  if (taVideo.id && Date.now() - taVideo.at < TA_PAGE_TTL_MS) return taVideo.id;
  try {
    const r = await fetch(TA_PAGE, {
      headers: { "User-Agent": USER_AGENT, Accept: "text/html" },
    });
    const m =
      r.ok &&
      (await r.text()).match(/dailymotion\.com\/(?:embed\/)?video\/([A-Za-z0-9]+)/);
    if (m) {
      taVideo = { id: m[1], at: Date.now() };
      return m[1];
    }
  } catch (err) {
    // Pagina caida: seguimos con el ultimo id conocido.
  }
  return taVideo.id || TA_FALLBACK_VIDEO;
}

/** Metadata + master de Dailymotion para un video. Devuelve { text, base }. */
async function taFetchMaster(id) {
  const meta = await fetch(
    "https://www.dailymotion.com/player/metadata/video/" + id +
      "?embedder=" + encodeURIComponent(TA_PAGE),
    {
      headers: {
        Referer: TA_PAGE,
        "User-Agent": USER_AGENT,
        Accept: "application/json",
      },
    }
  );
  const data = await meta.json().catch(() => null);
  if (!data) throw new Error("metadata de Dailymotion: HTTP " + meta.status);
  if (data.error) {
    throw new Error(
      "Dailymotion " + data.error.code + ": " +
        (data.error.raw_message || data.error.title)
    );
  }
  const hls = ((data.qualities && data.qualities.auto) || []).find((q) =>
    /mpegurl/i.test(q.type || "")
  );
  if (!hls) throw new Error("Dailymotion no da enlace HLS (fuera del aire?)");

  const r = await fetch(hls.url, {
    headers: { "User-Agent": USER_AGENT, Accept: "*/*" },
  });
  const text = await r.text();
  if (!r.ok || !text.includes("#EXTM3U")) {
    throw new Error(
      "master de Dailymotion: HTTP " + r.status +
        (r.headers.get("X-Error-Code") ? " " + r.headers.get("X-Error-Code") : "")
    );
  }
  return { text, base: r.url || hls.url };
}

/**
 * Pide a Dailymotion un master nuevo (o devuelve el de cache) y lo reescribe
 * para que cada variante apunte al Worker: v480.m3u8, v240.m3u8...
 *
 * Devuelve { id, at, playlist, variants: { v480: "https://...dmcdn.net/..." } }.
 */
async function taResolveMaster(force) {
  const now = Date.now();
  if (!force && taMaster && now - taMaster.at < TA_MASTER_TTL_MS) {
    return taMaster;
  }
  if (taFailure && now - taFailure.at < TA_FAILURE_TTL_MS) {
    throw taFailure.error;
  }

  try {
    const id = await taVideoId();
    let master = null;
    let lastError = null;
    for (let i = 0; i < TA_ATTEMPTS && !master; i++) {
      try {
        master = await taFetchMaster(id);
      } catch (err) {
        lastError = err;
      }
    }
    if (!master) throw lastError;

    const variants = {};
    const lines = [];
    let pending = null;
    for (const line of master.text.split(/\r?\n/)) {
      if (line.startsWith("#EXT-X-STREAM-INF")) {
        const m = line.match(/NAME="([A-Za-z0-9_-]+)"/);
        const n = Object.keys(variants).length;
        pending = "v" + (m ? m[1] : n);
        if (variants[pending]) pending += "_" + n;
        lines.push(line);
      } else if (pending && line && !line.startsWith("#")) {
        variants[pending] = new URL(line, master.base).href;
        lines.push(pending + ".m3u8"); // relativa: se resuelve contra el Worker
        pending = null;
      } else {
        lines.push(line);
      }
    }
    if (!Object.keys(variants).length) {
      throw new Error("master de Dailymotion sin variantes");
    }

    taMaster = { id, at: Date.now(), playlist: lines.join("\n"), variants };
    taFailure = null;
    return taMaster;
  } catch (err) {
    taFailure = { at: Date.now(), error: err };
    throw err;
  }
}

function playlistResponse(body) {
  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "application/vnd.apple.mpegurl",
      "Cache-Control": "no-cache, no-store, must-revalidate",
      ...CORS,
    },
  });
}
