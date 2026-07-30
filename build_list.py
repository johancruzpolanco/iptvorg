#!/usr/bin/env python3
"""
Descarga la lista en espanol (spa.m3u) de iptv-org y reemplaza
el stream de Telecentro.do por el enlace oficial de Telemicro en 1080p.
El resultado se guarda en lista.m3u, listo para usar en apps de IPTV.

Uso:
    python canales.py            genera lista.m3u
    python canales.py --check    diagnostica el stream desde ESTA pc
                                 (no genera nada, solo informa)
"""

import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------------
# CONFIGURACION - edita esto si en el futuro cambia algo
# ----------------------------------------------------------------------

# Lista base de iptv-org que vamos a seguir (idioma espanol)
SOURCE_URL = "https://iptv-org.github.io/iptv/languages/spa.m3u"

# ID del canal que queremos arreglar (tal como aparece en tvg-id).
# OJO: iptv-org le agrega sufijos de calidad al id ("Telecentro.do@SD").
# La comparacion ignora todo lo que venga despues de la "@", asi que
# esto sigue funcionando si manana cambian el sufijo otra vez.
TARGET_TVG_ID = "Telecentro.do"

# Cabeceras que el servidor EXIGE. Sin el referrer devuelve 403.
# Verificado: el iframe de telemicro.com.do/telecentro-en-vivo/ carga
# players/13tv/index.php, que redirige a index_pc.php (este referrer).
REFERRER = "https://telemicro.com.do/players/13tv/index_pc.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Enlaces BUENOS que queremos dejar para Telecentro.
# Se generan dos entradas que comparten el mismo tvg-id y el mismo logo,
# pero con distinto nombre y URL. Hoy AMBAS sirven 1920x1080; se dejan
# las dos como respaldo mutuo (son servidores distintos).
GOOD_STREAMS = [
    {
        "name": "Telecentro 13 (1080p)",
        "url": "https://live2.telemicro.com.do/live/telecentrocast_1080p/playlist.m3u8",
    },
    {
        "name": "Telecentro 13 (alterno)",
        "url": "https://live4.telemicro.com.do/live/13/playlist.m3u8",
    },
]

# Si tu app NO respeta las lineas #EXTVLCOPT (pasa en TiviMate, IPTV
# Smarters y varios reproductores de Android), deja esto en True: se
# agregan entradas EXTRA con las cabeceras pegadas a la URL con "|".
# Prueba en la app cual de las variantes reproduce y quedate con esa.
ADD_PIPE_VARIANTS = True

OUTPUT_FILE = "lista.m3u"

# ----------------------------------------------------------------------


def build_opts():
    """
    Devuelve las lineas de opciones que van debajo del #EXTINF.
    Se emite el mismo referrer/user-agent en los tres formatos mas
    comunes; cada reproductor lee el que entiende e ignora los demas,
    asi que ponerlos todos no rompe nada.
    """
    headers = {"Referer": REFERRER, "User-Agent": USER_AGENT}
    return [
        # VLC, OTT Navigator
        "#EXTVLCOPT:http-referrer=" + REFERRER,
        "#EXTVLCOPT:http-user-agent=" + USER_AGENT,
        # Kodi / inputstream.adaptive
        "#KODIPROP:inputstream.adaptive.manifest_headers="
        + urllib.parse.urlencode(headers),
        "#KODIPROP:inputstream.adaptive.stream_headers="
        + urllib.parse.urlencode(headers),
        # Varias apps genericas
        "#EXTHTTP:" + json.dumps({"referrer": REFERRER, "User-Agent": USER_AGENT}),
    ]


def pipe_url(url):
    """URL con las cabeceras pegadas al final, estilo TiviMate/Smarters."""
    return "%s|Referer=%s&User-Agent=%s" % (
        url,
        urllib.parse.quote(REFERRER, safe=""),
        urllib.parse.quote(USER_AGENT, safe=""),
    )


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


def base_id(tvg_id):
    """
    Quita el sufijo de calidad del tvg-id: "Telecentro.do@SD" -> "Telecentro.do".
    Sin esto la lista base no hace match y el canal viejo se queda dentro.
    """
    if not tvg_id:
        return tvg_id
    return tvg_id.split("@", 1)[0]


def set_extinf_attr(extinf, attr, value):
    """
    Pone (o corrige) un atributo attr="value" en la linea #EXTINF, sin
    tocar el nombre del canal. iptv-org ya trae http-referrer y
    http-user-agent propios; los sobreescribimos con los nuestros para
    que no haya dos valores distintos peleando.
    """
    comma = extinf.rfind(",")
    if comma == -1:
        return extinf

    attrs, name = extinf[:comma], extinf[comma:]
    marker = attr + '="'
    i = attrs.find(marker)
    if i != -1:
        j = attrs.find('"', i + len(marker))
        if j != -1:
            attrs = attrs[: i + len(marker)] + value + attrs[j:]
            return attrs + name
    return attrs + ' %s="%s"' % (attr, value) + name


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
    construye una lista de bloques: uno por cada calidad en GOOD_STREAMS,
    mas las variantes con cabeceras en la URL si ADD_PIPE_VARIANTS.
    """
    opts = build_opts()
    # unificar las cabeceras que iptv-org trae en la propia linea #EXTINF
    base = set_extinf_attr(original_extinf, "http-referrer", REFERRER)
    base = set_extinf_attr(base, "http-user-agent", USER_AGENT)

    blocks = []
    for stream in GOOD_STREAMS:
        block = [set_extinf_name(base, stream["name"])]
        block.extend(opts)
        block.append(stream["url"])
        blocks.append(block)

    if ADD_PIPE_VARIANTS:
        for stream in GOOD_STREAMS:
            name = stream["name"] + " [hdr]"
            blocks.append([set_extinf_name(base, name), pipe_url(stream["url"])])

    return blocks


# ----------------------------------------------------------------------
# Modo diagnostico
# ----------------------------------------------------------------------


def check():
    """
    Prueba el stream desde esta pc: manifiesto, sub-manifiesto y un
    segmento de video real. Conserva cookies, porque el servidor esta
    detras de un balanceador y la sesion se pierde sin la cookie __cflb.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    hdrs = {"Referer": REFERRER, "User-Agent": USER_AGENT}

    def get(url):
        """Devuelve (codigo, cuerpo_en_bytes). Nunca lanza excepcion."""
        try:
            with opener.open(urllib.request.Request(url, headers=hdrs), timeout=25) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except Exception as e:
            return 0, str(e).encode()

    ok_any = False

    for stream in GOOD_STREAMS:
        base = stream["url"].rsplit("/", 1)[0]
        print("\n=== %s" % stream["name"])
        print("    %s" % stream["url"])

        code, body = get(stream["url"])
        print("    [1] playlist.m3u8 ....... %s" % code)
        if code != 200:
            print("        -> el manifiesto no carga; enlace o referrer caducado")
            continue

        # ultima linea no comentada del master = sub-manifiesto
        lines = [l.strip() for l in body.decode("utf-8", "replace").splitlines()]
        sub = [l for l in lines if l and not l.startswith("#")]
        if not sub:
            print("        -> el manifiesto vino vacio")
            continue

        code, body = get(base + "/" + sub[-1])
        print("    [2] chunks.m3u8 ......... %s" % code)
        if code != 200:
            print("        -> 403 aqui = la sesion se pierde entre peticiones")
            continue

        segs = [
            l.strip()
            for l in body.decode("utf-8", "replace").splitlines()
            if l.strip() and not l.startswith("#")
        ]
        if not segs:
            print("        -> sin segmentos en el sub-manifiesto")
            continue

        code, body = get(base + "/" + segs[0])
        ts_ok = len(body) > 1000 and body[:1] == b"\x47"
        print(
            "    [3] segmento .ts ........ %s (%d bytes, MPEG-TS valido: %s)"
            % (code, len(body), "si" if ts_ok else "NO")
        )
        if ts_ok:
            ok_any = True
            print("        -> ESTE ENLACE FUNCIONA desde esta pc")
        else:
            print("        -> el manifiesto carga pero el video NO baja")

    print("\n" + "-" * 60)
    if ok_any:
        print("Resultado: al menos un enlace entrega video desde esta pc.")
        print("Si en tu app no reproduce, el problema son las cabeceras:")
        print("prueba las entradas '[hdr]' de la lista.")
    else:
        print("Resultado: ningun enlace entrego video desde esta pc.")
        print("El problema esta en el servidor o en el bloqueo por IP,")
        print("no en el formato de la lista.")


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
        if base_id(block_tvg_id(block)) == base_id(TARGET_TVG_ID):
            # Es Telecentro. Solo en la PRIMERA aparicion insertamos
            # nuestras entradas; las siguientes apariciones se
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
        n = len(GOOD_STREAMS) * (2 if ADD_PIPE_VARIANTS else 1)
        print("Reemplazadas por %d entradas, duplicados eliminados." % n)

    # Reconstruir el archivo final
    if not header:
        header = ["#EXTM3U"]

    out_lines = list(header)
    for block in out_blocks:
        out_lines.extend(block)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")

    print("Lista generada:", OUTPUT_FILE)
    print("\nSi Telecentro no reproduce, corre:  python canales.py --check")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        main()
