# Lista IPTV - Canales de Republica Dominicana

Lista M3U con canales dominicanos verificados, para IPTV Smarters.

## Enlace para el reproductor

```
https://raw.githubusercontent.com/johancruzpolanco/iptvorg/main/lista.m3u
```

Alternativa por jsDelivr (mas rapido pero cachea; el workflow lo purga solo):

```
https://cdn.jsdelivr.net/gh/johancruzpolanco/iptvorg@main/lista.m3u
```

## Como funciona

`build_list.py` genera `lista.m3u`. El workflow lo ejecuta cada 12 horas,
commitea el resultado si cambio y purga el cache de jsDelivr.

```
python build_list.py --check   # genera y verifica cada enlace
```

`--check` no se conforma con pedir el playlist: sigue la cadena
master -> variante -> segmento y **descarga video real**, porque un playlist
puede devolver 200 mientras los segmentos fallan.

## El proxy

Los canales de Telemicro (Telecentro, Telemicro 5 y Digital 15) no se pueden
enlazar directo. El servidor exige cabecera `Referer` en el playlist **y** en
cada segmento, e IPTV Smarters no manda cabeceras propias: ignora las lineas
`#EXTVLCOPT` y tampoco entiende el sufijo `|Referer=...` en la URL. Por eso
pasan por un Cloudflare Worker (`worker.js`) que anade la cabecera, y las
entradas de la lista quedan limpias.

Los demas canales (Telesistema por Dailymotion, Colorvision y Teleuniverso por
`cdn3.wind.do`) no necesitan cabeceras y van directos, sin gastar cuota del
Worker.

`worker.js` esta aqui solo como copia de seguridad del codigo desplegado en
Cloudflare. No se despliega desde este repo.

## Cosas aprendidas (para no repetir errores)

- **Usar `live4.telemicro.com.do`, no `live2`.** `live2` reparte las peticiones
  entre dos backends: la sesion (`nimblesessionid`) se crea en uno y el
  segmento se pide al otro, dando 403/404 intermitentes. Medido sobre 5
  intentos de descargar un segmento real:

  | Endpoint | live2 | live4 |
  | --- | --- | --- |
  | `/live/13` | 0/5 | 5/5 |
  | `/live/55` | 2/5 | 5/5 |
  | `/live/digital15cast_1080p` | 4/5 | 5/5 |

  Los dos servidores sirven las mismas rutas, asi que da igual con cual te
  encuentres por ahi: en este repo todos van por `live4`.
- **El master de Teleuniverso miente.** Anuncia 1080/720/640 pero solo existe
  la de 720; por eso la lista apunta directo a la variante.
- **El enlace de Telesistema por Dailymotion lleva un token** (`sec2(...)`) que
  puede caducar. Si deja de funcionar, sustituir esa URL en `build_list.py`.
- **jsDelivr cachea hasta 7 dias en el cliente.** Sin purgar, se sigue viendo
  la lista vieja aunque el repo este actualizado.
