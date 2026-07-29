#!/usr/bin/env python3
"""
Descarga la lista en espanol (spa.m3u) de iptv-org y reemplaza
el stream de Telecentro.do por el enlace oficial de Telemicro en 1080p.
El resultado se guarda en lista.m3u, listo para usar en apps de IPTV.
"""

import urllib.request
import sys

# ----------------------------------------------------------------------
# CONFIGURACION - edita esto si en el futuro cambia algo
# ----------------------------------------------------------------------

# Lista base de iptv-org que vamos a seguir (idioma espanol)
SOURCE_URL = "https://iptv-org.github.io/iptv/languages/spa.m3u"

# ID del canal que queremos arreglar (tal como aparece en tvg-id)
TARGET_TVG_ID = "Telecentro.do@SD"

# Enlaces BUENOS que queremos dejar para Telecentro.
# Se generan dos entradas (1080p y 720p) que comparten el mismo tvg-id
# y el mismo logo (icono), pero con distinto nombre y URL.
GOOD_STREAMS = [
    {
        "name": "Telecentro 13 (1080p)",
        "url": "https://live2.telemicro.com.do/live/telecentrocast_1080p/playlist.m3u8|Referer=https://telemicro.com.do/telecentro-en-vivo/&User-Agent=Mozilla/5.0",
    },
    {
        "name": "Telecentro 13 (720p)",
        "url": "https://live4.telemicro.com.do/live/13/playlist.m3u8|Referer=https://telemicro.com.do/telecentro-en-vivo/&User-Agent=Mozilla/5.0",
    },
]

# Opciones del reproductor (referrer y user-agent) que el stream necesita.
# Se mantienen para VLC en PC, pero las apps móviles usarán los parámetros en la URL (arriba).
GOOD_OPTS = [
    "#EXTVLCOPT:http-referrer=https://telemicro.com.do/telecentro-en-vivo/",
    "#EXTVLCOPT:http-user-agent=Mozilla/5.0",
]

OUTPUT_FILE = "lista.m3u"

# ----------------------------------------------------------------------


def download(url):
    """Descarga el texto de la lista remota."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_blocks(text):
    """
    Divide la lista en bloques. Cada bloque empieza con una linea #EXTINF
    e incluye las lineas siguientes (opciones #EXTVLCOPT, etc.) hasta la URL.
    Devuelve (cabecera, lista_de_bloques).
    """
    lines = text.splitlines()
    header = []
    blocks = []
    current = []
    started = False

    for line in lines:
        if line.startswith("#EXTINF"):
            # empieza un bloque nuevo; guarda el anterior si existe
            if current:
                blocks.append(current)
            current = [line]
            started = True
        elif not started:
            # todo lo que va antes del primer #EXTINF es cabecera (#EXTM3U ...)
            header.append(line)
        else:
            current.append(line)

    if current:
        blocks.append(current)

    return header, blocks


def block_tvg_id(block):
    """Extrae el valor de tvg-id de la linea #EXTINF de un bloque."""
    extinf = block[0]
    marker = 'tvg-id="'
    i = extinf.find(marker)
    if i == -1:
        return None
    i += len(marker)
    j = extinf.find('"', i)
    if j == -1:
        return None
    return extinf[i:j]


def set_extinf_name(extinf, new_name):
    """
    Toma una linea #EXTINF y le cambia SOLO el nombre del canal
    (lo que va despues de la ultima coma), conservando todos los
    atributos como tvg-id, tvg-logo, group-title, etc.
    """
    comma = extinf.rfind(",")
    if comma == -1:
        return extinf  # formato raro: no tocar
    return extinf[: comma + 1] + new_name


def build_replacement_blocks(original_extinf):
    """
    A partir del #EXTINF original (que conserva el logo y demas atributos),
    construye una lista de bloques: uno por cada calidad en GOOD_STREAMS.
    Todos comparten el mismo logo/tvg-id; solo cambian el nombre y la URL.
    """
    blocks = []
    for stream in GOOD_STREAMS:
        extinf = set_extinf_name(original_extinf, stream["name"])
        block = [extinf]
        block.extend(GOOD_OPTS)
        block.append(stream["url"])
        blocks.append(block)
    return blocks


def main():
    print("Descargando lista base de iptv-org (espanol)...")
    try:
        text = download(SOURCE_URL)
    except Exception as e:
        print("ERROR al descargar la lista:", e)
        sys.exit(1)

    header, blocks = parse_blocks(text)
    print("Canales encontrados en la lista base:", len(blocks))

    replaced = 0
    out_blocks = []

    for block in blocks:
        if block_tvg_id(block) == TARGET_TVG_ID:
            # Es Telecentro. Solo en la PRIMERA aparicion insertamos
            # nuestras dos calidades; las siguientes apariciones se
            # descartan para no dejar enlaces viejos ni duplicados.
            if replaced == 0:
                out_blocks.extend(build_replacement_blocks(block[0]))
            replaced += 1
        else:
            out_blocks.append(block)

    if replaced == 0:
        # El canal no estaba en la lista base: lo agregamos al final igual
        print("AVISO: no se encontro", TARGET_TVG_ID, "en la lista base. Se agregara.")
        extinf = (
            '#EXTINF:-1 tvg-id="%s" group-title="Republica Dominicana",'
            "Telecentro 13" % TARGET_TVG_ID
        )
        out_blocks.extend(build_replacement_blocks(extinf))
    else:
        print("Apariciones de Telecentro en la lista base:", replaced)
        print("Reemplazadas por 2 calidades (1080p + 720p), duplicados eliminados.")

    # Reconstruir el archivo final
    if not header:
        header = ["#EXTM3U"]

    out_lines = list(header)
    for block in out_blocks:
        out_lines.extend(block)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")

    print("Lista generada:", OUTPUT_FILE)


if __name__ == "__main__":
    main()
