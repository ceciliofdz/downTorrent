#!/usr/bin/env python3
"""Cliente mínimo para la API Web de qBittorrent, sin dependencias externas.

Usa ``urllib`` y ``http.cookiejar``. La API está documentada en
https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
"""

import http.cookiejar
import os
import urllib.error
import urllib.parse
import urllib.request


class ErrorQbittorrent(Exception):
    """Error de conexión, autenticación o respuesta de qBittorrent."""


class ErrorTorrentRechazado(ErrorQbittorrent):
    """qBittorrent respondió «Fails.» a un torrent concreto (no es fatal)."""


class ClienteQbittorrent:
    def __init__(self, url="http://127.0.0.1:8080", usuario="", clave="",
                 timeout=8):
        url = (url or "").strip()
        if not url:
            raise ErrorQbittorrent("Indica la dirección de la Web UI de qBittorrent.")
        if "://" not in url:
            url = "http://" + url
        self.url = url.rstrip("/")
        self.usuario = usuario or ""
        self.clave = clave or ""
        self.timeout = timeout
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cookies),
        )
        self._autenticado = False

    # ------------------------------------------------------------------
    def _peticion(self, ruta, datos=None, tipo=None):
        url = self.url + ruta
        cabeceras = {
            "Referer": self.url + "/",
            "Origin": self.url,
        }
        if tipo:
            cabeceras["Content-Type"] = tipo
        peticion = urllib.request.Request(url, data=datos, headers=cabeceras)
        try:
            with self._opener.open(peticion, timeout=self.timeout) as respuesta:
                return respuesta.status, respuesta.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:
            raise ErrorQbittorrent(
                f"No se pudo conectar con qBittorrent en {self.url}: {exc}"
            ) from exc

    def _login(self):
        datos = urllib.parse.urlencode(
            {"username": self.usuario, "password": self.clave}
        ).encode("utf-8")
        codigo, cuerpo = self._peticion(
            "/api/v2/auth/login",
            datos=datos,
            tipo="application/x-www-form-urlencoded",
        )
        texto = cuerpo.decode("utf-8", "replace").strip()
        if codigo == 200 and texto.lower() == "ok.":
            self._autenticado = True
            return True
        raise ErrorQbittorrent(
            "qBittorrent rechazó el usuario o la contraseña (Web UI). "
            "Revisa las credenciales y que la Web UI esté activada."
        )

    def _con_reintento(self, funcion):
        resultado = funcion()
        if resultado[0] == 403 and not self._autenticado:
            self._login()
            resultado = funcion()
        return resultado

    # ------------------------------------------------------------------
    def version(self):
        """Devuelve la versión de qBittorrent o lanza ErrorQbittorrent."""
        codigo, cuerpo = self._con_reintento(
            lambda: self._peticion("/api/v2/app/version")
        )
        if codigo != 200:
            raise ErrorQbittorrent(
                f"qBittorrent respondió HTTP {codigo} al pedir la versión."
            )
        return cuerpo.decode("utf-8", "replace").strip()

    def agregar(self, ruta_torrent, categoria="", pausado=False):
        """Añade un .torrent. Devuelve la respuesta de qBittorrent."""
        if not os.path.isfile(ruta_torrent):
            raise ErrorQbittorrent(f"No existe el archivo: {ruta_torrent}")

        campos = {}
        if categoria.strip():
            campos["category"] = categoria.strip()
        if pausado:
            # qBittorrent 4.x usa "paused"; 5.x usa "stopped".
            campos["paused"] = "true"
            campos["stopped"] = "true"

        with open(ruta_torrent, "rb") as f:
            contenido = f.read()
        cuerpo, tipo = _multipart(
            os.path.basename(ruta_torrent), contenido, campos
        )
        codigo, respuesta = self._con_reintento(
            lambda: self._peticion(
                "/api/v2/torrents/add", datos=cuerpo, tipo=tipo
            )
        )
        texto = respuesta.decode("utf-8", "replace").strip()
        if codigo != 200:
            raise ErrorQbittorrent(
                f"qBittorrent respondió HTTP {codigo} al añadir el torrent."
            )
        if texto and texto.lower() not in ("ok.", "ok"):
            raise ErrorTorrentRechazado(
                f"qBittorrent rechazó el torrent: {texto} "
                "(puede que ya estuviera añadido)."
            )
        return texto or "Ok."


def _multipart(nombre_archivo, contenido, campos):
    """Construye un cuerpo multipart/form-data con el campo ``torrents``."""
    frontera = "----downTorrent" + os.urandom(12).hex()
    partes = []
    for clave, valor in campos.items():
        partes.append(
            (
                f"--{frontera}\r\n"
                f'Content-Disposition: form-data; name="{clave}"\r\n\r\n'
                f"{valor}\r\n"
            ).encode("utf-8")
        )
    partes.append(
        (
            f"--{frontera}\r\n"
            f'Content-Disposition: form-data; name="torrents"; '
            f'filename="{nombre_archivo}"\r\n'
            "Content-Type: application/x-bittorrent\r\n\r\n"
        ).encode("utf-8")
    )
    partes.append(contenido)
    partes.append(f"\r\n--{frontera}--\r\n".encode("utf-8"))
    return b"".join(partes), f"multipart/form-data; boundary={frontera}"


def ruta_qbittorrent():
    """Ruta del ejecutable de qBittorrent si está instalado."""
    import shutil

    candidatas = (
        r"%ProgramFiles%\qBittorrent\qbittorrent.exe",
        r"%ProgramFiles(x86)%\qBittorrent\qbittorrent.exe",
        r"%LocalAppData%\Programs\qBittorrent\qbittorrent.exe",
        "/Applications/qBittorrent.app/Contents/MacOS/qBittorrent",
        "/usr/bin/qbittorrent",
        "/usr/bin/qbittorrent-nox",
    )
    for ruta in candidatas:
        ruta = os.path.expandvars(ruta)
        if os.path.exists(ruta):
            return ruta
    for nombre in ("qbittorrent", "qbittorrent-nox"):
        ruta = shutil.which(nombre)
        if ruta:
            return ruta
    return None
