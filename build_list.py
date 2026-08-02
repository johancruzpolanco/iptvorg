#!/usr/bin/env python3
"""
Genera lista.m3u para IPTV Smarters: toma la lista en espanol de iptv-org y
sustituye los canales dominicanos por enlaces propios verificados.

Pensado para correr en GitHub Actions (solo stdlib, sin dependencias).

    python build_list.py                      genera lista.m3u
    python build_list.py --check              verifica los enlaces propios
    python build_list.py --check --strict     ...y falla el build si alguno cae
    python build_list.py --only-own -o rd.m3u solo los canales dominicanos

Codigos de salida:
    0  todo bien
    1  error irrecuperable (no se pudo bajar la lista base, no se pudo escribir)
    2  --strict y algun enlace propio fallo la verificacion

MANTENIMIENTO - las tres cosas que ya rompieron esta lista una vez:
  1) iptv-org cambio el formato de tvg-id: "Telecentro.do" paso a
     "Telecentro.do@SD". Por eso el match ignora el sufijo tras "@".
  2) live2.telemicro.com.do tiene el balanceo roto: reparte las peticiones
     entre dos backends, la sesion (nimblesessionid) se crea en uno y el
     segmento se pide al otro -> 403/404 intermitentes. Usar live4.
  3) Los segmentos .ts de Telemicro dan 403 sin Referer + User-Agent (el
     playlist si pasa, por eso parece que funciona). Los headers se escriben
     como atributos del #EXTINF -ademas de #EXTVLCOPT- porque IPTV Smarters
     no aplica de forma fiable las lineas #EXTVLCOPT.
"""

import argparse
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------------
# CONFIGURACION
# ----------------------------------------------------------------------

SOURCE_URL = "https://iptv-org.github.io/iptv/languages/spa.m3u"

# User-Agent moderno: Telemicro rechaza (403) los segmentos .ts cuando el
# User-Agent es de un cliente no-navegador.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TELEMICRO_REF = "https://telemicro.com.do/players/13tv/index_pc.php"

GROUP = "Republica Dominicana"
DEFAULT_OUTPUT = "lista.m3u"

# Canales propios; se colocan al principio de la lista final.
#   tvg_id : id en iptv-org SIN el sufijo @SD/@HD, para heredar el logo y
#            para eliminar de la lista base la entrada que sustituimos.
#            None si el canal no existe en iptv-org.
#   headers: True si el stream exige Referer + User-Agent.
CHANNELS = [
    {
        "name": "Telecentro 13 (1080p)",
        "url": "https://live4.telemicro.com.do/live/telecentrocast_1080p/playlist.m3u8",
        "tvg_id": "Telecentro.do",
        "headers": True,
        "logo": "https://i.imgur.com/F17zNXh.png",
    },
    {
        "name": "Telecentro 13 (respaldo)",
        "url": "https://live4.telemicro.com.do/live/13/playlist.m3u8",
        "tvg_id": "Telecentro.do",
        "headers": True,
        "logo": "https://i.imgur.com/F17zNXh.png",
    },
    {
        "name": "Colorvision 9",
        "url": "https://cdn3.wind.do/streams/colorvision/colorvision_master.m3u8",
        "tvg_id": "ColorVision.do",
        "headers": False,
        "logo": "",
    },
    {
        "name": "Telesistema 11",
        "url": "https://cdn3.wind.do/streams/telesistema/telesistema_master.m3u8",
        "tvg_id": "Telesistema11.do",
        "headers": False,
        "logo": "",
    },
    {
        "name": "Telemicro 5",
        "url": "https://cdn3.wind.do/streams/telemicro/telemicro_master.m3u8",
        "tvg_id": "Telemicro.do",
        "headers": False,
        "logo": "https://i.imgur.com/WhgySAk.png",
    },
    {
        "name": "Digital 15",
        "url": "https://cdn3.wind.do/streams/digital15/digital15_master.m3u8",
        "tvg_id": "Digital15.do",
        "headers": False,
        "logo": "https://i.imgur.com/v3mkmZa.png",
    },
    # El master de teleuniverso anuncia 1080/720/640 pero solo existe la de
    # 720; las otras dan 404 y el reproductor puede colgarse eligiendo la que
    # no esta. Por eso apuntamos directo a la variante buena.
    {
        "name": "Teleuniverso 29",
        "url": "https://cdn3.wind.do/streams/teleuniverso/teleuniverso_720.m3u8",
        "tvg_id": None,
        "headers": False,
        "logo": "",
    },
]

# ----------------------------------------------------------------------
# Salida (con anotaciones de GitHub Actions si estamos en CI)
# ----------------------------------------------------------------------

ON_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(("::warning::" if ON_CI else "AVISO: ") + msg, flush=True)


def error(msg):
    print(("::error::" if ON_CI else "ERROR: ") + msg, flush=True)


def summary(lines):
    """Escribe el resumen en la pestana Summary del job."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        warn("no se pudo escribir el summary: %s" % e)


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


def http_get(url, referer=None, timeout=30, retries=3):
    """
    GET con User-Agent de navegador y reintentos con backoff.
    Los runners de Actions sufren cortes de red esporadicos, y estos CDNs
    responden 5xx de vez en cuando; un solo intento da falsos negativos.
    """
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer

    last = None
    for intento in range(retries):
        if intento:
            time.sleep(2 * intento)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (401, 403, 404):
                break  # no tiene sentido reintentar
        except Exception as e:  # noqa: BLE001 - red: timeouts, DNS, TLS...
            last = str(e)

    raise RuntimeError(last or "fallo desconocido")


# ----------------------------------------------------------------------
# M3U
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
    """'Telecentro.do@SD' -> 'telecentro.do' (ver nota 1 de mantenimiento)."""
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


def build_block(channel):
    """
    Bloque M3U de un canal propio. Los headers van por partida doble:
      - atributos del #EXTINF -> los lee IPTV Smarters / TiviMate
      - lineas #EXTVLCOPT     -> las lee VLC
    """
    attrs = ['tvg-id="%s"' % (channel["tvg_id"] or "")]
    if channel["logo"]:
        attrs.append('tvg-logo="%s"' % channel["logo"])

    opts = []
    if channel["headers"]:
        attrs.append('http-referrer="%s"' % TELEMICRO_REF)
        attrs.append('http-user-agent="%s"' % BROWSER_UA)
        opts.append("#EXTVLCOPT:http-referrer=%s" % TELEMICRO_REF)
        opts.append("#EXTVLCOPT:http-user-agent=%s" % BROWSER_UA)

    attrs.append('group-title="%s"' % GROUP)

    return ["#EXTINF:-1 %s,%s" % (" ".join(attrs), channel["name"])] + opts + [
        channel["url"]
    ]


# ----------------------------------------------------------------------
# Verificacion
# ----------------------------------------------------------------------


def check_stream(channel):
    """
    Verifica de verdad: master playlist -> variante -> segmento .ts.
    Comprobar solo el playlist no sirve: devuelve 200 aunque los segmentos
    esten dando 403. Devuelve (ok, mensaje).
    """
    ref = TELEMICRO_REF if channel["headers"] else None

    try:
        master = http_get(channel["url"], ref, timeout=20).decode("utf-8", "replace")
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
            media = http_get(target, ref, timeout=20).decode("utf-8", "replace")
        except RuntimeError as e:
            return False, "variante: %s" % e
        segs = [l.strip() for l in media.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            return False, "sin segmentos (canal fuera del aire?)"
        target = urllib.parse.urljoin(target.rsplit("/", 1)[0] + "/", segs[0])

    try:
        data = http_get(target, ref, timeout=30)
    except RuntimeError as e:
        return False, "segmento: %s (faltan Referer/User-Agent?)" % e

    if len(data) < 10000:
        return False, "segmento sospechosamente pequeno (%d bytes)" % len(data)

    return True, "%.1f KB de video" % (len(data) / 1024.0)


def run_checks():
    """Verifica todos los canales propios. Devuelve (fallos, filas_para_summary)."""
    log("Verificando enlaces propios...")
    fallos, filas = 0, []

    for ch in CHANNELS:
        ok, msg = check_stream(ch)
        log("  [%s] %-26s %s" % ("OK  " if ok else "FALLA", ch["name"], msg))
        filas.append("| %s | %s | %s |" % ("OK" if ok else "FALLA", ch["name"], msg))
        if not ok:
            fallos += 1
            warn("%s: %s" % (ch["name"], msg))

    log("")
    return fallos, filas


# ----------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Genera lista.m3u para IPTV Smarters.")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="archivo de salida")
    ap.add_argument("--check", action="store_true", help="verifica los enlaces propios")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="con --check, sale con codigo 2 si algun enlace falla",
    )
    ap.add_argument(
        "--only-own",
        action="store_true",
        help="genera solo los canales dominicanos, sin la lista base",
    )
    args = ap.parse_args()

    filas = []
    fallos = 0

    if args.check:
        fallos, filas = run_checks()

    out_blocks = [build_block(c) for c in CHANNELS]
    header = ["#EXTM3U"]
    base_total = descartados = 0

    if not args.only_own:
        log("Descargando lista base de iptv-org (espanol)...")
        try:
            text = http_get(SOURCE_URL, timeout=60).decode("utf-8", "replace")
        except RuntimeError as e:
            error("no se pudo descargar la lista base: %s" % e)
            return 1

        base_header, blocks = parse_blocks(text)
        base_total = len(blocks)
        log("Canales en la lista base: %d" % base_total)

        own_ids = {normalize_id(c["tvg_id"]) for c in CHANNELS if c["tvg_id"]}
        kept = []
        for block in blocks:
            if block_tvg_id(block) in own_ids:
                descartados += 1
            else:
                kept.append(block)

        log("Bloques de la base descartados (los sustituimos): %d" % descartados)
        if descartados == 0:
            warn(
                "no se descarto ninguno: revisa si los tvg-id volvieron a cambiar "
                "en iptv-org. IDs buscados: %s" % sorted(own_ids)
            )

        if base_header:
            header = base_header
        out_blocks += kept

    out_lines = list(header)
    for block in out_blocks:
        out_lines.extend(block)

    # newline="\n" para que la lista no salga con CRLF segun el runner.
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

    log("Canales propios anadidos: %d" % len(CHANNELS))
    log("Total en la lista final: %d" % len(out_blocks))
    log("Lista generada: %s" % destino)

    resumen = [
        "## Lista IPTV generada",
        "",
        "- Canales propios: **%d**" % len(CHANNELS),
        "- Canales de iptv-org: **%d** (descartados %d que sustituimos)"
        % (base_total, descartados),
        "- Total: **%d**" % len(out_blocks),
        "- Archivo: `%s`" % args.output,
    ]
    if filas:
        resumen += [
            "",
            "### Verificacion de enlaces propios",
            "",
            "| Estado | Canal | Detalle |",
            "| --- | --- | --- |",
        ] + filas
    summary(resumen)

    if fallos:
        if args.strict:
            error("%d enlace(s) fallaron la verificacion" % fallos)
            return 2
        warn(
            "%d enlace(s) fallaron la verificacion, pero la lista se genero igual. "
            "Ojo: los runners de GitHub salen por IPs de datacenter y estos CDNs "
            "pueden bloquearlas aunque el canal funcione desde tu casa." % fallos
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
