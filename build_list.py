#!/usr/bin/env python3
"""
Genera lista.m3u para IPTV Smarters:

  1. Grupo DOMINICANOS: los canales de la categoria RD de la API de tvabierta,
     ordenados por numero de canal, mas los enlaces propios que sustituyen a
     los de la API cuando tenemos uno mejor (los de Telemicro, que van por
     nuestro proxy).
  2. Detras, la lista en espanol de iptv-org, sin los canales que ya salen
     arriba para que no haya duplicados.

    python build_list.py                  genera lista.m3u
    python build_list.py --check          verifica cada enlace (video real)
    python build_list.py --check --drop-broken   omite los que fallen
    python build_list.py --no-base        solo el grupo DOMINICANOS

Codigos de salida:  0 ok | 1 error irrecuperable | 2 --strict con fallos

NOTAS DE MANTENIMIENTO
  - Los canales de Telemicro (Telecentro, Telemicro 5, Digital 15) pasan por un
    Cloudflare Worker (worker.js). El servidor exige cabecera Referer en el
    playlist Y en cada segmento, e IPTV Smarters no manda cabeceras propias:
    ignora los #EXTVLCOPT y no entiende el sufijo "|Referer=..." de la URL.
  - Usar live4.telemicro.com.do, NO live2: live2 reparte entre dos backends, la
    sesion (nimblesessionid) se crea en uno y el segmento se pide al otro, lo
    que da 403/404 intermitentes.
  - NO fijar enlaces de dmcdn.net (Dailymotion): llevan un token sec2(...) que
    caduca en horas. Tampoco fijar los /memfs/<uuid> de tvabierta: son ids de
    proceso que cambian si el canal reinicia. Por eso se resuelven por API.
  - Verificar solo el playlist no sirve: devuelve 200 aunque los segmentos
    fallen. Y hay que pedir el ULTIMO segmento, no el primero: la playlist de
    un directo es una ventana deslizante y el mas antiguo puede haber expirado.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

GROUP = "DOMINICANOS"
DEFAULT_OUTPUT = "lista.m3u"

SOURCE_URL = "https://iptv-org.github.io/iptv/languages/spa.m3u"
TVABIERTA_API = "https://tvabierta.net/api/tv/channels.json"
TVABIERTA_CATEGORY = "RD"

# Va en el codigo a proposito: cuando dependia de una variable del repo, la
# lista se publicaba sin proxy y Telecentro no cargaba en Smarters.
DEFAULT_PROXY = "https://tc13.johanecruzpolanco.workers.dev"
PROXY_BASE = os.environ.get("PROXY_BASE", DEFAULT_PROXY).rstrip("/")

# Canales propios: sustituyen al de la API con el mismo "api_name" porque
# tenemos un enlace mejor. El resto de la categoria RD se importa tal cual.
CHANNELS = [
    {
        "name": "Telecentro 13",
        "number": 13,
        "api_name": "telecentro",
        "url": "https://live4.telemicro.com.do/live/telecentrocast_1080p/playlist.m3u8",
        "proxy_path": "/live/telecentrocast_1080p/playlist.m3u8",
        "logo": "https://i.imgur.com/F17zNXh.png",
    },
    {
        "name": "Telemicro 5",
        "number": 5,
        "api_name": "telemicro",
        "url": "https://live4.telemicro.com.do/live/55/playlist.m3u8",
        "proxy_path": "/live/55/playlist.m3u8",
        "logo": "https://i.imgur.com/WhgySAk.png",
    },
    {
        "name": "Digital 15",
        "number": 15,
        "api_name": "digital15",
        "url": "https://live4.telemicro.com.do/live/digital15cast_1080p/playlist.m3u8",
        "proxy_path": "/live/digital15cast_1080p/playlist.m3u8",
        "logo": "https://i.imgur.com/v3mkmZa.png",
    },
    {
        "name": "Teleuniverso 29",
        "number": 29,
        "api_name": "teleuniversotv",
        # El master de wind.do anuncia 1080/720/640 pero solo existe la de 720;
        # las otras dan 404 y el reproductor puede colgarse con la que no esta.
        "url": "https://cdn3.wind.do/streams/teleuniverso/teleuniverso_720.m3u8",
        "logo": "",
    },
]

# tvg-id (sin sufijo @SD/@HD) a eliminar de la lista de iptv-org por estar ya
# en el grupo DOMINICANOS. iptv-org cambio el formato una vez ("Telecentro.do"
# paso a "Telecentro.do@SD"), por eso se compara normalizado.
OWN_IDS = {
    "telecentro.do", "telesistema11.do", "telemicro.do", "digital15.do",
    "colorvision.do", "teleantillas.do", "antena7.do", "rnn.do", "cdn.do",
    "telefuturo.do", "teleunion.do", "acentotv.do", "telemax.do", "tvo.do",
    "retv.do", "boreal.do", "televida.do", "cieltv.do", "ahoratv.do",
}

ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(("::warning::" if ON_CI else "AVISO: ") + msg, flush=True)


def error(msg):
    print(("::error::" if ON_CI else "ERROR: ") + msg, flush=True)


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        warn("no se pudo escribir el summary: %s" % e)


def http_get(url, timeout=30, retries=3):
    """GET con User-Agent de navegador y reintentos con backoff."""
    last = None
    for intento in range(retries):
        if intento:
            time.sleep(1.5 * intento)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (401, 403, 404, 410):
                break
        except Exception as e:  # noqa: BLE001 - red: timeouts, DNS, TLS...
            last = str(e)
    raise RuntimeError(last or "fallo desconocido")


# ----------------------------------------------------------------------
# Canales
# ----------------------------------------------------------------------


def bonito(nombre):
    """'colorvision' -> 'Colorvision'; respeta los que ya vienen con mayusculas."""
    nombre = (nombre or "").strip()
    return nombre if nombre[:1].isupper() else nombre.capitalize()


def cargar_canales():
    """
    Devuelve la lista final del grupo DOMINICANOS: los canales propios mas los
    de la categoria RD de la API, ordenados por numero de canal.

    Si la API no responde se sigue adelante solo con los propios: preferimos una
    lista corta a no generar nada.
    """
    propios = {c["api_name"] for c in CHANNELS if c.get("api_name")}
    canales = list(CHANNELS)

    try:
        data = json.loads(http_get(TVABIERTA_API, timeout=30).decode("utf-8", "replace"))
    except (RuntimeError, ValueError) as e:
        warn("no se pudo leer la API de tvabierta (%s); solo van los canales "
             "propios" % e)
        return sorted(canales, key=lambda c: c.get("number") or 999)

    importados = 0
    for c in data.get("channels", []):
        if c.get("category") != TVABIERTA_CATEGORY or not c.get("enabled", True):
            continue
        nombre = (c.get("name") or "").strip()
        stream = (c.get("stream") or "").strip()
        if not nombre or not stream:
            continue
        if nombre.lower() in propios:
            continue  # ya lo tenemos con un enlace mejor
        canales.append({
            "name": bonito(nombre),
            "number": c.get("number") or 999,
            "url": stream,
            "logo": c.get("logo") or "",
        })
        importados += 1

    log("API tvabierta: %d canales importados de la categoria %s"
        % (importados, TVABIERTA_CATEGORY))
    return sorted(canales, key=lambda c: c.get("number") or 999)


def build_block(channel):
    """Bloque M3U. Ningun canal necesita cabeceras: la entrada queda limpia."""
    attrs = []
    if channel.get("logo"):
        attrs.append('tvg-logo="%s"' % channel["logo"])
    attrs.append('group-title="%s"' % GROUP)
    return ["#EXTINF:-1 %s,%s" % (" ".join(attrs), channel["name"]), channel["url"]]


# ----------------------------------------------------------------------
# Lista base de iptv-org
# ----------------------------------------------------------------------


def parse_blocks(text):
    """Divide la lista en (cabecera, bloques); cada bloque empieza en #EXTINF."""
    header, blocks, current, started = [], [], [], False

    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            if current:
                blocks.append(current)
            current = [line]
            started = True
        elif not started:
            header.append(line)
        else:
            current.append(line)

    if current:
        blocks.append(current)

    return header, blocks


def normalize_id(tvg_id):
    """'Telecentro.do@SD' -> 'telecentro.do'."""
    if not tvg_id:
        return None
    return tvg_id.split("@", 1)[0].strip().lower()


def block_tvg_id(block):
    """Extrae el tvg-id normalizado de un bloque."""
    extinf = block[0]
    marker = 'tvg-id="'
    i = extinf.find(marker)
    if i == -1:
        return None
    i += len(marker)
    j = extinf.find('"', i)
    if j == -1:
        return None
    return normalize_id(extinf[i:j])


# ----------------------------------------------------------------------
# Verificacion
# ----------------------------------------------------------------------


def check_stream(channel):
    """master playlist -> variante -> segmento .ts. Devuelve (ok, mensaje)."""
    try:
        master = http_get(channel["url"], timeout=15, retries=2).decode("utf-8", "replace")
    except RuntimeError as e:
        return False, "playlist: %s" % e

    if "#EXTM3U" not in master:
        return False, "la respuesta no es un M3U8"

    lines = [l.strip() for l in master.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return False, "playlist vacia (canal fuera del aire?)"

    # Si es un master, lines[0] es una variante; si es una media playlist
    # directa, es un segmento y vale el ultimo (ventana deslizante).
    primero = lines[0] if ".m3u8" in lines[0] else lines[-1]
    target = urllib.parse.urljoin(channel["url"].rsplit("/", 1)[0] + "/", primero)

    if ".m3u8" in target:
        try:
            media = http_get(target, timeout=15, retries=2).decode("utf-8", "replace")
        except RuntimeError as e:
            return False, "variante: %s" % e
        segs = [l.strip() for l in media.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            return False, "sin segmentos (canal fuera del aire?)"
        # El ULTIMO, no el primero: el mas antiguo puede haber expirado ya.
        target = urllib.parse.urljoin(target.rsplit("/", 1)[0] + "/", segs[-1])

    try:
        data = http_get(target, timeout=25, retries=2)
    except RuntimeError as e:
        return False, "segmento: %s" % e

    if len(data) < 10000:
        return False, "segmento sospechosamente pequeno (%d bytes)" % len(data)

    return True, "%.1f KB de video" % (len(data) / 1024.0)


def run_checks(canales):
    """
    Verifica en paralelo. Con ~95 canales en serie esto tardaria varios minutos
    y se comeria el timeout del workflow; con hilos baja a menos de un minuto.
    """
    log("Verificando %d enlaces..." % len(canales))
    with ThreadPoolExecutor(max_workers=12) as pool:
        resultados = list(pool.map(check_stream, canales))

    ok, filas = [], []
    for ch, (bien, msg) in zip(canales, resultados):
        log("  [%s] %-24s %s" % ("OK  " if bien else "FALLA", ch["name"][:24], msg))
        filas.append("| %s | %s | %s |" % ("OK" if bien else "FALLA", ch["name"], msg))
        if bien:
            ok.append(ch)

    fallos = len(canales) - len(ok)
    log("  -> %d OK, %d con fallos\n" % (len(ok), fallos))
    return ok, filas, fallos


# ----------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Genera lista.m3u con canales de RD.")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="archivo de salida")
    ap.add_argument("--check", action="store_true", help="verifica cada enlace")
    ap.add_argument("--drop-broken", action="store_true",
                    help="con --check, omite de la lista los que fallen")
    ap.add_argument("--strict", action="store_true",
                    help="con --check, sale con codigo 2 si algo falla")
    ap.add_argument("--proxy", default=PROXY_BASE, help="base del proxy (o env PROXY_BASE)")
    ap.add_argument("--no-base", action="store_true",
                    help="genera solo el grupo DOMINICANOS, sin iptv-org")
    args = ap.parse_args()

    proxy = (args.proxy or "").rstrip("/")
    if proxy:
        n = sum(1 for ch in CHANNELS if ch.get("proxy_path"))
        for ch in CHANNELS:
            if ch.get("proxy_path"):
                ch["url"] = proxy + ch["proxy_path"]
        log("Usando proxy para %d canal(es): %s" % (n, proxy))
    else:
        warn("sin proxy: los canales de Telemicro no funcionaran en Smarters")

    canales = cargar_canales()

    filas, fallos = [], 0
    if args.check:
        ok, filas, fallos = run_checks(canales)
        if args.drop_broken:
            canales = ok

    out_lines = ["#EXTM3U"]
    for ch in canales:
        out_lines.extend(build_block(ch))

    base_total = descartados = 0
    if not args.no_base:
        log("Descargando lista base de iptv-org (espanol)...")
        try:
            text = http_get(SOURCE_URL, timeout=60).decode("utf-8", "replace")
        except RuntimeError as e:
            error("no se pudo descargar la lista base: %s" % e)
            return 1

        _, blocks = parse_blocks(text)
        base_total = len(blocks)
        for block in blocks:
            if block_tvg_id(block) in OWN_IDS:
                descartados += 1
            else:
                out_lines.extend(block)
        log("Lista base: %d canales, %d descartados por duplicados"
            % (base_total, descartados))

    try:
        destino = os.path.abspath(args.output)
        carpeta = os.path.dirname(destino)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        with open(destino, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out_lines) + "\n")
    except OSError as e:
        error("no se pudo escribir %s: %s" % (args.output, e))
        return 1

    total = len(canales) + (base_total - descartados)
    log("Grupo %s: %d canales" % (GROUP, len(canales)))
    log("Total en la lista: %d" % total)
    log("Lista generada: %s" % destino)

    resumen = ["## Lista IPTV", "",
               "- Grupo `%s`: **%d** canales" % (GROUP, len(canales)),
               "- iptv-org: **%d** (descartados %d duplicados)" % (base_total, descartados),
               "- Total: **%d**" % total]
    if filas:
        resumen += ["", "### Verificacion del grupo %s" % GROUP, "",
                    "| Estado | Canal | Detalle |", "| --- | --- | --- |"] + filas
    summary(resumen)

    if fallos and args.strict:
        error("%d enlace(s) fallaron" % fallos)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
