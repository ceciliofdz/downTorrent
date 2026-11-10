#!/usr/bin/env python3
"""
Descargador de archivos .torrent desde páginas de listado de un catálogo web.

Cloudflare sirve un "managed challenge" que no pasa con una petición HTTP normal
ni con un Chrome lanzado por Playwright (lo detecta por el CDP de automatización).
La solución es lanzar Chrome real de forma independiente con
--remote-debugging-port y conectar Playwright por connect_over_cdp: así el
navegador no lleva marcas de automatización y el challenge se resuelve solo.

El DNS se resuelve por DNS-over-HTTPS (1.1.1.1) y se fuerza dentro del
navegador para saltarse bloqueos de DNS del ISP.
"""

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import quote, urlparse

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


class DescargaCancelada(Exception):
    """La descarga se ha detenido a petición del usuario (interfaz gráfica)."""


def _comprobar_cancelado(cancelado):
    """Lanza DescargaCancelada si el evento de cancelación está activo."""
    if cancelado is not None and cancelado.is_set():
        raise DescargaCancelada()


def _dormir(segundos, cancelado=None):
    """time.sleep troceado para reaccionar rápido a la cancelación."""
    fin = time.monotonic() + segundos
    while True:
        _comprobar_cancelado(cancelado)
        restante = fin - time.monotonic()
        if restante <= 0:
            return
        time.sleep(min(0.2, restante))


def _notificar(callback, *args):
    """Llama a un callback de la interfaz sin que un fallo suyo rompa la descarga."""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        pass


def doh_resolve(host, timeout=10):
    """Resuelve los registros A de un host vía DNS-over-HTTPS (Cloudflare)."""
    url = f"{DOH_URL}?name={quote(host)}&type=A"
    try:
        peticion = urllib.request.Request(
            url, headers={"accept": "application/dns-json"}
        )
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            data = json.load(respuesta)
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


def detectar_navegador(executable=None):
    """Ruta del navegador que se usaría, o None si no se encuentra.

    Si ``executable`` apunta a una ruta inexistente lanza FileNotFoundError.
    """
    return _ruta_navegador(executable)


def _en_entorno_agente():
    """True si el proceso se lanzó desde la terminal del agente (DSH)."""
    return bool(os.environ.get("DSH_SESSION_ID") or os.environ.get("DSH_SHELL"))


def _ruta_log_chrome():
    return os.path.join(CARPETA_PERFIL, "downTorrent-chrome.log")


def _preparar_temp():
    """Redirige TMP/TEMP a una carpeta escribible para Playwright y Chrome.

    Algunos entornos (Python de Microsoft Store, perfiles restringidos) no
    permiten crear carpetas en %TEMP%, y Playwright/Chrome fallan entonces.
    """
    base_proyecto = os.path.dirname(os.path.abspath(__file__))
    candidatas = (
        os.path.join(base_proyecto, ".temp"),
        os.path.join(os.path.dirname(CARPETA_PERFIL), "temp"),
    )
    for carpeta in candidatas:
        prueba = os.path.join(carpeta, f".prueba-{os.getpid()}")
        try:
            os.makedirs(carpeta, exist_ok=True)
            with open(prueba, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(prueba)
        except OSError:
            continue
        for clave in ("TMP", "TEMP", "TMPDIR"):
            os.environ[clave] = carpeta
        tempfile.tempdir = carpeta
        return True
    return False


def comprobar_perfil_chrome():
    """Comprueba que el proceso puede escribir en el perfil de Chrome.

    Devuelve ``(True, None)`` si se puede escribir, o ``(False, detalle)`` con
    el error. Sirve para detectar entornos restringidos (sandbox, contenedor,
    terminal de un agente) en los que Chrome no llegará a arrancar.
    """
    ruta_prueba = os.path.join(CARPETA_PERFIL, f".escritura-{os.getpid()}")
    try:
        os.makedirs(CARPETA_PERFIL, exist_ok=True)
        with open(ruta_prueba, "w", encoding="utf-8") as f:
            f.write("ok")
        try:
            os.remove(ruta_prueba)
        except OSError:
            pass
        return True, None
    except Exception as exc:
        if isinstance(exc, PermissionError):
            return False, "sin permisos de escritura"
        return False, getattr(exc, "strerror", None) or str(exc)


def _leer_log_chrome(max_lineas=8):
    """Devuelve las últimas líneas del stderr de Chrome, si existe."""
    try:
        with open(_ruta_log_chrome(), encoding="utf-8", errors="replace") as f:
            lineas = [linea.strip() for linea in f if linea.strip()]
    except OSError:
        return ""
    return "\n".join(lineas[-max_lineas:])


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

    ruta_log = _ruta_log_chrome()
    try:
        log = open(ruta_log, "w", encoding="utf-8", errors="replace")
    except OSError:
        log = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=log if log is not None else subprocess.DEVNULL,
        )
    finally:
        if log is not None:
            log.close()
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


def _esperar_challenge(page, timeout, cancelado=None):
    """Espera a que el título deje de ser el del challenge de Cloudflare."""
    inicio = time.monotonic()
    while time.monotonic() - inicio < timeout:
        _comprobar_cancelado(cancelado)
        try:
            titulo = page.title()
        except Exception:
            titulo = ""
        if titulo and not _es_challenge(titulo):
            return True
        _dormir(2, cancelado)
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


def _descargar_torrent(page, url, carpeta_salida, idx, total, cancelado=None,
                       al_descargar=None):
    """Descarga un .torrent y devuelve "descargado", "omitido" o "error"."""
    _comprobar_cancelado(cancelado)

    nombre = _nombre_seguro(os.path.basename(urlparse(url).path)) or f"archivo_{idx}.torrent"
    ruta = os.path.join(carpeta_salida, nombre)

    if os.path.exists(ruta) and os.path.getsize(ruta) > 0:
        print(f"⏭️  ({idx}/{total}) Ya existe: {nombre}")
        return "omitido"

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
        return "error"

    if not datos or datos.lstrip()[:1] == b"<":
        print("   ⚠️  La respuesta parece HTML (posible challenge), se omite.")
        return "error"
    if len(datos) < 50:
        print(f"   ⚠️  Archivo sospechosamente pequeño ({len(datos)} bytes), se omite.")
        return "error"

    with open(ruta, "wb") as f:
        f.write(datos)
    print(f"   ✅ Guardado: {ruta} ({len(datos)} bytes)")
    _notificar(al_descargar, ruta, url)
    return "descargado"


def descargar_torrents(urls, carpeta_salida=CARPETA_SALIDA, headless=False,
                       executable=None, timeout=60, al_progreso=None,
                       cancelado=None, al_descargar=None):
    """Descarga los .torrent enlazados en cada una de las páginas indicadas.

    ``al_progreso(hechos, total)`` se invoca tras procesar cada archivo de la
    página actual (total 0 si la página no tenía enlaces). ``cancelado`` es un
    ``threading.Event`` opcional que permite detener la descarga desde la
    interfaz gráfica. ``al_descargar(ruta, url)`` se invoca tras guardar
    correctamente cada archivo. Devuelve un diccionario con el resumen.
    """
    resumen = {
        "descargados": 0,
        "omitidos": 0,
        "errores": 0,
        "cancelado": False,
        "error": None,
    }
    _configurar_salida_unicode()
    os.makedirs(carpeta_salida, exist_ok=True)

    ruta_navegador = _ruta_navegador(executable)
    if not ruta_navegador:
        resumen["error"] = "No se encontró Chrome/Chromium."
        print("❌ No se encontró Chrome/Chromium. Usa --executable con la ruta del binario.")
        return resumen

    if _en_entorno_agente():
        ruta_lanzador = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "iniciar_gui.bat"
        )
        resumen["error"] = (
            "La aplicación se está ejecutando desde la terminal del agente (DSH), "
            "que impide que Chrome arranque.\n\n"
            "Ciérrala y ábrela con doble clic en iniciar_gui.bat desde el "
            "Explorador de Windows, o pulsa Win+R y ejecuta:\n"
            f"{ruta_lanzador}"
        )
        print("❌ La aplicación se ejecuta desde la terminal del agente (DSH).")
        print("   Chrome no puede arrancar en ese entorno restringido.")
        print("   Ciérrala y ábrela con doble clic en iniciar_gui.bat desde el")
        print("   Explorador de Windows, o con Win+R.")
        return resumen

    escribible, detalle_perfil = comprobar_perfil_chrome()
    if not escribible:
        resumen["error"] = (
            "No se puede escribir en el perfil de Chrome:\n"
            f"{CARPETA_PERFIL}\n\n"
            f"{detalle_perfil}\n\n"
            "Si has abierto la aplicación desde una terminal del agente o un "
            "entorno con sandbox, ciérrala y ábrela con doble clic en "
            "iniciar_gui.bat desde el Explorador de Windows."
        )
        print("❌ No se puede escribir en el perfil de Chrome:")
        print(f"   {CARPETA_PERFIL}")
        print(f"   {detalle_perfil}")
        print("   Si la abriste desde una terminal del agente/sandbox, ábrela con")
        print("   doble clic en iniciar_gui.bat desde el Explorador de Windows.")
        return resumen

    if not _preparar_temp():
        print("⚠️  No se pudo preparar una carpeta temporal propia; "
              "se usará la del sistema.")

    _comprobar_cancelado(cancelado)

    hosts = set()
    for url in urls:
        host = urlparse(url).hostname
        if host:
            hosts.add(host)
    reglas = construir_host_resolver_rules(hosts)

    _comprobar_cancelado(cancelado)

    proc, puerto = _lanzar_chrome(ruta_navegador, reglas, headless)
    try:
        if not _esperar_puerto(puerto):
            detalle_chrome = _leer_log_chrome()
            mensaje = "Chrome no respondió en el puerto de depuración."
            print("❌ Chrome no respondió en el puerto de depuración.")
            print("   Si hay una ventana de Chrome anterior abierta por el script, "
                  "ciérrala y reintenta.")
            if detalle_chrome:
                print("   Últimas líneas de Chrome:")
                for linea in detalle_chrome.splitlines():
                    print(f"   {linea}")
                mensaje += "\n\n" + detalle_chrome
                pistas = ("crashpad", "openprocess", "access is denied",
                          "acceso denegado")
                if any(pista in detalle_chrome.lower() for pista in pistas):
                    mensaje += (
                        "\n\nChrome no puede arrancar en este entorno restringido. "
                        "Abre la aplicación desde el Explorador de Windows, fuera "
                        "del sandbox o la terminal del agente."
                    )
            resumen["error"] = mensaje
            return resumen

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{puerto}")
            try:
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                page.set_viewport_size({"width": 1366, "height": 900})

                for pagina, url in enumerate(urls, start=1):
                    _comprobar_cancelado(cancelado)
                    print(f"🔎 Abriendo ({pagina}/{len(urls)}): {url}")
                    try:
                        page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                    except Exception as e:
                        print(f"❌ Error al abrir la página: {e}")
                        continue

                    if not _esperar_challenge(page, timeout, cancelado):
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
                        _notificar(al_progreso, 0, 0)
                        continue

                    print(f"✅ Se encontraron {len(enlaces)} archivos .torrent.")
                    _notificar(al_progreso, 0, len(enlaces))
                    for i, url_torrent in enumerate(enlaces, start=1):
                        _comprobar_cancelado(cancelado)
                        resultado = _descargar_torrent(
                            page, url_torrent, carpeta_salida, i,
                            len(enlaces), cancelado, al_descargar
                        )
                        if resultado == "descargado":
                            resumen["descargados"] += 1
                        elif resultado == "omitido":
                            resumen["omitidos"] += 1
                        else:
                            resumen["errores"] += 1
                        _notificar(al_progreso, i, len(enlaces))
            finally:
                browser.close()
    except DescargaCancelada:
        resumen["cancelado"] = True
        print("⏹️  Descarga detenida por el usuario.")
    finally:
        _cerrar_chrome(proc)

    if not resumen["cancelado"] and resumen["error"] is None:
        print(
            f"🎉 Proceso finalizado: {resumen['descargados']} descargados, "
            f"{resumen['omitidos']} omitidos, {resumen['errores']} errores."
        )
    return resumen


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
        epilog=(
            f"Ejemplo: %(prog)s '{URL_EJEMPLO}' -o {CARPETA_SALIDA}\n"
            f"Interfaz gráfica: %(prog)s --gui"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="Una o varias URLs de páginas desde las que descargar los .torrent.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Abre la interfaz gráfica en lugar de usar la línea de comandos.",
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

    if args.gui:
        from downtorrent_gui import main as gui_main

        gui_main()
        return

    if not args.urls:
        parser.error("indica al menos una URL, o usa --gui para abrir la interfaz gráfica.")

    descargar_torrents(
        args.urls,
        carpeta_salida=args.salida,
        headless=args.headless,
        executable=args.executable,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
