#!/usr/bin/env python3
"""
Descargador de archivos .torrent desde páginas de listado de un catálogo web.

Cloudflare sirve un "managed challenge" que no pasa con curl_cffi ni con un
Chrome lanzado por Playwright (lo detecta por el CDP de automatización). La
solución es lanzar Chrome real de forma independiente con
--remote-debugging-port y conectar Playwright por connect_over_cdp: así el
navegador no lleva marcas de automatización y el challenge se resuelve solo.

El DNS se resuelve por DNS-over-HTTPS (1.1.1.1) y se fuerza dentro del
navegador para saltarse bloqueos de DNS del ISP.
"""

import argparse
import base64
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests
from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------
# URL de ejemplo que aparece en la ayuda. Sustitúyela por la página real del listado.
URL_EJEMPLO = "https://ejemplo.com/serie/000000/Titulo-de-la-serie-720p"
CARPETA_SALIDA = "torrents"

# Perfil persistente de Chrome: conserva cookies (cf_clearance) entre ejecuciones,
# de modo que el navegador parece un visitante habitual y Cloudflare desafía menos.
CARPETA_PERFIL = os.path.join(os.path.expanduser("~"), ".downtorrent", "chrome-profile")

DOH_URL = "https://1.1.1.1/dns-query"

# Marcas del título mientras Cloudflare está verificando (en varios idiomas).
CHALLENGE_MARKERS = ("moment", "checking", "verificando", "espera", "attention")

# Posibles rutas del binario de Chrome/Chromium (macOS, Linux y Windows).
# En Windows las variables de entorno (%ProgramFiles%, etc.) se expanden al comprobar.
RUTAS_NAVEGADOR = (
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    # Windows
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Chromium\Application\chrome.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
)

# Caracteres no permitidos en nombres de archivo de Windows.
CARACTERES_INVALIDOS_NOMBRE = '<>:"/\\|?*'


def doh_resolve(host, timeout=10):
    """Resuelve los registros A de un host vía DNS-over-HTTPS (Cloudflare)."""
    try:
        r = cffi_requests.get(
            DOH_URL,
            params={"name": host, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"No se pudo resolver {host} vía DoH: {e}") from e

    ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    if not ips:
        raise RuntimeError(f"{host} no tiene registros A")
    return ips[0]


def _base_domain(host):
    partes = host.split(".")
    return host if len(partes) <= 2 else ".".join(partes[-2:])


def construir_host_resolver_rules(hosts):
    """Genera --host-resolver-rules mapeando cada host a su IP real por DoH.

    Solo se fuerzan los dominios del sitio de torrents. Los dominios del
    challenge de Cloudflare se dejan al DNS normal: forzarlos a una IP
    concreta puede romper el widget de verificación y provocar el bucle.
    """
    reglas = []
    for host in sorted(hosts):
        try:
            ip = doh_resolve(host)
        except RuntimeError as e:
            print(f"⚠️  {e}")
            continue
        reglas.append(f"MAP {host} {ip}")
        base = _base_domain(host)
        if base != host:
            reglas.append(f"MAP *.{base} {ip}")
    return ", ".join(reglas)


def _ruta_navegador(executable=None):
    if executable:
        if os.path.exists(executable):
            return executable
        raise FileNotFoundError(f"No existe el ejecutable indicado: {executable}")

    for ruta in RUTAS_NAVEGADOR:
        ruta = os.path.expandvars(ruta)
        if os.path.exists(ruta):
            return ruta
    for nombre in ("google-chrome", "chromium", "chromium-browser",
                   "chrome", "msedge", "brave"):
        ruta = shutil.which(nombre)
        if ruta:
            return ruta
    return None


def _puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lanzar_chrome(ruta, reglas, headless):
    """Lanza Chrome real con depuración remota y devuelve (proceso, puerto)."""
    os.makedirs(CARPETA_PERFIL, exist_ok=True)
    puerto = _puerto_libre()
    args = [
        ruta,
        f"--remote-debugging-port={puerto}",
        f"--user-data-dir={CARPETA_PERFIL}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if reglas:
        args.append(f"--host-resolver-rules={reglas}")
    if headless:
        args.append("--headless=new")
    args.append("about:blank")

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, puerto


def _cerrar_chrome(proc):
    """Cierra Chrome y sus procesos hijos (en Windows hace falta taskkill /T)."""
    try:
        if os.name == "nt" and proc.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            proc.terminate()
    except Exception:
        pass

    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _esperar_puerto(puerto, timeout=20):
    url = f"http://127.0.0.1:{puerto}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _es_challenge(titulo):
    t = titulo.lower()
    return any(m in t for m in CHALLENGE_MARKERS)


def _esperar_challenge(page, timeout):
    """Espera a que el título deje de ser el del challenge de Cloudflare."""
    inicio = time.monotonic()
    while time.monotonic() - inicio < timeout:
        try:
            titulo = page.title()
        except Exception:
            titulo = ""
        if titulo and not _es_challenge(titulo):
            return True
        time.sleep(2)
    print(f"⚠️  El challenge de Cloudflare no se resolvió en {timeout}s.")
    print("    Si estás detrás de una VPN o IP de datacenter, desconéctala y reintenta.")
    return False


def _extraer_enlaces_torrent(page):
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    enlaces = []
    for href in hrefs:
        if href and href.lower().endswith(".torrent") and href not in enlaces:
            enlaces.append(href)
    return enlaces


def _nombre_seguro(nombre):
    """Sustituye los caracteres no válidos en nombres de archivo de Windows."""
    limpio = "".join(
        "_" if c in CARACTERES_INVALIDOS_NOMBRE else c for c in nombre
    ).strip(" .")
    return limpio


def _descargar_torrent(page, url, carpeta_salida, idx, total):
    nombre = _nombre_seguro(os.path.basename(urlparse(url).path)) or f"archivo_{idx}.torrent"
    ruta = os.path.join(carpeta_salida, nombre)

    if os.path.exists(ruta) and os.path.getsize(ruta) > 0:
        print(f"⏭️  ({idx}/{total}) Ya existe: {nombre}")
        return

    print(f"⬇️  ({idx}/{total}) {url}")
    try:
        # fetch desde el contexto de la página: reutiliza cookies/clearance y
        # la resolución DNS forzada del navegador.
        data_b64 = page.evaluate(
            """
            async (url) => {
                const r = await fetch(url, { credentials: 'include' });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                return btoa(bin);
            }
            """,
            url,
        )
        datos = base64.b64decode(data_b64)
    except Exception as e:
        print(f"   ❌ Error al descargar: {e}")
        return

    if not datos or datos.lstrip()[:1] == b"<":
        print("   ⚠️  La respuesta parece HTML (posible challenge), se omite.")
        return
    if len(datos) < 50:
        print(f"   ⚠️  Archivo sospechosamente pequeño ({len(datos)} bytes), se omite.")
        return

    with open(ruta, "wb") as f:
        f.write(datos)
    print(f"   ✅ Guardado: {ruta} ({len(datos)} bytes)")


def descargar_torrents(urls, carpeta_salida=CARPETA_SALIDA, headless=False,
                       executable=None, timeout=60):
    """Descarga los .torrent enlazados en cada una de las páginas indicadas."""
    os.makedirs(carpeta_salida, exist_ok=True)

    ruta_navegador = _ruta_navegador(executable)
    if not ruta_navegador:
        print("❌ No se encontró Chrome/Chromium. Usa --executable con la ruta del binario.")
        return

    hosts = set()
    for url in urls:
        host = urlparse(url).hostname
        if host:
            hosts.add(host)
    reglas = construir_host_resolver_rules(hosts)

    proc, puerto = _lanzar_chrome(ruta_navegador, reglas, headless)
    try:
        if not _esperar_puerto(puerto):
            print("❌ Chrome no respondió en el puerto de depuración.")
            print("   Si hay una ventana de Chrome anterior abierta por el script, ciérrala y reintenta.")
            return

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{puerto}")
            try:
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                page.set_viewport_size({"width": 1366, "height": 900})

                for url in urls:
                    print(f"🔎 Abriendo: {url}")
                    try:
                        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                    except Exception as e:
                        print(f"❌ Error al abrir la página: {e}")
                        continue

                    if not _esperar_challenge(page, timeout):
                        continue

                    try:
                        page.wait_for_load_state("load", timeout=15000)
                    except PWTimeoutError:
                        pass
                    try:
                        page.wait_for_selector("a[href]", state="attached", timeout=10000)
                    except PWTimeoutError:
                        pass

                    enlaces = _extraer_enlaces_torrent(page)
                    if not enlaces:
                        print("⚠️  No se encontraron enlaces .torrent en esta página.")
                        continue

                    print(f"✅ Se encontraron {len(enlaces)} archivos .torrent.")
                    for i, url_torrent in enumerate(enlaces, start=1):
                        _descargar_torrent(page, url_torrent, carpeta_salida, i, len(enlaces))
            finally:
                browser.close()
    finally:
        _cerrar_chrome(proc)

    print("🎉 Proceso finalizado.")


def _configurar_salida_unicode():
    """Evita UnicodeEncodeError al imprimir emojis en consolas de Windows."""
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _configurar_salida_unicode()
    parser = argparse.ArgumentParser(
        description="Descarga los archivos .torrent enlazados en una o varias páginas web.",
        epilog=f"Ejemplo: %(prog)s '{URL_EJEMPLO}' -o {CARPETA_SALIDA}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "urls",
        nargs="+",
        help="Una o varias URLs de páginas desde las que descargar los .torrent.",
    )
    parser.add_argument(
        "-o",
        "--salida",
        default=CARPETA_SALIDA,
        help=f"Carpeta de destino (por defecto: {CARPETA_SALIDA}).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Ejecutar Chrome sin ventana. Menos fiable contra Cloudflare.",
    )
    parser.add_argument(
        "--executable",
        default=None,
        help="Ruta al binario de Chrome/Chromium si no se detecta automáticamente.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Segundos máximos esperando al challenge de Cloudflare (por defecto: 60).",
    )

    args = parser.parse_args()
    descargar_torrents(
        args.urls,
        carpeta_salida=args.salida,
        headless=args.headless,
        executable=args.executable,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
