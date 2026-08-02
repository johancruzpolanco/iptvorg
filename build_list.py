#!/usr/bin/env python3
"""
Genera lista.m3u con los canales dominicanos verificados, para IPTV Smarters.

    python build_list.py                  genera lista.m3u
    python build_list.py --check          verifica cada enlace (video real)
    python build_list.py --check --drop-broken   omite los que fallen

Codigos de salida:  0 ok | 1 error al escribir | 2 --strict con fallos

NOTAS DE MANTENIMIENTO
  - Telecentro pasa por un proxy (Cloudflare Worker, ver proxy/worker.js).
    Telemicro exige cabecera Referer en el playlist Y en los segmentos .ts, e
    IPTV Smarters no manda cabeceras propias: ignora las lineas #EXTVLCOPT y
    tampoco entiende el sufijo "|Referer=..." en la URL. El proxy la anade por
    nosotros, asi la entrada de la lista queda limpia.
  - Usar live4.telemicro.com.do, NO live2: live2 reparte entre dos backends,
    la sesion (nimblesessionid) se crea en uno y el segmento se pide al otro,
    lo que da 403/404 intermitentes.
  - Verificar solo el playlist no sirve: devuelve 200 aunque los segmentos
    esten fallando. Por eso --check descarga un segmento .ts de verdad.
"""

import argparse
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

GROUP = "Republica Dominicana"
DEFAULT_OUTPUT = "lista.m3u"

# Va en el codigo a proposito: cuando dependia de una variable del repo, la
# lista se publicaba sin proxy y Telecentro no cargaba en Smarters.
DEFAULT_PROXY = "https://tc13.johanecruzpolanco.workers.dev"
PROXY_BASE = os.environ.get("PROXY_BASE", DEFAULT_PROXY).rstrip("/")

CHANNELS = [
    {
        "name": "Telecentro 13",
        "url": "https://live4.telemicro.com.do/live/telecentrocast_1080p/playlist.m3u8",
        "proxy_path": "/live/telecentrocast_1080p/playlist.m3u8",
        "logo": "https://i.imgur.com/F17zNXh.png",
    },
    {
        "name": "Telecentro 13 (respaldo)",
        "url": "https://live4.telemicro.com.do/live/13/playlist.m3u8",
        "proxy_path": "/live/13/playlist.m3u8",
        "logo": "https://i.imgur.com/F17zNXh.png",
    },
    {
        "name": "Telesistema 11",
        "url": "https://cdn3.wind.do/streams/telesistema/telesistema_master.m3u8",
        "logo": "",
    },
    {
        "name": "Telesistema 11 (480p)",
        "url": (
            "https://live2.eu-north-1b.cf.dmcdn.net/sec2(UltzauhveZAlBafG4CTb_"
            "oOKxk7aIVTMKxqNIIKVDoPCPImCpEbDgvEICc7KG0cJsqGIe4k8gZLPOoK1zl61C_"
            "Iu1zsFtE2z4-oDpqE84-vgE9DwRxCci80_s_a5_aVv)/cloud/3/x80ac48/d/"
            "live-480.m3u8"
        ),
        "logo": "",
    },
    {
        "name": "Colorvision 9",
        "url": "https://cdn3.wind.do/streams/colorvision/colorvision_master.m3u8",
        "logo": "",
    },
    {
        "name": "Telemicro 5",
        "url": "https://cdn3.wind.do/streams/telemicro/telemicro_master.m3u8",
        "logo": "https://i.imgur.com/WhgySAk.png",
    },
    {
        "name": "Digital 15",
        "url": "https://cdn3.wind.do/streams/digital15/digital15_master.m3u8",
        "logo": "https://i.imgur.com/v3mkmZa.png",
    },
    # El master de teleuniverso anuncia 1080/720/640 pero solo existe la de
    # 720; las otras dan 404 y el reproductor puede colgarse con la que no esta.
    {
        "name": "Teleuniverso 29",
        "url": "https://cdn3.wind.do/streams/teleuniverso/teleuniverso_720.m3u8",
        "logo": "",
    },
]

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
            time.sleep(2 * intento)
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


def build_block(channel):
    """Bloque M3U. Ningun canal necesita cabeceras: la entrada queda limpia."""
    attrs = []
    if channel["logo"]:
        attrs.append('tvg-logo="%s"' % channel["logo"])
    attrs.append('group-title="%s"' % GROUP)
    return ["#EXTINF:-1 %s,%s" % (" ".join(attrs), channel["name"]), channel["url"]]


def check_stream(channel):
    """master playlist -> variante -> segmento .ts. Devuelve (ok, mensaje)."""
    try:
        master = http_get(channel["url"], timeout=20).decode("utf-8", "replace")
    except RuntimeError as e:
        return False, "playlist: %s" % e

    if "#EXTM3U" not in master:
        return False, "la respuesta no es un M3U8"

    lines = [l.strip() for l in master.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return False, "playlist vacia (canal fuera del aire?)"

    target = urllib.parse.urljoin(channel["url"].rsplit("/", 1)[0] + "/", lines[0])

    if ".m3u8" in target:
        try:
            media = http_get(target, timeout=20).decode("utf-8", "replace")
        except RuntimeError as e:
            return False, "variante: %s" % e
        segs = [l.strip() for l in media.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            return False, "sin segmentos (canal fuera del aire?)"
        target = urllib.parse.urljoin(target.rsplit("/", 1)[0] + "/", segs[0])

    try:
        data = http_get(target, timeout=30)
    except RuntimeError as e:
        return False, "segmento: %s" % e

    if len(data) < 10000:
        return False, "segmento sospechosamente pequeno (%d bytes)" % len(data)

    return True, "%.1f KB de video" % (len(data) / 1024.0)


def main():
    ap = argparse.ArgumentParser(description="Genera lista.m3u con canales de RD.")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="archivo de salida")
    ap.add_argument("--check", action="store_true", help="verifica cada enlace")
    ap.add_argument(
        "--drop-broken",
        action="store_true",
        help="con --check, omite de la lista los canales que fallen",
    )
    ap.add_argument(
        "--strict", action="store_true", help="con --check, sale con codigo 2 si algo falla"
    )
    ap.add_argument("--proxy", default=PROXY_BASE, help="base del proxy (o env PROXY_BASE)")
    args = ap.parse_args()

    proxy = (args.proxy or "").rstrip("/")
    if proxy:
        n = sum(1 for ch in CHANNELS if ch.get("proxy_path"))
        for ch in CHANNELS:
            if ch.get("proxy_path"):
                ch["url"] = proxy + ch["proxy_path"]
        log("Usando proxy para %d canal(es): %s" % (n, proxy))
    else:
        warn("sin proxy: Telecentro no funcionara en IPTV Smarters (falta el Referer)")

    incluidos, filas, fallos = list(CHANNELS), [], 0

    if args.check:
        log("Verificando enlaces...")
        incluidos = []
        for ch in CHANNELS:
            ok, msg = check_stream(ch)
            log("  [%s] %-26s %s" % ("OK  " if ok else "FALLA", ch["name"], msg))
            filas.append("| %s | %s | %s |" % ("OK" if ok else "FALLA", ch["name"], msg))
            if ok:
                incluidos.append(ch)
            else:
                fallos += 1
                warn("%s: %s" % (ch["name"], msg))
                if not args.drop_broken:
                    incluidos.append(ch)
        log("")

    out_lines = ["#EXTM3U"]
    for ch in incluidos:
        out_lines.extend(build_block(ch))

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

    log("Canales en la lista: %d de %d" % (len(incluidos), len(CHANNELS)))
    log("Lista generada: %s" % destino)

    resumen = ["## Lista IPTV", "", "- Canales: **%d**" % len(incluidos),
               "- Archivo: `%s`" % args.output]
    if filas:
        resumen += ["", "### Verificacion", "", "| Estado | Canal | Detalle |",
                    "| --- | --- | --- |"] + filas
    summary(resumen)

    if fallos and args.strict:
        error("%d enlace(s) fallaron" % fallos)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
