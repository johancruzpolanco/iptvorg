#!/usr/bin/env python3
"""
Genera lista.m3u para IPTV Smarters:

  1. Grupo DOMINICANOS: los canales de la categoria RD de la API de tvabierta,
     ordenados por numero de canal, mas los enlaces propios que sustituyen a
     los de la API cuando tenemos uno mejor (los de Telemicro, que van por
     nuestro proxy).
  2. Detras, la lista en espanol de iptv-org, los canales hispanos de Free-TV
     y otras categorias de tvabierta (HISPANOS, DEPORTES, MX, PE...), sin los
     que ya salen antes para que no haya duplicados, repartidos en grupos en
     espanol (Noticias, Deportes, Documentales... y Generales por pais).
  3. Al final, los grupos "Pluto TV" (Latinoamerica, por el Worker) y
     "Samsung TV Plus" (Espana).

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
  - Teleantillas NO va por el Worker. Su pagina emite con un embed de
    Dailymotion, pero Dailymotion bloquea a Cloudflare (403 E030) y desde el
    Worker nunca sale la senal. Se usa tvabierta como principal (por peticion
    del usuario) y el Flussonic de iptv-org queda como alternativa. Ojo: la
    retransmision de tvabierta se reinicia a menudo (MEDIA-SEQUENCE vuelve a 0;
    3 veces en 2 horas el 13/09/2026) y Smarters puede congelarse en cada
    reinicio.
  - Los canales de iptv-org que piden Referer/User-Agent (http-referrer,
    http-user-agent, #EXTVLCOPT, sufijo "|Referer=") se publican ya cambiados
    por enlaces /h/<token>/... del Worker, que es quien pone esas cabeceras.
    Asi funcionan tambien las pantallas que leen lista.m3u por jsDelivr. El
    token va firmado con PROXY_KEY, que tiene que ser el MISMO secreto en el
    repo (Settings > Secrets > Actions) y en el Worker. Sin el, esos canales
    se publican como vienen (y solo funcionan en VLC y similares).
  - Teleuniverso 29 viene de la API de tvabierta (por peticion del usuario).
    Esta en RESPALDOS: si la API no lo trae (o no responde) se usa el enlace
    fijo de hls.tvabierta.net.
  - Antena 7 es canal propio: la API de tvabierta lo quito de RD (el 7 paso a
    ser Pulso Vision, 29/09/2026). Su senal oficial no pide cabeceras, asi que
    NO va por el Worker. El enlace se relee de "streamUrl" en su pagina en
    cada build ("stream_page"); la pagina limita el reproductor a visitas de
    RD, pero es solo en el navegador (ip-api). El CDN SI da 403 a GitHub
    Actions (30/09/2026; desde RD y desde otros servidores responde 200), asi
    que con --check el 403 no cuenta como caida ("ignorar_403") y se publica
    la senal oficial. Si falla de otra forma se pasa a tvabierta (007).
  - "alternatives": enlaces de repuesto. Con --check, si el principal falla se
    publica el primero de ellos que funcione.
  - NO fijar enlaces de dmcdn.net (Dailymotion): llevan un token sec2(...) que
    caduca en horas. Tampoco fijar los /memfs/<uuid> de tvabierta: son ids de
    proceso que cambian si el canal reinicia. Por eso se resuelven por API.
  - Verificar solo el playlist no sirve: devuelve 200 aunque los segmentos
    fallen. Y hay que pedir el ULTIMO segmento, no el primero: la playlist de
    un directo es una ventana deslizante y el mas antiguo puede haber expirado.
  - Duplicados: un canal de iptv-org/Free-TV/tvabierta se descarta si ya esta
    antes con el mismo tvg-id, el mismo enlace (host + ruta) o el mismo nombre
    normalizado (clave_canal: "CV Vision TV (288p)" y "cvvision" -> "cvvision").
    Contra DOMINICANOS solo se comparan los canales de RD (tvg-id .do): hay
    nombres repetidos en otros paises (Telefuturo de Paraguay). Si el
    duplicado trae otro enlace, queda como alternativa del canal de arriba.
    Los que el nombre no delata van en ALIAS ("CDN" es "cdn37").
  - Pluto TV: el enlace sin token (service-stitcher/stitch/hls) devuelve 200
    pero es un video de relleno igual para todos los canales, y jmp2.uk/plu-
    (el que trae iptv-org) da 403 "channel not permitted for partner" a los
    canales de Latinoamerica. Hace falta un token de boot.pluto.tv, que dura
    24 h: por eso cada canal apunta a /pluto/<id>.m3u8 del Worker, que pide el
    token y redirige. Los Pluto de iptv-org se quitan para no tenerlos dos
    veces (y rotos). No se verifican con --check: desde GitHub no da la senal
    de Latinoamerica.
  - Samsung TV Plus solo tiene Espana en la region hispana. Desde RD funcionan
    unos 2 de cada 3 (06/10/2026); el resto da 403/400 por bloqueo a Espana.
    Los que llevan DRM (license_url) se omiten: Smarters no los reproduce.
  - Plex NO se usa: jmp2.uk lo sirve con un X-Plex-Token compartido que
    devuelve 429 casi siempre, y los de Mexico dan 404 fuera de Mexico (de 60
    probados desde RD funcionaron 11, el 06/10/2026).
"""

import argparse
import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import sys
import time
import unicodedata
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

# Free-TV: lista revisada a mano. Su group-title es el pais; de ella solo se
# toman estos grupos. "News (ES)" son noticias en espanol de varios paises.
FREETV_URL = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
FREETV_GRUPOS = {
    "Argentina", "Chile", "Costa Rica", "Dominican Republic", "Mexico",
    "Paraguay", "Peru", "Spain", "Venezuela", "News (ES)",
}

# Otras categorias de tvabierta (ademas de RD): categoria -> (categoria al
# estilo iptv-org para clasificar(), pais). No van US, LocalNow, BR, NEWS ni
# CHRISTIAN: casi todo es en ingles, portugues, arabe o fines.
TVABIERTA_EXTRA = {
    "HISPANOS": ("General", ""), "DEPORTES": ("Sports", ""),
    "PELICULAS": ("Movies", ""), "KIDS": ("Kids", ""), "MUSIC": ("Music", ""),
    "CULTURA": ("Culture", ""),
    "MX": ("General", "mx"), "PR": ("General", "pr"), "ES": ("General", "es"),
    "AR": ("General", "ar"), "CL": ("General", "cl"), "CO": ("General", "co"),
    "VE": ("General", "ve"), "HN": ("General", "hn"), "PE": ("General", "pe"),
    "GT": ("General", "gt"), "EC": ("General", "ec"), "BO": ("General", "bo"),
    "CR": ("General", "cr"), "PA": ("General", "pa"), "SV": ("General", "sv"),
}

# Canales que tvabierta mete en RD sin ser dominicanos: van con los demas,
# en el grupo de esa categoria.
NO_DOMINICANOS = {"bloomberg": "News"}

# Canales gratis con anuncios. i.mjh.nz publica la parrilla de cada region
# (id, nombre, logo, numero); ver NOTAS para Pluto, Samsung y Plex.
PLUTO_CHANNELS = "https://i.mjh.nz/PlutoTV/.channels.json.gz"
PLUTO_REGIONES = ["mx", "ar"]
PLUTO_GROUP = "Pluto TV"
SAMSUNG_CHANNELS = "https://i.mjh.nz/SamsungTVPlus/.channels.json.gz"
SAMSUNG_REGION = "es"
SAMSUNG_GROUP = "Samsung TV Plus"

# Guias de programacion (EPG) que casan con los tvg-id de Pluto y Samsung.
EPG_URLS = [
    "https://i.mjh.nz/PlutoTV/mx.xml.gz",
    "https://i.mjh.nz/SamsungTVPlus/es.xml.gz",
]

# Va en el codigo a proposito: cuando dependia de una variable del repo, la
# lista se publicaba sin proxy y Telecentro no cargaba en Smarters.
DEFAULT_PROXY = "https://tc13.johanecruzpolanco.workers.dev"
PROXY_BASE = os.environ.get("PROXY_BASE", DEFAULT_PROXY).rstrip("/")

# Secreto compartido con el Worker para firmar los enlaces /h/... (ver NOTAS).
PROXY_KEY = os.environ.get("PROXY_KEY", "")

# Igual que HDR_SIG_BYTES en worker.js.
HDR_SIG_BYTES = 16

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
        "name": "Teleantillas",
        "number": 10,
        "api_name": "teleantillas",
        # tvabierta como principal (por peticion del usuario). Ojo: se reinicia
        # con frecuencia y puede congelar Smarters. Flussonic de iptv-org de
        # repuesto (1080p, ventana de 24 s, sin reinicios; solo http).
        "url": "https://hls.tvabierta.net/hls/010.m3u8",
        "alternatives": ["http://45.171.108.253:8888/TELEANTILLAS/index.m3u8"],
        "logo": "https://tvabierta.net/010.png",
    },
    {
        "name": "Antena 7",
        "number": 7,
        "api_name": "antena7",
        # Senal oficial (OvenMediaEngine tras CloudFront, 720p/360p/236p, sin
        # cabeceras ni token). La pagina la trae en "streamUrl"; se relee en
        # cada build por si cambia y este enlace queda de respaldo.
        "url": "https://d3gie3ig6argu.cloudfront.net/ts:abr.m3u8",
        "stream_page": "https://www.antena7.com.do/envivo-canal-7/",
        "alternatives": ["https://hls.tvabierta.net/hls/007.m3u8"],
        # El CDN da 403 a GitHub Actions (y no a los usuarios): ver NOTAS.
        "ignorar_403": True,
        "logo": "https://tvabierta.net/007.png",
    },
]

# "streamUrl" que las paginas WordPress de Mediatique (Antena 7) embeben en su
# configuracion (wpApp.appModel.schedule), con las barras escapadas.
STREAM_URL_RX = re.compile(r'"streamUrl":"(https?:[^"]+?\.m3u8[^"]*)"')

# Canales que se toman de la API. Aqui solo se fija el nombre con el que se
# muestran y un enlace de respaldo que se usa SOLO si la API no trae el canal
# (no esta, viene deshabilitado, sin stream, o la API no responde).
RESPALDOS = [
    {
        "name": "Teleuniverso 29",
        "number": 29,
        "api_name": "teleuniversotv",
        "url": "https://hls.tvabierta.net/hls/029.m3u8",
        "logo": "",
    },
]

# Nombre con el que se muestran los canales de tvabierta, que vienen como
# "boncheslatinostv" o "cvvision". Clave: clave_nombre() del nombre de la API.
# Los que no estan aqui pasan por bonito().
NOMBRES = {
    # RD
    "a7tv": "A7 TV", "rnn": "RNN", "latinostvny": "Latinos TV NY",
    "boncheslatinostv": "Bonches Latinos TV", "manaclartv": "Manaclar TV",
    "canal19": "Cinevisión Canal 19", "elpuertotv": "El Puerto TV",
    "multivision": "Multivisión", "canalda": "Canalda 26",
    "hainavision": "Haina Visión", "bajotechotv": "Bajo Techo TV",
    "bellavision": "Bellavisión", "retv": "R&E TV", "lareina": "La Reina TV",
    "jimanitv": "Jimaní TV", "oepm": "OEPM TV", "colorvision": "Color Visión",
    "zonavision": "Zona Visión", "telecibao": "Telecibao",
    "cvvision": "CV Visión", "rtvd": "RTVD", "telesistema": "Telesistema 11",
    "teleunion": "Teleunión", "cielotv": "Cielo TV", "antena21": "Antena 21",
    "vtv32": "VTV 32", "cdn37": "CDN 37", "acentotv": "Acento TV",
    "cotubanamatv": "Cotubanamá TV", "misioneltv": "Misión EL TV",
    "tvluz": "TV Luz", "santacruztv": "Santa Cruz TV", "makaotv": "Makao TV",
    "adoram": "Adoram TV", "radioemanuel": "Radio Emanuel TV",
    "cocotv": "Coco TV", "cntmas": "CNT Más TV",
    "portaldigitaltv": "Portal Digital TV", "telecanal12": "Telecanal 12",
    "televisiondeleste": "Televisión del Este", "radio67tv": "Radio 67 TV",
    "canaldtvlatino": "Canal DTV Latino", "senaldigital": "Señal Digital TV",
    "visionndv": "Visión NDV", "carivision": "Carivisión",
    "canaldelsol": "Canal del Sol", "elseis": "El Seis", "14tv": "Canal 14",
    "ahoratv": "Ahora TV", "entelevision": "En Televisión",
    "altantotv": "Al Tanto TV", "super7": "Super 7 TV",
    "telecontacto": "Telecontacto", "jarabacoatv": "Jarabacoa TV",
    "microvision": "Microvisión 10", "readytelevision": "Ready TV Canal 6",
    "vallevision": "Vallevisión", "alegretvrd": "Alegre TV",
    "palmartv": "Palmar TV", "bonaotv": "Bonao TV", "latora": "La Tora TV",
    "chinolatv": "Chinola TV", "lunatv": "Luna TV Canal 53",
    "extra86": "Extra 86 TV", "romanatv": "Romana TV Canal 42", "zol": "Zol TV",
    "tvo": "TVO", "vegateve": "Vega Teve", "sitvrd": "SiTV",
    "telemedios": "Telemedios Canal 8", "telefuturo": "Telefuturo",
    "teleimpacto": "Teleimpacto", "tvm": "TV Montaña Canal 10",
    "boreal": "Boreal TV",
    # Otras categorias
    "bloomberg": "Bloomberg", "cnne": "CNN en Español",
    "f24e": "France 24 Español", "rte": "RT en Español", "kanald": "Kanal D",
    "cn247": "CN 24/7", "ejtv": "EJTV", "daystar": "Daystar", "24h": "24 Horas",
    "mpatv": "MPA TV", "tgn": "TGN", "bpskids": "BPS Kids", "ducktv": "Duck TV",
    "sensical": "Sensical", "zoomoo": "Zoomoo", "sat": "SAT-7 Kids",
    "canalsinnombre": "Extrema Kids", "energytvmusic": "Energy TV Music",
    "djazz": "DJazz", "lxhome": "LX Home", "vconline": "VC Online",
    "sealcolombia": "Señal Colombia", "tlt": "Tele Tuya", "willax": "Willax",
    "panamericana": "Panamericana", "latinape": "Latina",
    "canalantigua": "Canal Antigua", "penielfamiliar": "Peniel Familiar",
    "ecuavisa1": "Ecuavisa", "aragontv": "Aragón TV", "startve": "Star TVE",
    "monterrey": "Canal 6 Monterrey", "quierotv": "Quiero TV",
}

# Nombres de iptv-org/Free-TV -> clave del canal de tvabierta, para los
# duplicados que ni el nombre (clave_canal) ni el enlace delatan.
ALIAS = {"cdn": "cdn37", "elseis6": "elseis", "telesistema11": "telesistema"}

# Cambios al nombre normalizado para comparar canales, en orden. Cada uno solo
# se aplica si deja al menos 3 caracteres ("retv" no es "re"). "Canal 32" se
# queda en "32" y no se borra: VTV Canal 32 y VTV Canal 17 son distintos.
CAMBIOS_NOMBRE = [
    (r"^canal(?=[a-z])", ""),
    (r"canal(\d+)$", r"\1"),
    (r"(?:television|teve|tv|rd|fm)$", ""),
]

# --- Categorias de iptv-org en espanol ---
#
# iptv-org da categorias en ingles y a veces varias ("Documentary;Series").
# Cada canal va a UN grupo: el de mayor prioridad entre sus categorias y las
# palabras clave de su nombre (asi "Historia HD", que viene como
# Entertainment, cae en Documentales). El orden de PRIORIDAD es tambien el
# orden de los grupos en la lista, detras de DOMINICANOS.
CATEGORIAS = {
    "documentary": "Documentales", "science": "Documentales",
    "travel": "Documentales", "outdoor": "Documentales",
    "kids": "Infantiles", "animation": "Infantiles",
    "sports": "Deportes",
    "news": "Noticias", "weather": "Noticias", "business": "Noticias",
    "movies": "Películas", "classic": "Películas",
    "series": "Series",
    "music": "Música",
    "religious": "Religiosos",
    "culture": "Cultura y educación", "education": "Cultura y educación",
    "lifestyle": "Estilo de vida", "cooking": "Estilo de vida",
    "auto": "Estilo de vida", "shop": "Estilo de vida",
    "entertainment": "Entretenimiento", "comedy": "Entretenimiento",
    "family": "Entretenimiento", "relax": "Entretenimiento",
    "legislative": "Institucionales",
    "general": "Generales", "public": "Generales", "undefined": "Generales",
}

GENERALES = "Generales"

PRIORIDAD = [
    "Infantiles", "Documentales", "Deportes", "Religiosos", "Noticias",
    "Películas", "Series", "Música", "Cultura y educación", "Estilo de vida",
    "Entretenimiento", "Institucionales", GENERALES,
]

# Orden en que salen los grupos en la lista.
ORDEN_GRUPOS = [
    "Noticias", "Deportes", "Películas", "Series", "Documentales", "Infantiles",
    "Entretenimiento", "Música", "Cultura y educación", "Estilo de vida",
    "Religiosos", "Institucionales",
]

# Palabras del nombre que delatan el tema aunque la categoria diga otra cosa.
# Sin "MTV" ni "Hits": hoy son realities (MTV Catfish) o cine (HBO Hits).
PALABRAS = [(g, re.compile(rx, re.I)) for g, rx in [
    ("Infantiles", r"\bkids?\b|niñ[oa]s|infantil|cartoon|\btoons?\b|dibujos"
                   r"|\bbaby\b|\bclan\b|disney|\bnick|anime"),
    ("Documentales", r"discovery|nat ?geo|national geographic|\bhistory\b"
                     r"|\bhistoria\b|documental|\bdocu|animal planet|naturaleza"
                     r"|\bnature\b|\bwild\b|\bviajes?\b|\btravel\b|ciencia"
                     r"|\bscience\b|odisea|curiosity"),
    ("Deportes", r"\bsports?\b|\bdeportes?\b|f[uú]tbol|\bgol\b|\bespn|\btudn"
                 r"|\btyc\b|\bdazn\b|\bgolf\b|\btenis\b|\bnba\b|\bnfl\b|\bmlb\b"
                 r"|\bufc\b|boxeo|\bracing\b"),
    ("Religiosos", r"iglesia|church|cristian|\bcristo\b|\bjes[uú]s\b|cat[oó]lic"
                   r"|evang|\bdios\b|gospel|\bewtn\b|\benlace\b"
                   r"|mar[ií]a ?visi[oó]n|\besne\b|adventist|\bhope\b|3abn"
                   r"|biblia|bible|ministerio"),
    ("Noticias", r"\bnews\b|noticia|\b24 ?h\b|24 horas|\bcnn\b|telediario"
                 r"|informativ|euronews|\bdw\b|france 24"),
    ("Películas", r"\bcine\b|cinema|\bmovies?\b|pel[ií]culas|\bfilms?\b|\btcm\b"),
    ("Música", r"\bmusic|m[uú]sica|karaoke|reggaet|\bsalsa\b|bachata"),
]]

# Generales se parte por pais (sale del tvg-id: "Canal13.cl@SD"); si no, son
# mas de mil canales en un solo grupo. Paises con menos canales van a "Otros".
PAISES = {
    "do": "República Dominicana", "ar": "Argentina", "bo": "Bolivia",
    "cl": "Chile", "co": "Colombia", "cr": "Costa Rica", "cu": "Cuba",
    "ec": "Ecuador", "es": "España", "gt": "Guatemala", "hn": "Honduras",
    "mx": "México", "ni": "Nicaragua", "pa": "Panamá", "pe": "Perú",
    "pr": "Puerto Rico", "py": "Paraguay", "sv": "El Salvador",
    "us": "Estados Unidos", "uy": "Uruguay", "ve": "Venezuela",
}
MIN_POR_PAIS = 10
OTROS_PAISES = "Otros países"

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


def http_get(url, timeout=30, retries=3, con_url=False):
    """
    GET con User-Agent de navegador y reintentos con backoff. Con con_url
    devuelve (cuerpo, URL final tras las redirecciones).
    """
    last = None
    for intento in range(retries):
        if intento:
            time.sleep(1.5 * intento)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (resp.read(), resp.geturl()) if con_url else resp.read()
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
    """
    Nombre para mostrar: el de NOMBRES si lo hay; si no, sin el "154 | " ni
    los emojis que trae a veces la API y con la primera letra en mayuscula
    ('colorvision' -> 'Colorvision'; respeta los que ya vienen en mayusculas).
    """
    nombre = (nombre or "").strip()
    if clave_nombre(nombre) in NOMBRES:
        return NOMBRES[clave_nombre(nombre)]
    nombre = re.sub(r"^\d+\s*\|\s*", "", nombre)
    nombre = "".join(ch for ch in nombre
                     if unicodedata.category(ch)[0] in "LNPZ" or ch in "+&").strip()
    # Sin capitalize(): pasaria "974 Qwest TV" a "974 qwest tv".
    return nombre[:1].upper() + nombre[1:]


def clave_nombre(nombre):
    """'Antena 7 HD', 'antena-7', 'Antena7' -> 'antena7'."""
    nombre = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore")
    clave = re.sub(r"[^a-z0-9]", "", nombre.decode("ascii").lower())
    return re.sub(r"(?<=.)(?:fhd|hd|sd)$", "", clave)


def clave_canal(nombre):
    """
    Clave para buscar duplicados entre fuentes: sin resolucion ni etiquetas,
    con CAMBIOS_NOMBRE ("canal" delante, "TV" detras...) y pasada por ALIAS.
    'CV Vision TV (288p) [Geo-blocked]' y 'cvvision' -> 'cvvision';
    'Canal Multivision' y 'multivision' -> 'multivision'.
    """
    nombre = re.sub(r"\([^)]*\)|\[[^\]]*\]|^\d+\s*\|", "", nombre or "")
    clave = clave_nombre(nombre)
    for rx, por in CAMBIOS_NOMBRE:
        corto = re.sub(rx, por, clave)
        if len(corto) >= 3:
            clave = corto
    return ALIAS.get(clave, clave)


def clave_enlace(url):
    """
    'https://Host:8081/a/b.m3u8?id=7|Referer=...' -> 'host/a/b.m3u8?id=7'.
    Sin esquema ni puerto, pero con la query: hay servidores que reparten
    varios canales por la misma ruta (hybrid/play.m3u8?id=..., ?network_id=).
    """
    partes = urllib.parse.urlsplit((url or "").split("|", 1)[0].strip())
    clave = ((partes.hostname or "") + partes.path).lower()
    return clave + ("?" + partes.query if partes.query else "")


def numero_canal(c):
    """
    Numero del canal de la API. El "number" de tvabierta es solo la posicion
    en su lista (Bloomberg es el 1); el numero real va en el nombre de sus
    archivos: 004.png, hls/011.m3u8. None si no lo trae.
    """
    for campo in ("logo", "stream"):
        m = re.search(r"/(\d{3})\.(?:png|jpe?g|m3u8)$", c.get(campo) or "")
        if m:
            return int(m.group(1))
    return None


def ordenar(canales):
    """Primero los que tienen numero, por numero; luego el resto, por nombre."""
    return sorted(canales, key=lambda c: (c.get("number") is None,
                                          c.get("number") or 0,
                                          c["name"].lower()))


def resolver_desde_pagina(canal):
    """Cambia canal["url"] por el streamUrl de su pagina; si no, deja el fijo."""
    try:
        html = http_get(canal["stream_page"], timeout=20, retries=2).decode("utf-8", "replace")
    except RuntimeError as e:
        warn("%s: no se pudo leer %s (%s); se usa el enlace fijo"
             % (canal["name"], canal["stream_page"], e))
        return
    m = STREAM_URL_RX.search(html)
    if not m:
        warn("%s: la pagina no trae streamUrl; se usa el enlace fijo" % canal["name"])
        return
    url = m.group(1).replace("\\/", "/")
    if url != canal["url"]:
        warn("%s: la pagina da otro enlace (%s); se usa ese" % (canal["name"], url))
        canal["alternatives"] = [canal["url"]] + canal.get("alternatives", [])
        canal["url"] = url


def leer_tvabierta():
    """El JSON de la API de tvabierta, o None si no responde."""
    try:
        return json.loads(http_get(TVABIERTA_API, timeout=30).decode("utf-8", "replace"))
    except (RuntimeError, ValueError) as e:
        warn("no se pudo leer la API de tvabierta (%s); van los canales propios "
             "y los enlaces de respaldo" % e)
        return None


def cargar_canales(data):
    """
    Devuelve la lista final del grupo DOMINICANOS: los canales propios mas los
    de la categoria RD de la API (data), ordenados por numero de canal. Los
    canales de RESPALDOS que la API no traiga se anaden con su enlace fijo.

    Si la API no respondio (data None) se sigue adelante con los propios y los
    respaldos: preferimos una lista corta a no generar nada.
    """
    # Un canal de la API es "nuestro" si coincide el nombre normalizado o si su
    # enlace es uno de los nuestros (principal o alternativa): asi no sale dos
    # veces aunque tvabierta lo renombre ("Antena7" -> "Antena 7 HD").
    propios = {clave_nombre(c["api_name"]) for c in CHANNELS if c.get("api_name")}
    enlaces_propios = {u for c in CHANNELS
                       for u in [c["url"]] + c.get("alternatives", [])}
    respaldos = {clave_nombre(c["api_name"]): c for c in RESPALDOS}
    canales = [dict(c) for c in CHANNELS]
    for c in canales:
        if c.get("stream_page"):
            resolver_desde_pagina(c)

    if data is None:
        return ordenar(canales + [dict(c) for c in RESPALDOS])

    importados = 0
    vistos = set()
    for c in data.get("channels", []):
        if c.get("category") != TVABIERTA_CATEGORY or not c.get("enabled", True):
            continue
        nombre = (c.get("name") or "").strip()
        stream = (c.get("stream") or "").strip()
        if not nombre or not stream:
            continue
        clave = clave_nombre(nombre)
        if clave in propios or stream in enlaces_propios:
            continue  # ya lo tenemos con un enlace mejor
        if clave in NO_DOMINICANOS:
            continue  # va con los demas: ver tvabierta_extra()

        canal = {
            "name": bonito(nombre),
            "api_name": nombre,
            "number": numero_canal(c),
            "url": stream,
            "logo": c.get("logo") or "",
        }
        # Canal con respaldo: enlace y logo de la API, nombre y numero nuestros.
        if clave in respaldos:
            ref = respaldos[clave]
            canal["name"] = ref["name"]
            canal["number"] = ref.get("number") or canal["number"]
            canal["logo"] = canal["logo"] or ref.get("logo", "")
            vistos.add(clave)
        canales.append(canal)
        importados += 1

    log("API tvabierta: %d canales importados de la categoria %s"
        % (importados, TVABIERTA_CATEGORY))

    for clave, ref in respaldos.items():
        if clave in vistos:
            log("  %s: enlace de la API" % ref["name"])
        else:
            warn("%s no esta en la API; se usa el respaldo %s"
                 % (ref["name"], ref["url"]))
            canales.append(dict(ref))

    return ordenar(canales)


def bloque(nombre, url, grupo, logo="", tvg_id="", pais=""):
    """Bloque M3U sin cabeceras: #EXTINF + enlace."""
    attrs = []
    if tvg_id:
        attrs.append('tvg-id="%s"' % tvg_id)
    if logo:
        attrs.append('tvg-logo="%s"' % logo)
    if pais:
        attrs.append('tvg-country="%s"' % pais.upper())
    attrs.append('group-title="%s"' % grupo)
    return ["#EXTINF:-1 %s,%s" % (" ".join(attrs), nombre), url]


def build_block(channel):
    """Bloque M3U. Ningun canal necesita cabeceras: la entrada queda limpia."""
    return bloque(channel["name"], channel["url"], GROUP, channel.get("logo", ""))


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


def block_url(block):
    """Enlace del bloque: la primera linea que no es comentario."""
    return next((l.strip() for l in block[1:] if l.strip() and not l.startswith("#")), "")


def necesita_cabeceras(block):
    """El canal pide Referer/User-Agent (ver proxificar_bloque)."""
    return bool(re.search(r'\shttp-(?:referrer|user-agent)="[^"]+"', block[0])
                or any(l.upper().startswith("#EXTVLCOPT:HTTP-") for l in block[1:])
                or "|" in block_url(block))


def con_nombre(extinf, nombre):
    """Cambia el nombre del canal (lo que va tras la coma de los atributos)."""
    m = re.match(r'^(#EXTINF:[^\s,]*(?:\s+[\w-]+="[^"]*")*\s*,)', extinf)
    return (m.group(1) if m else extinf.rsplit(",", 1)[0] + ",") + nombre


# ----------------------------------------------------------------------
# Otras fuentes
# ----------------------------------------------------------------------


def cargar_freetv():
    """
    Bloques de los grupos FREETV_GRUPOS de Free-TV, con la categoria al estilo
    de iptv-org para que clasificar() los reparta igual. Sin los de YouTube
    (Ⓨ: Smarters no los abre); Ⓖ pasa a "[Geo-blocked]" como en iptv-org.
    """
    try:
        text = http_get(FREETV_URL, timeout=60).decode("utf-8", "replace")
    except RuntimeError as e:
        warn("no se pudo descargar Free-TV (%s); se sigue sin ella" % e)
        return []
    salida = []
    for block in parse_blocks(text)[1]:
        grupo = extinf_attr(block[0], "group-title")
        if grupo not in FREETV_GRUPOS:
            continue
        nombre = extinf_nombre(block[0])
        host = urllib.parse.urlsplit(block_url(block)).hostname or ""
        if "Ⓨ" in nombre or re.search(r"(^|\.)(youtube\.com|youtu\.be)$", host):
            continue
        nombre = nombre.replace("Ⓢ", "").replace("Ⓖ", " [Geo-blocked]")
        nombre = re.sub(r"\s+", " ", nombre).strip()
        extinf = con_grupo(con_nombre(block[0], nombre),
                           "News" if grupo.startswith("News") else "General")
        salida.append([extinf] + block[1:])
    return salida


def tvabierta_extra(data):
    """
    Bloques de las categorias TVABIERTA_EXTRA de la API, mas los que la API
    mete en RD sin ser dominicanos (NO_DOMINICANOS). Sin repetidos dentro de
    la propia API (trae varios Stingray dos veces).
    """
    if data is None:
        return []
    salida, vistos = [], set()
    for c in data.get("channels", []):
        nombre = (c.get("name") or "").strip()
        stream = (c.get("stream") or "").strip()
        if not nombre or not stream or not c.get("enabled", True):
            continue
        if c.get("category") == TVABIERTA_CATEGORY:
            categoria = NO_DOMINICANOS.get(clave_nombre(nombre))
            if not categoria:
                continue
            cc = ""
        elif c.get("category") in TVABIERTA_EXTRA:
            categoria, cc = TVABIERTA_EXTRA[c["category"]]
        else:
            continue
        nombre = bonito(nombre)
        if clave_canal(nombre) in vistos or clave_enlace(stream) in vistos:
            continue
        vistos.update((clave_canal(nombre), clave_enlace(stream)))
        salida.append(bloque(nombre, stream, categoria, c.get("logo") or "",
                             (c.get("tvg_id") or "").strip(), cc))
    return salida


def leer_mjh(url):
    """JSON comprimido de i.mjh.nz (.channels.json.gz), o None si falla."""
    try:
        return json.loads(gzip.decompress(http_get(url, timeout=60)).decode("utf-8"))
    except (RuntimeError, ValueError, OSError) as e:
        warn("no se pudo leer %s (%s); se sigue sin esos canales" % (url, e))
        return None


def cargar_pluto(proxy):
    """Grupo Pluto TV: regiones PLUTO_REGIONES, cada canal por /pluto/ del Worker."""
    if not proxy:
        warn("sin proxy no hay Pluto TV: sus canales necesitan el Worker")
        return []
    data = leer_mjh(PLUTO_CHANNELS)
    if not data:
        return []
    canales = {}
    for i, region in enumerate(PLUTO_REGIONES):
        for cid, c in data.get("regions", {}).get(region, {}).get("channels", {}).items():
            if re.match(r"^[0-9a-f]{24}$", cid) and cid not in canales:
                canales[cid] = (i, c.get("chno") or 9999, c)
    orden = sorted(canales.items(), key=lambda kv: kv[1][:2])
    return [bloque(c["name"], "%s/pluto/%s.m3u8" % (proxy, cid), PLUTO_GROUP,
                   c.get("logo") or "", cid)
            for cid, (_, _, c) in orden]


def cargar_samsung():
    """Grupo Samsung TV Plus (SAMSUNG_REGION), sin los canales con DRM."""
    data = leer_mjh(SAMSUNG_CHANNELS)
    if not data:
        return []
    slug = data.get("slug") or "stvp-{id}"
    canales = data.get("regions", {}).get(SAMSUNG_REGION, {}).get("channels", {})
    orden = sorted(canales.items(), key=lambda kv: kv[1].get("chno") or 9999)
    return [bloque(c["name"], "https://jmp2.uk/" + slug.replace("{id}", cid),
                   SAMSUNG_GROUP, c.get("logo") or "", cid, SAMSUNG_REGION)
            for cid, c in orden if not c.get("license_url")]


# ----------------------------------------------------------------------
# Duplicados
# ----------------------------------------------------------------------


class Duplicados:
    """
    Recuerda los canales ya puestos en la lista para no repetirlos: los de
    DOMINICANOS (por nombre, contra los de RD, y por enlace) y los de cada
    fuente anterior (por tvg-id, enlace o nombre + pais). Una fuente no se
    compara consigo misma: iptv-org trae a proposito varias versiones de un
    canal (720p, 1080p, otra senal).
    """

    def __init__(self, dominicanos):
        self.dom_clave, self.dom_enlace = {}, {}
        for ch in dominicanos:
            for n in (ch["name"], ch.get("api_name")):
                if n:
                    self.dom_clave.setdefault(clave_canal(n), ch)
            for u in [ch["url"]] + ch.get("alternatives", []):
                self.dom_enlace.setdefault(clave_enlace(u), ch)
        self.ids, self.enlaces, self.nombres, self.nombres_pais = set(), set(), set(), set()
        self._nuevos = []

    def dominicano(self, block):
        """Canal de DOMINICANOS del que el bloque es duplicado, o None."""
        enlace = clave_enlace(block_url(block))
        if enlace in self.dom_enlace:
            return self.dom_enlace[enlace]
        if pais(block[0]) == "do":
            return self.dom_clave.get(clave_canal(extinf_nombre(block[0])))
        return None

    def repetido(self, block):
        """Ya esta en una fuente anterior. Si no, lo apunta para las siguientes."""
        tid = block_tvg_id(block)
        enlace = clave_enlace(block_url(block))
        clave = clave_canal(extinf_nombre(block[0]))
        cc = pais(block[0])
        # Con pais: igual nombre en ese pais o en uno sin pais (los de las
        # categorias de tvabierta). Sin pais: igual nombre en cualquiera.
        if (tid and tid in self.ids) or enlace in self.enlaces or (clave and (
                (cc and ((clave, cc) in self.nombres_pais or (clave, "") in self.nombres_pais))
                or (not cc and clave in self.nombres))):
            return True
        self._nuevos.append((tid, enlace, clave, cc))
        return False

    def fin_de_fuente(self):
        """Lo visto en la fuente que acaba pasa a contar para las siguientes."""
        for tid, enlace, clave, cc in self._nuevos:
            if tid:
                self.ids.add(tid)
            self.enlaces.add(enlace)
            if clave:
                self.nombres.add(clave)
                self.nombres_pais.add((clave, cc))
        self._nuevos = []


# ----------------------------------------------------------------------
# Canales con cabeceras -> Worker
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Categorias
# ----------------------------------------------------------------------


def extinf_nombre(extinf):
    """Nombre del canal: lo que va tras la coma que cierra los atributos."""
    m = re.match(r'^#EXTINF:[^\s,]*(?:\s+[\w-]+="[^"]*")*\s*,(.*)$', extinf)
    return (m.group(1) if m else extinf.rsplit(",", 1)[-1]).strip()


def clasificar(extinf):
    """Grupo en espanol: el de mas prioridad entre categorias y nombre."""
    candidatos = {CATEGORIAS.get(c.strip().lower(), GENERALES)
                  for c in extinf_attr(extinf, "group-title").split(";")}
    nombre = extinf_nombre(extinf)
    candidatos.update(g for g, rx in PALABRAS if rx.search(nombre))
    return min(candidatos, key=PRIORIDAD.index)


def pais(extinf):
    """'Canal13.cl@SD' -> 'cl'; si el tvg-id no lo dice, el tvg-country."""
    m = re.search(r"\.([a-z]{2})$", normalize_id(extinf_attr(extinf, "tvg-id")) or "")
    if m:
        return m.group(1)
    cc = extinf_attr(extinf, "tvg-country").lower()
    return cc if re.match(r"^[a-z]{2}$", cc) else ""


def con_grupo(extinf, grupo):
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', lambda _: 'group-title="%s"' % grupo,
                      extinf, count=1)
    return re.sub(r"^(#EXTINF:\S*)", lambda m: '%s group-title="%s"' % (m.group(1), grupo),
                  extinf, count=1)


def agrupar(blocks):
    """
    Pone a cada bloque su grupo en espanol y los devuelve ordenados por grupo
    (ORDEN_GRUPOS y luego los Generales por pais), respetando el orden
    original dentro de cada grupo. Devuelve (bloques, {grupo: cantidad}).
    """
    clasificados = []
    por_pais = {}
    for block in blocks:
        grupo = clasificar(block[0])
        cc = pais(block[0]) if grupo == GENERALES else ""
        if grupo == GENERALES:
            por_pais[cc] = por_pais.get(cc, 0) + 1
        clasificados.append((grupo, cc, block))

    def nombre_pais(cc):
        if cc in PAISES and por_pais.get(cc, 0) >= MIN_POR_PAIS:
            return PAISES[cc]
        return OTROS_PAISES

    # RD primero, el resto por orden alfabetico, "Otros" al final.
    paises = sorted({nombre_pais(cc) for g, cc, _ in clasificados if g == GENERALES},
                    key=lambda p: (p != PAISES["do"], p == OTROS_PAISES, p))
    orden = ORDEN_GRUPOS + ["%s - %s" % (GENERALES, p) for p in paises]

    salida = {g: [] for g in orden}
    for grupo, cc, block in clasificados:
        if grupo == GENERALES:
            grupo = "%s - %s" % (GENERALES, nombre_pais(cc))
        salida.setdefault(grupo, []).append([con_grupo(block[0], grupo)] + block[1:])

    bloques = [b for g in salida for b in salida[g]]
    return bloques, {g: len(v) for g, v in salida.items() if v}


def b64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def token_cabeceras(key, origen, referer, ua):
    """Mismo token que hdrPath() de worker.js: <payload>.<firma HMAC>."""
    campos = [origen, referer or ""] + ([ua] if ua else [])
    payload = b64url(json.dumps(campos, separators=(",", ":")).encode("utf-8"))
    firma = hmac.new(key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256)
    return payload + "." + b64url(firma.digest()[:HDR_SIG_BYTES])


def extinf_attr(extinf, nombre):
    m = re.search(r'\s%s="([^"]*)"' % re.escape(nombre), extinf)
    return m.group(1).strip() if m else ""


def proxificar_bloque(block, proxy, key):
    """
    Si el canal pide Referer/User-Agent, devuelve el bloque con la URL cambiada
    por la del Worker y sin las cabeceras (ya las pone el Worker). Si no, o si
    la URL no es http(s), devuelve el bloque tal cual. Mismo criterio que
    rewriteEntry() de worker.js.
    """
    extinf = block[0]
    referer = extinf_attr(extinf, "http-referrer")
    ua = extinf_attr(extinf, "http-user-agent")

    resto, enlace, despues = [], None, []
    for line in block[1:]:
        if enlace is not None:
            despues.append(line)
            continue
        m = re.match(r"^#EXTVLCOPT:http-(referrer|user-agent)=(.*)$", line, re.I)
        if m:
            if m.group(1).lower() == "referrer":
                referer = referer or m.group(2).strip()
            else:
                ua = ua or m.group(2).strip()
        elif line.strip() and not line.startswith("#"):
            enlace = line.strip()
        else:
            resto.append(line)

    if enlace is None:
        return block, False

    # Sufijo estilo Kodi: https://...m3u8|Referer=...&User-Agent=...
    destino = enlace
    if "|" in enlace:
        destino, opciones = enlace.split("|", 1)
        for kv in opciones.split("&"):
            k, sep, v = kv.partition("=")
            if not sep:
                continue
            v = urllib.parse.unquote(v)
            if k.lower() in ("referer", "referrer"):
                referer = referer or v
            elif k.lower() == "user-agent":
                ua = ua or v

    partes = urllib.parse.urlsplit(destino)
    if (not referer and not ua) or partes.scheme.lower() not in ("http", "https") \
            or not partes.netloc:
        return block, False

    origen = "%s://%s" % (partes.scheme.lower(), partes.netloc)
    ruta = (partes.path or "/") + ("?" + partes.query if partes.query else "")
    limpio = re.sub(r'\s+http-(?:referrer|user-agent)="[^"]*"', "", extinf)
    nuevo = "%s/h/%s%s" % (proxy, token_cabeceras(key, origen, referer, ua), ruta)
    return [limpio] + resto + [nuevo] + despues, True


# ----------------------------------------------------------------------
# Verificacion
# ----------------------------------------------------------------------


def check_stream(channel):
    """master playlist -> variante -> segmento .ts. Devuelve (ok, mensaje)."""
    try:
        master, base = http_get(channel["url"], timeout=15, retries=2, con_url=True)
        master = master.decode("utf-8", "replace")
    except RuntimeError as e:
        return False, "playlist: %s" % e

    if "#EXTM3U" not in master:
        return False, "la respuesta no es un M3U8"

    lines = [l.strip() for l in master.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return False, "playlist vacia (canal fuera del aire?)"

    # Si es un master, lines[0] es una variante; si es una media playlist
    # directa, es un segmento y vale el ultimo (ventana deslizante).
    # Las rutas relativas van contra la URL final: tras una redireccion no es
    # la de la lista.
    primero = lines[0] if ".m3u8" in lines[0] else lines[-1]
    target = urllib.parse.urljoin(base, primero)

    if ".m3u8" in target:
        try:
            media, base = http_get(target, timeout=15, retries=2, con_url=True)
            media = media.decode("utf-8", "replace")
        except RuntimeError as e:
            return False, "variante: %s" % e
        segs = [l.strip() for l in media.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            return False, "sin segmentos (canal fuera del aire?)"
        # El ULTIMO, no el primero: el mas antiguo puede haber expirado ya.
        target = urllib.parse.urljoin(base, segs[-1])

    try:
        data = http_get(target, timeout=25, retries=2)
    except RuntimeError as e:
        return False, "segmento: %s" % e

    if len(data) < 10000:
        return False, "segmento sospechosamente pequeno (%d bytes)" % len(data)

    return True, "%.1f KB de video" % (len(data) / 1024.0)


def anadir_alternativa(ch, block, proxy, key):
    """
    Deja el enlace del bloque (un duplicado de otra fuente) como alternativa
    del canal ch, si es otro enlace. Si pide cabeceras, solo con el Worker.
    """
    if necesita_cabeceras(block):
        if not key:
            return
        block, _ = proxificar_bloque(block, proxy, key)
    url = block_url(block)
    actuales = [ch["url"]] + ch.get("alternatives", [])
    if url and clave_enlace(url) not in {clave_enlace(u) for u in actuales}:
        # Lista nueva: ch es una copia de CHANNELS que comparte la de alli.
        ch["alternatives"] = ch.get("alternatives", []) + [url]


def check_with_alternatives(channel):
    """
    Como check_stream, pero si el enlace principal falla prueba los de
    "alternatives" y deja en el canal el primero que funcione.
    """
    ok, msg = check_stream(channel)
    if ok:
        return ok, msg
    # 403 = el CDN bloquea al runner, no al canal: se publica el principal.
    # Cualquier otro fallo (caido, 404, sin segmentos) sigue a la alternativa.
    if channel.get("ignorar_403") and "HTTP 403" in msg:
        return True, "sin verificar (%s: bloqueo a GitHub, se publica igual)" % msg
    for alt in channel.get("alternatives", []):
        ok_alt, msg_alt = check_stream({"name": channel["name"], "url": alt})
        if ok_alt:
            # Como aviso para que salga en las anotaciones del workflow: el
            # log del job pide login, las anotaciones se leen sin el.
            warn("%s: el enlace principal fallo (%s); se publica la alternativa %s"
                 % (channel["name"], msg, alt))
            channel["url"] = alt
            return True, "%s con la alternativa (principal: %s)" % (msg_alt, msg)
    return ok, msg


def run_checks(canales):
    """
    Verifica en paralelo. Con ~95 canales en serie esto tardaria varios minutos
    y se comeria el timeout del workflow; con hilos baja a menos de un minuto.
    """
    log("Verificando %d enlaces..." % len(canales))
    with ThreadPoolExecutor(max_workers=12) as pool:
        resultados = list(pool.map(check_with_alternatives, canales))

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

    data = leer_tvabierta()
    canales = cargar_canales(data)

    key = PROXY_KEY
    if key and not proxy:
        warn("hay PROXY_KEY pero no proxy: los canales con Referer van sin Worker")
        key = ""
    elif not key:
        warn("sin PROXY_KEY: los canales de iptv-org que piden Referer/User-Agent "
             "se publican sin Worker y no funcionaran en Smarters")

    # Antes de verificar: los duplicados de DOMINICANOS dejan su enlace como
    # alternativa y el --check puede usarlo.
    base, pluto, samsung, fuentes = [], [], [], []
    proxificados = 0
    grupos = {}
    if not args.no_base:
        log("Descargando lista base de iptv-org (espanol)...")
        try:
            text = http_get(SOURCE_URL, timeout=60).decode("utf-8", "replace")
        except RuntimeError as e:
            error("no se pudo descargar la lista base: %s" % e)
            return 1
        pluto = cargar_pluto(proxy)

        dup = Duplicados(canales)
        for nombre, blocks in [("iptv-org", parse_blocks(text)[1]),
                               ("Free-TV", cargar_freetv()),
                               ("tvabierta", tvabierta_extra(data))]:
            n = {"total": len(blocks), "nuevos": 0, "rd": 0, "repetidos": 0, "pluto": 0}
            for block in blocks:
                if pluto and "jmp2.uk/plu-" in block_url(block):
                    n["pluto"] += 1  # rotos; van en el grupo Pluto TV
                    continue
                ch = dup.dominicano(block)
                if ch:
                    n["rd"] += 1
                    anadir_alternativa(ch, block, proxy, key)
                    continue
                if dup.repetido(block):
                    n["repetidos"] += 1
                    continue
                if key:
                    block, cambiado = proxificar_bloque(block, proxy, key)
                    proxificados += cambiado
                base.append(block)
                n["nuevos"] += 1
            dup.fin_de_fuente()
            fuentes.append((nombre, n))
            log("%s: %d canales, %d nuevos, %d ya en %s, %d repetidos%s"
                % (nombre, n["total"], n["nuevos"], n["rd"], GROUP, n["repetidos"],
                   ", %d de Pluto quitados" % n["pluto"] if n["pluto"] else ""))
        samsung = [b for b in cargar_samsung() if not dup.repetido(b)]

        base, grupos = agrupar(base)
        for grupo, lista in ((PLUTO_GROUP, pluto), (SAMSUNG_GROUP, samsung)):
            if lista:
                grupos[grupo] = len(lista)
        log("Con cabeceras pasados por el Worker: %d" % proxificados)
        for grupo, cuantos in grupos.items():
            log("  %5d  %s" % (cuantos, grupo))

    filas, fallos = [], 0
    if args.check:
        ok, filas, fallos = run_checks(canales)
        if args.drop_broken:
            canales = ok

    if pluto or samsung:
        out_lines = ['#EXTM3U x-tvg-url="%s"' % ",".join(EPG_URLS)]
    else:
        out_lines = ["#EXTM3U"]
    for ch in canales:
        out_lines.extend(build_block(ch))
    for block in base + pluto + samsung:
        out_lines.extend(block)

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

    total = len(canales) + len(base) + len(pluto) + len(samsung)
    log("Grupo %s: %d canales" % (GROUP, len(canales)))
    log("Total en la lista: %d" % total)
    log("Lista generada: %s" % destino)

    resumen = ["## Lista IPTV", "",
               "- Grupo `%s`: **%d** canales" % (GROUP, len(canales))]
    resumen += ["- %s: **%d** nuevos de %d (%d ya en %s, %d repetidos%s)"
                % (nombre, n["nuevos"], n["total"], n["rd"], GROUP, n["repetidos"],
                   ", %d de Pluto quitados" % n["pluto"] if n["pluto"] else "")
                for nombre, n in fuentes]
    resumen += ["- %s: **%d**" % (PLUTO_GROUP, len(pluto)),
                "- %s: **%d**" % (SAMSUNG_GROUP, len(samsung)),
                "- Con Referer/User-Agent por el Worker: **%d**" % proxificados,
                "- Total: **%d**" % total]
    if grupos:
        resumen += ["", "### Grupos", "", "| Grupo | Canales |", "| --- | --- |"] + \
                   ["| %s | %d |" % (g, n) for g, n in grupos.items()]
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
  
