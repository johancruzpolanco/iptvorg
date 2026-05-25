# Mi lista IPTV personalizada

Esta lista sigue automaticamente la lista en espanol (`spa.m3u`) de
[iptv-org](https://github.com/iptv-org/iptv), pero reemplaza el canal
**Telecentro (Republica Dominicana)** por el enlace oficial de Telemicro
en 1080p, que es de mejor calidad.

Se actualiza sola cada 12 horas siguiendo a iptv-org.

---

## Como montarlo (paso a paso)

### 1. Crear el repositorio en GitHub
1. Entra a https://github.com y crea una cuenta (gratis) si no tienes.
2. Pulsa el **+** arriba a la derecha → **New repository**.
3. Ponle un nombre, por ejemplo `mi-iptv`.
4. Marcalo como **Public**.
5. Crea el repositorio.

### 2. Subir estos archivos
Sube al repositorio (boton **Add file → Upload files**, o arrastrando):
- `build_list.py`
- `.github/workflows/update.yml`  (respeta esa ruta de carpetas)
- `README.md`  (este archivo, opcional)

> Importante: la carpeta `.github/workflows/` debe escribirse tal cual,
> con el punto al inicio. Si la subes por la web, al crear el archivo
> escribe el nombre completo: `.github/workflows/update.yml` y GitHub
> creara las carpetas solo.

### 3. Activar y ejecutar la accion
1. Ve a la pestana **Actions** del repositorio.
2. Si te pide habilitar los workflows, acepta.
3. Selecciona el workflow **"Actualizar lista IPTV"**.
4. Pulsa **Run workflow** para generarla por primera vez.
5. Espera ~1 minuto. Aparecera un archivo nuevo: `lista.m3u`.

A partir de ahi se actualiza sola cada 12 horas.

### 4. Obtener el enlace para tu app
1. Abre el archivo `lista.m3u` en tu repositorio.
2. Pulsa el boton **Raw**.
3. Copia la URL de la barra del navegador. Sera algo asi:

```
https://raw.githubusercontent.com/TU_USUARIO/mi-iptv/main/lista.m3u
```

### 5. Cargarla en tu app de IPTV
En tu app (TiviMate, IPTV Smarters, OTT Navigator, la app de iptv-org, etc.):
- Busca "Anadir lista" / "Add playlist" / "M3U URL".
- Pega el enlace **Raw** del paso anterior.
- Listo.

---

## Personalizar

Todo se configura al inicio de `build_list.py`:

- **Cambiar la lista base**: edita `SOURCE_URL`. Ejemplos:
  - Espanol: `https://iptv-org.github.io/iptv/languages/spa.m3u`
  - Republica Dominicana: `https://iptv-org.github.io/iptv/countries/do.m3u`
  - Global: `https://iptv-org.github.io/iptv/index.m3u`
- **Cambiar el enlace bueno**: edita `GOOD_URL`.
- **Si el canal NO necesita referrer/user-agent**: deja `GOOD_OPTS = []`.
- **Cambiar la frecuencia**: edita el `cron` en `.github/workflows/update.yml`.

## Nota sobre el referrer
Algunas apps no respetan las opciones `#EXTVLCOPT` (referrer / user-agent).
Las apps basadas en VLC (como la oficial de iptv-org) si las respetan.
Si el canal no reproduce en tu app, prueba con una basada en VLC.
