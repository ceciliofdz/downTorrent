# downTorrent

Script de línea de comandos que descarga los archivos `.torrent` enlazados en una o
varias páginas de un catálogo web, sorteando el *managed challenge* de Cloudflare.

> **Aviso legal.** Esta herramienta solo descarga archivos de metadatos `.torrent`
> desde páginas públicas y no aloja, distribuye ni enlaza contenido protegido.
> Úsala únicamente con material sobre el que tengas derechos o que sea de dominio
> público, y respeta las condiciones de uso de los sitios que visites y la
> legislación de tu país. El autor no se responsabiliza del uso que se haga de ella.

## ¿Cómo funciona?

Un navegador lanzado por Playwright (o una petición con `curl_cffi`) es detectado por
Cloudflare y el challenge nunca se resuelve. La estrategia del script es la contraria:
lanzar un Chrome **real** como proceso independiente y conectarse a él por CDP.

1. **Chrome independiente.** Se arranca con `--remote-debugging-port` y un perfil
   persistente (`~/.downtorrent/chrome-profile`) que conserva las cookies
   (`cf_clearance`) entre ejecuciones, así que el navegador parece un visitante
   habitual. Sin marcas de automatización.
2. **Playwright por CDP.** Se conecta con `connect_over_cdp` al Chrome ya lanzado,
   en lugar de lanzarlo él mismo.
3. **DNS-over-HTTPS.** Los dominios del sitio se resuelven con DoH (1.1.1.1) y se
   fuerzan en el navegador con `--host-resolver-rules`, saltándose bloqueos de DNS
   del ISP. Los dominios del challenge de Cloudflare se dejan al DNS normal para no
   romper el widget de verificación.
4. **Espera del challenge.** Se vigila el título de la pestaña hasta que deja de
   contener los marcadores típicos (`moment`, `checking`, `verificando`, …).
5. **Descarga.** Se extraen los `a[href$=".torrent"]` y cada archivo se descarga con
   `fetch` desde el contexto de la página, reutilizando cookies y clearance. Se
   omiten los archivos que ya existen y se descartan respuestas HTML o de menos de
   50 bytes (síntoma de challenge sin resolver).

## Requisitos

- Python 3.9 o superior (probado con 3.11).
- Chrome, Chromium, Brave o Edge instalado en el sistema. **No** hace falta ejecutar
  `playwright install`: el script usa el navegador del sistema vía CDP.

## Instalación

```bash
git clone <URL_DEL_REPOSITORIO> downTorrent
cd downTorrent

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

## Uso

```bash
# Una página
python downtorrent.py "https://ejemplo.com/serie/000000/000000/Titulo-de-la-serie"

# Varias páginas a la vez
python downtorrent.py URL1 URL2 URL3

# Carpeta de destino distinta y más tiempo para el challenge
python downtorrent.py URL -o torrents --timeout 120

# Forzar un navegador concreto
python downtorrent.py URL --executable "/Applications/Chromium.app/Contents/MacOS/Chromium"
```

### Opciones

| Opción | Descripción | Por defecto |
| --- | --- | --- |
| `urls` | Una o varias URLs de páginas de las que extraer los `.torrent`. | — |
| `-o`, `--salida` | Carpeta donde se guardan los archivos descargados. | `torrents` |
| `--headless` | Ejecuta Chrome sin ventana. Menos fiable contra Cloudflare. | desactivado |
| `--executable` | Ruta al binario de Chrome/Chromium si no se detecta automáticamente. | autodetección |
| `--timeout` | Segundos máximos esperando a que se resuelva el challenge. | `60` |
| `-h`, `--help` | Muestra la ayuda. | — |

La lista de rutas de navegador que se prueban está en `RUTAS_NAVEGADOR`; si tu
navegador no aparece ahí, usa `--executable`.

## Salida

Los `.torrent` se guardan en la carpeta indicada (por defecto `torrents/`),
conservando el nombre de archivo del enlace. Los que ya existen con tamaño mayor que
cero se omiten, así que se puede relanzar el script sin volver a descargarlo todo.

## Estructura del proyecto

```
.
├── downtorrent.py      # script principal
├── requirements.txt    # dependencias
├── README.md
├── LICENSE
└── .gitignore
```

## Privacidad

El perfil de Chrome se guarda **fuera** del repositorio, en
`~/.downtorrent/chrome-profile`, y contiene cookies de sesión. No lo copies al
proyecto ni lo subas a ningún sitio.

## Problemas frecuentes

| Síntoma | Causa probable / solución |
| --- | --- |
| `El challenge de Cloudflare no se resolvió en Ns` | VPN o IP de datacenter, o `--headless`. Desactiva la VPN, prueba con ventana normal y sube `--timeout`. |
| `No se encontró Chrome/Chromium` | Ninguna ruta conocida coincide: indica el binario con `--executable`. |
| `Chrome no respondió en el puerto de depuración` | Quedó una instancia abierta con el mismo perfil; ciérrala (`pkill -f remote-debugging-port`) y reintenta. |
| `La respuesta parece HTML` | El challenge no estaba resuelto al descargar; reintenta subiendo `--timeout`. |
| No aparecen enlaces | La página cambió de estructura o no era una página de listado. |

## Licencia

Distribuido bajo licencia MIT. Consulta [LICENSE](LICENSE).
