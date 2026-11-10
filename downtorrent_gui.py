#!/usr/bin/env python3
"""Interfaz gráfica de downTorrent (Tkinter, sin dependencias adicionales).

La descarga se ejecuta en un hilo aparte para que la ventana siga respondiendo y
la salida del núcleo (los ``print`` de ``downtorrent.py``) se redirige a la caja
de registro. El botón Detener activa un ``threading.Event`` que el núcleo
consulta entre página y página, y entre archivo y archivo.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, font as tkfont, messagebox, ttk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CARPETA_SALIDA_POR_DEFECTO = os.path.join(BASE_DIR, "torrents")
RUTA_ICONO = os.path.join(BASE_DIR, "assets", "icono.png")
RUTA_CONFIG = os.path.join(os.path.expanduser("~"), ".downtorrent", "gui.json")

# La importación se protege para poder mostrar un diálogo explicativo si el
# usuario abre la interfaz sin haber instalado las dependencias.
_ERROR_IMPORT = None
try:
    from downtorrent import (
        DescargaCancelada,
        comprobar_perfil_chrome,
        descargar_torrents,
        detectar_navegador,
    )
    from qbittorrent import (
        ClienteQbittorrent,
        ErrorTorrentRechazado,
        ruta_qbittorrent,
    )
except Exception as _exc:  # pragma: no cover - depende del entorno
    _ERROR_IMPORT = _exc


# Los emojis del núcleo no se representan bien en todos los Tk (Windows suele
# dibujarlos como cuadros). En la interfaz se sustituyen por etiquetas de texto.
_EMOJI_A_TEXTO = (
    ("✅", "[OK]"),
    ("❌", "[ERROR]"),
    ("⚠️", "[AVISO]"),
    ("⚠", "[AVISO]"),
    ("⏭️", "[OMITIDO]"),
    ("⏭", "[OMITIDO]"),
    ("⬇️", "[DESCARGA]"),
    ("⬇", "[DESCARGA]"),
    ("🔎", "[PÁGINA]"),
    ("📤", "[QBITTORRENT]"),
    ("⏹️", "[DETENIDO]"),
    ("⏹", "[DETENIDO]"),
    ("🎉", "[FIN]"),
)


def _sanear(linea):
    """Sustituye los emojis por etiquetas legibles en el registro."""
    for emoji, texto in _EMOJI_A_TEXTO:
        linea = linea.replace(emoji, texto)
    return linea.replace("\ufe0f", "")


def _nivel(linea):
    """Etiqueta de color según el marcador con el que empieza la línea."""
    limpia = linea.lstrip()
    if limpia.startswith("✅") or limpia.startswith("🎉") or limpia.startswith("📤"):
        return "ok"
    if limpia.startswith("❌"):
        return "error"
    if limpia.startswith("⚠") or limpia.startswith("⏹"):
        return "aviso"
    if limpia.startswith("⏭"):
        return "omitido"
    if limpia.startswith("🔎"):
        return "pagina"
    return "normal"


class _FlujoCola:
    """Sustituto de ``sys.stdout`` que convierte cada línea en un evento."""

    def __init__(self, cola):
        self._cola = cola
        self._buffer = ""

    def write(self, texto):
        if not isinstance(texto, str):
            texto = str(texto)
        self._buffer += texto
        while "\n" in self._buffer:
            linea, self._buffer = self._buffer.split("\n", 1)
            self._cola.put(("log", linea))

    def flush(self):
        if self._buffer:
            self._cola.put(("log", self._buffer))
            self._buffer = ""

    def isatty(self):
        return False


def _elegir_familia(root, *candidatas):
    disponibles = set(tkfont.families(root))
    for familia in candidatas:
        if familia in disponibles:
            return familia
    return tkfont.nametofont("TkDefaultFont").actual("family")


def _entorno_agente():
    """True si la GUI se lanzó desde la terminal del agente (DSH)."""
    return bool(os.environ.get("DSH_SESSION_ID") or os.environ.get("DSH_SHELL"))


def _activar_conciencia_dpi():
    """Evita que la ventana se vea borrosa en pantallas con escalado en Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            # PROCESS_SYSTEM_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class Aplicacion:
    def __init__(self, root):
        self.root = root
        self._cola = queue.Queue()
        self._cancelar = threading.Event()
        self._hilo = None
        self._activa = False
        self._en_cierre = False
        self._intentos_cierre = 0
        self._after_id = None

        self._configurar_ventana()
        self._configurar_estilos()
        self._construir_widgets()
        self._cargar_config()
        self._actualizar_pista_navegador()
        self._comprobar_perfil()

        # Traza del campo navegador: actualiza la pista al escribir o borrar.
        self.var_navegador.trace_add("write", lambda *_: self._actualizar_pista_navegador())

        self.root.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        self.root.bind("<Control-Return>", lambda _e: self._iniciar())
        self.root.bind("<F5>", lambda _e: self._iniciar())
        self.root.bind("<Destroy>", self._al_destruir, add="+")
        self._after_id = self.root.after(80, self._procesar_cola)

    # ------------------------------------------------------------------
    # Construcción de la ventana
    # ------------------------------------------------------------------
    def _configurar_ventana(self):
        self.root.title("downTorrent · Descargador de .torrent")
        self.root.minsize(740, 700)

        self._icono = None
        try:
            self._icono = tk.PhotoImage(file=RUTA_ICONO)
            self.root.iconphoto(True, self._icono)
        except Exception:
            self._icono = None

        ancho_pantalla = self.root.winfo_screenwidth()
        alto_pantalla = self.root.winfo_screenheight()
        ancho = max(760, min(940, int(ancho_pantalla * 0.52)))
        alto = max(700, min(940, int(alto_pantalla * 0.86)))
        x = max(0, (ancho_pantalla - ancho) // 2)
        y = max(0, (alto_pantalla - alto) // 2 - 20)
        self.root.geometry(f"{ancho}x{alto}+{x}+{y}")

    def _configurar_estilos(self):
        estilo = ttk.Style(self.root)
        temas = estilo.theme_names()
        for preferido in ("vista", "aqua", "clam"):
            if preferido in temas:
                estilo.theme_use(preferido)
                break

        familia = _elegir_familia(
            self.root, "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Noto Sans"
        )
        mono = _elegir_familia(
            self.root, "Consolas", "Cascadia Mono", "Menlo", "DejaVu Sans Mono"
        )

        self._f_normal = (familia, 10)
        self._f_pequena = (familia, 9)
        self._f_log = (mono, 9)

        estilo.configure("Titulo.TLabel", font=(familia, 18, "bold"))
        estilo.configure("Subtitulo.TLabel", font=(familia, 9), foreground="#5c677d")
        estilo.configure("Pista.TLabel", font=(familia, 9), foreground="#5c677d")
        estilo.configure("Estado.TLabel", font=(familia, 10))
        estilo.configure("TLabelframe.Label", font=(familia, 10, "bold"))
        estilo.configure("TButton", font=(familia, 10), padding=(12, 5))
        estilo.configure("Primario.TButton", font=(familia, 10, "bold"), padding=(18, 7))

    def _construir_widgets(self):
        cont = ttk.Frame(self.root, padding=(18, 14, 18, 14))
        cont.grid(row=0, column=0, sticky="nsew")
        cont.columnconfigure(0, weight=1)
        cont.rowconfigure(6, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Cabecera -------------------------------------------------------
        cabecera = ttk.Frame(cont)
        cabecera.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        cabecera.columnconfigure(0, weight=1)
        ttk.Label(cabecera, text="downTorrent", style="Titulo.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            cabecera,
            text="Descarga los archivos .torrent de una página de catálogo "
                 "sin pelearte con Cloudflare.",
            style="Subtitulo.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(1, 0))

        # URLs -----------------------------------------------------------
        marco_urls = ttk.LabelFrame(cont, text=" Páginas de origen ", padding=(10, 8))
        marco_urls.grid(row=1, column=0, sticky="ew")
        marco_urls.columnconfigure(0, weight=1)
        marco_urls.rowconfigure(0, weight=1)

        self.txt_urls = tk.Text(
            marco_urls,
            height=4,
            wrap="word",
            undo=True,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#c9ced6",
            highlightcolor="#3b82f6",
            font=self._f_normal,
            padx=8,
            pady=6,
        )
        self.txt_urls.grid(row=0, column=0, sticky="nsew")
        scroll_urls = ttk.Scrollbar(marco_urls, orient="vertical", command=self.txt_urls.yview)
        scroll_urls.grid(row=0, column=1, sticky="ns")
        self.txt_urls.configure(yscrollcommand=scroll_urls.set)

        fila_urls = ttk.Frame(marco_urls)
        fila_urls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.btn_pegar = ttk.Button(fila_urls, text="Pegar", command=self._pegar_urls)
        self.btn_pegar.pack(side="left")
        self.btn_limpiar = ttk.Button(fila_urls, text="Limpiar", command=self._limpiar_urls)
        self.btn_limpiar.pack(side="left", padx=(6, 0))
        ttk.Label(fila_urls, text="Una URL por línea", style="Pista.TLabel").pack(side="right")

        # Opciones -------------------------------------------------------
        marco_op = ttk.LabelFrame(cont, text=" Opciones ", padding=(10, 8))
        marco_op.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        marco_op.columnconfigure(1, weight=1)

        ttk.Label(marco_op, text="Carpeta de destino").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=3
        )
        self.var_salida = tk.StringVar(value=CARPETA_SALIDA_POR_DEFECTO)
        self.ent_salida = ttk.Entry(
            marco_op, textvariable=self.var_salida, font=self._f_normal
        )
        self.ent_salida.grid(row=0, column=1, sticky="ew", pady=3)
        self.btn_exam_carpeta = ttk.Button(
            marco_op, text="Examinar…", command=self._examinar_carpeta
        )
        self.btn_exam_carpeta.grid(row=0, column=2, padx=(8, 0), pady=3)

        ttk.Label(marco_op, text="Navegador (opcional)").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=3
        )
        self.var_navegador = tk.StringVar(value="")
        self.ent_navegador = ttk.Entry(
            marco_op, textvariable=self.var_navegador, font=self._f_normal
        )
        self.ent_navegador.grid(row=1, column=1, sticky="ew", pady=3)
        self.btn_exam_navegador = ttk.Button(
            marco_op, text="Examinar…", command=self._examinar_navegador
        )
        self.btn_exam_navegador.grid(row=1, column=2, padx=(8, 0), pady=3)

        self.lbl_navegador = ttk.Label(
            marco_op, text="", style="Pista.TLabel", wraplength=780, justify="left"
        )
        self.lbl_navegador.grid(row=2, column=1, columnspan=2, sticky="w", pady=(0, 6))

        fila_op = ttk.Frame(marco_op)
        fila_op.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self.var_headless = tk.BooleanVar(value=False)
        self.chk_headless = ttk.Checkbutton(
            fila_op,
            text="Chrome sin ventana (menos fiable contra Cloudflare)",
            variable=self.var_headless,
        )
        self.chk_headless.pack(side="left")

        ttk.Label(fila_op, text="segundos").pack(side="right")
        self.var_timeout = tk.StringVar(value="60")
        self.spn_timeout = ttk.Spinbox(
            fila_op,
            from_=5,
            to=600,
            increment=5,
            width=6,
            textvariable=self.var_timeout,
            justify="center",
        )
        self.spn_timeout.pack(side="right", padx=(4, 0))
        ttk.Label(fila_op, text="Espera del challenge:").pack(side="right", padx=(10, 6))

        # qBittorrent ----------------------------------------------------
        marco_qb = ttk.LabelFrame(marco_op, text=" qBittorrent ", padding=(8, 6))
        marco_qb.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        marco_qb.columnconfigure(1, weight=1)

        self.var_qb_activo = tk.BooleanVar(value=False)
        self.chk_qb_activo = ttk.Checkbutton(
            marco_qb,
            text="Enviar cada .torrent a qBittorrent al descargarlo",
            variable=self.var_qb_activo,
        )
        self.chk_qb_activo.grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(marco_qb, text="Web UI").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.var_qb_url = tk.StringVar(value="http://127.0.0.1:8080")
        self.ent_qb_url = ttk.Entry(
            marco_qb, textvariable=self.var_qb_url, font=self._f_normal
        )
        self.ent_qb_url.grid(row=1, column=1, sticky="ew", pady=3)
        self.btn_qb_probar = ttk.Button(
            marco_qb, text="Probar", command=self._probar_qbittorrent
        )
        self.btn_qb_probar.grid(row=1, column=2, padx=(8, 0), pady=3)
        self.btn_qb_abrir = ttk.Button(
            marco_qb, text="Abrir qBittorrent", command=self._abrir_qbittorrent
        )
        self.btn_qb_abrir.grid(row=1, column=3, padx=(6, 0), pady=3)

        fila_qb = ttk.Frame(marco_qb)
        fila_qb.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        ttk.Label(fila_qb, text="Usuario").pack(side="left")
        self.var_qb_usuario = tk.StringVar(value="admin")
        self.ent_qb_usuario = ttk.Entry(
            fila_qb, textvariable=self.var_qb_usuario, width=12,
            font=self._f_normal,
        )
        self.ent_qb_usuario.pack(side="left", padx=(4, 10))
        ttk.Label(fila_qb, text="Contraseña").pack(side="left")
        self.var_qb_clave = tk.StringVar(value="")
        self.ent_qb_clave = ttk.Entry(
            fila_qb, textvariable=self.var_qb_clave, width=14, show="*",
            font=self._f_normal,
        )
        self.ent_qb_clave.pack(side="left", padx=(4, 10))
        ttk.Label(fila_qb, text="Categoría").pack(side="left")
        self.var_qb_categoria = tk.StringVar(value="")
        self.ent_qb_categoria = ttk.Entry(
            fila_qb, textvariable=self.var_qb_categoria, width=14,
            font=self._f_normal,
        )
        self.ent_qb_categoria.pack(side="left", padx=(4, 10))
        self.var_qb_pausa = tk.BooleanVar(value=False)
        self.chk_qb_pausa = ttk.Checkbutton(
            fila_qb, text="Añadir en pausa", variable=self.var_qb_pausa
        )
        self.chk_qb_pausa.pack(side="left")

        ttk.Label(
            marco_qb,
            text="Activa la Web UI en qBittorrent: Herramientas → Opciones → Web UI.",
            style="Pista.TLabel",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # Acciones -------------------------------------------------------
        acciones = ttk.Frame(cont)
        acciones.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.btn_descargar = ttk.Button(
            acciones,
            text="Descargar",
            style="Primario.TButton",
            command=self._iniciar,
        )
        self.btn_descargar.pack(side="left")
        self.btn_detener = ttk.Button(
            acciones, text="Detener", command=self._detener, state="disabled"
        )
        self.btn_detener.pack(side="left", padx=(8, 0))
        self.btn_abrir = ttk.Button(
            acciones, text="Abrir carpeta", command=self._abrir_carpeta
        )
        self.btn_abrir.pack(side="left", padx=(8, 0))
        ttk.Label(
            acciones, text="Ctrl+Intro para descargar", style="Pista.TLabel"
        ).pack(side="right")

        # Progreso y estado ---------------------------------------------
        barra_marco = ttk.Frame(cont)
        barra_marco.grid(row=4, column=0, sticky="ew", pady=(14, 2))
        barra_marco.columnconfigure(0, weight=1)
        self.barra = ttk.Progressbar(
            barra_marco, mode="determinate", maximum=100, value=0
        )
        self.barra.grid(row=0, column=0, sticky="ew")
        self.lbl_conteo = ttk.Label(
            barra_marco, text="", style="Pista.TLabel", width=11, anchor="e"
        )
        self.lbl_conteo.grid(row=0, column=1, padx=(10, 0))

        self.lbl_estado = ttk.Label(
            cont, text="Listo para descargar.", style="Estado.TLabel", wraplength=800
        )
        self.lbl_estado.grid(row=5, column=0, sticky="w", pady=(2, 8))

        # Registro -------------------------------------------------------
        marco_log = ttk.LabelFrame(cont, text=" Registro ", padding=(8, 6))
        marco_log.grid(row=6, column=0, sticky="nsew")
        marco_log.columnconfigure(0, weight=1)
        marco_log.rowconfigure(1, weight=1)

        barra_log = ttk.Frame(marco_log)
        barra_log.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(barra_log, text="Copiar", command=self._copiar_log).pack(side="right")
        ttk.Button(barra_log, text="Limpiar", command=self._limpiar_log).pack(
            side="right", padx=(0, 6)
        )

        self.txt_log = tk.Text(
            marco_log,
            height=9,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#c9ced6",
            font=self._f_log,
            padx=8,
            pady=6,
            background="#ffffff",
        )
        self.txt_log.grid(row=1, column=0, sticky="nsew")
        scroll_log = ttk.Scrollbar(marco_log, orient="vertical", command=self.txt_log.yview)
        scroll_log.grid(row=1, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=scroll_log.set)

        self.txt_log.tag_configure("ok", foreground="#1a7f37")
        self.txt_log.tag_configure("error", foreground="#b42318")
        self.txt_log.tag_configure("aviso", foreground="#b54708")
        self.txt_log.tag_configure("omitido", foreground="#667085")
        self.txt_log.tag_configure("pagina", foreground="#175cd3")
        self.txt_log.tag_configure("tenue", foreground="#667085")
        self.txt_log.tag_configure("normal", foreground="#1f2937")

        self._controles = [
            self.txt_urls,
            self.btn_pegar,
            self.btn_limpiar,
            self.ent_salida,
            self.btn_exam_carpeta,
            self.ent_navegador,
            self.btn_exam_navegador,
            self.spn_timeout,
            self.chk_headless,
            self.chk_qb_activo,
            self.ent_qb_url,
            self.btn_qb_probar,
            self.btn_qb_abrir,
            self.ent_qb_usuario,
            self.ent_qb_clave,
            self.ent_qb_categoria,
            self.chk_qb_pausa,
        ]

        self._agregar_log(
            "Pega una o varias URL de páginas de catálogo y pulsa Descargar.",
            "tenue",
        )
        self.txt_urls.focus_set()

    # ------------------------------------------------------------------
    # Acciones de la interfaz
    # ------------------------------------------------------------------
    def _leer_urls(self):
        return self.txt_urls.get("1.0", "end").split()

    def _pegar_urls(self):
        try:
            texto = self.root.clipboard_get()
        except tk.TclError:
            texto = ""
        texto = texto.strip()
        if not texto:
            return
        actual = self.txt_urls.get("1.0", "end-1c")
        if actual.strip():
            self.txt_urls.insert("end", "\n" + texto)
        else:
            self.txt_urls.insert("1.0", texto)
        self._actualizar_estado("URL pegada en el cuadro de origen.")

    def _limpiar_urls(self):
        self.txt_urls.delete("1.0", "end")
        self.txt_urls.focus_set()

    def _examinar_carpeta(self):
        inicial = self.var_salida.get().strip() or BASE_DIR
        if not os.path.isdir(inicial):
            inicial = BASE_DIR
        ruta = filedialog.askdirectory(
            parent=self.root,
            title="Carpeta de destino",
            initialdir=inicial,
            mustexist=False,
        )
        if ruta:
            self.var_salida.set(os.path.normpath(ruta))

    def _examinar_navegador(self):
        actual = self.var_navegador.get().strip()
        inicial = os.path.dirname(actual) if os.path.isfile(actual) else BASE_DIR
        tipos = [("Ejecutables", "*.exe"), ("Todos los archivos", "*.*")] \
            if sys.platform == "win32" else [("Todos los archivos", "*.*")]
        ruta = filedialog.askopenfilename(
            parent=self.root,
            title="Selecciona Chrome, Chromium, Brave o Edge",
            initialdir=inicial,
            filetypes=tipos,
        )
        if ruta:
            self.var_navegador.set(os.path.normpath(ruta))

    def _actualizar_pista_navegador(self):
        ruta_manual = self.var_navegador.get().strip()
        if ruta_manual:
            if os.path.isfile(ruta_manual):
                texto = f"Se usará: {ruta_manual}"
            else:
                texto = "⚠ No existe esa ruta; revisa el navegador indicado."
            self.lbl_navegador.configure(text=texto)
            return

        try:
            detectado = detectar_navegador()
        except Exception:
            detectado = None
        if detectado:
            texto = f"Detectado automáticamente: {detectado}"
        else:
            texto = "No se detectó Chrome/Chromium; indica la ruta al ejecutable."
        self.lbl_navegador.configure(text=texto)

    def _comprobar_perfil(self):
        """Avisa al arrancar si el entorno impide que Chrome use su perfil."""
        if _entorno_agente():
            self._agregar_log(
                "[AVISO] La aplicación se ha abierto desde la terminal del agente "
                "(DSH). Chrome no puede arrancar en ese entorno restringido. "
                "Ciérrala y ábrela con doble clic en iniciar_gui.bat desde el "
                "Explorador de Windows, o con Win+R.",
                "aviso",
            )
            self._actualizar_estado(
                "Abierta desde la terminal del agente: Chrome no podrá arrancar."
            )
            return

        try:
            escribible, _detalle = comprobar_perfil_chrome()
        except Exception:
            return
        if escribible:
            return
        self._agregar_log(
            "[AVISO] No se puede escribir en el perfil de Chrome, así que Chrome "
            "no podrá arrancar. Revisa los permisos de la carpeta o abre la "
            "aplicación desde el Explorador de Windows.",
            "aviso",
        )
        self._actualizar_estado(
            "Aviso: perfil de Chrome no escribible; revisa los permisos de la carpeta."
        )

    def _abrir_carpeta(self):
        ruta = self.var_salida.get().strip() or CARPETA_SALIDA_POR_DEFECTO
        ruta = os.path.abspath(os.path.expanduser(ruta))
        try:
            os.makedirs(ruta, exist_ok=True)
            if os.name == "nt":
                os.startfile(ruta)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", ruta])
            else:
                subprocess.Popen(["xdg-open", ruta])
        except Exception as exc:
            messagebox.showerror(
                "downTorrent", f"No se pudo abrir la carpeta:\n{exc}", parent=self.root
            )

    def _crear_cliente_qbittorrent(self):
        return ClienteQbittorrent(
            self.var_qb_url.get().strip(),
            self.var_qb_usuario.get().strip(),
            self.var_qb_clave.get(),
        )

    def _probar_qbittorrent(self):
        self.btn_qb_probar.configure(state="disabled")
        self.root.update_idletasks()
        try:
            cliente = self._crear_cliente_qbittorrent()
            version = cliente.version()
        except Exception as exc:
            messagebox.showerror(
                "qBittorrent",
                f"{exc}\n\nComprueba que qBittorrent esté abierto y que la Web UI "
                "esté activada (Herramientas → Opciones → Web UI).",
                parent=self.root,
            )
        else:
            messagebox.showinfo(
                "qBittorrent",
                f"Conectado a qBittorrent {version}.",
                parent=self.root,
            )
        finally:
            self.btn_qb_probar.configure(state="normal")

    def _abrir_qbittorrent(self):
        ruta = ruta_qbittorrent()
        if not ruta:
            messagebox.showerror(
                "qBittorrent",
                "No se encontró qBittorrent instalado en las rutas habituales.",
                parent=self.root,
            )
            return
        try:
            subprocess.Popen([ruta])
        except Exception as exc:
            messagebox.showerror(
                "qBittorrent",
                f"No se pudo abrir qBittorrent:\n{exc}",
                parent=self.root,
            )

    def _iniciar(self):
        if self._activa:
            return
        if _ERROR_IMPORT is not None:
            messagebox.showerror(
                "downTorrent",
                "No se pudieron cargar las dependencias del proyecto.\n\n"
                f"{_ERROR_IMPORT}\n\n"
                "Instálalas desde la carpeta del proyecto con:\n\n"
                "    pip install -r requirements.txt",
                parent=self.root,
            )
            return

        urls = self._leer_urls()
        if not urls:
            messagebox.showwarning(
                "downTorrent",
                "Escribe al menos una URL de una página de catálogo.",
                parent=self.root,
            )
            self.txt_urls.focus_set()
            return

        invalidas = [u for u in urls if not re.match(r"^https?://", u, re.IGNORECASE)]
        if invalidas:
            muestra = "\n".join(invalidas[:5])
            messagebox.showwarning(
                "downTorrent",
                "Estas direcciones no empiezan por http:// o https://:\n\n"
                f"{muestra}",
                parent=self.root,
            )
            return

        carpeta = self.var_salida.get().strip()
        if not carpeta:
            messagebox.showwarning(
                "downTorrent", "Indica una carpeta de destino.", parent=self.root
            )
            self.ent_salida.focus_set()
            return
        carpeta = os.path.abspath(os.path.expanduser(carpeta))

        navegador = self.var_navegador.get().strip()
        if navegador and not os.path.isfile(navegador):
            messagebox.showerror(
                "downTorrent",
                f"No existe el navegador indicado:\n{navegador}\n\n"
                "Corrige la ruta o déjala vacía para detectarlo automáticamente.",
                parent=self.root,
            )
            return

        try:
            timeout = int(float(self.var_timeout.get().replace(",", ".")))
        except (ValueError, AttributeError):
            timeout = 60
        timeout = max(5, min(600, timeout))
        self.var_timeout.set(str(timeout))

        al_descargar_qb = None
        if self.var_qb_activo.get():
            try:
                cliente_qb = self._crear_cliente_qbittorrent()
            except Exception as exc:
                messagebox.showerror("qBittorrent", str(exc), parent=self.root)
                return
            categoria_qb = self.var_qb_categoria.get().strip()
            pausado_qb = bool(self.var_qb_pausa.get())
            estado_qb = {"fallo": False}

            def enviar_qb(ruta, url_torrent):
                if estado_qb["fallo"]:
                    return
                try:
                    cliente_qb.agregar(ruta, categoria_qb, pausado_qb)
                except ErrorTorrentRechazado as exc:
                    print(f"   ⚠️  {exc}")
                except Exception as exc:
                    estado_qb["fallo"] = True
                    print(f"   ⚠️  No se pudo enviar a qBittorrent: {exc}")
                    print("      Se omite el envío del resto de archivos.")
                else:
                    print(f"   📤 Enviado a qBittorrent: {os.path.basename(ruta)}")

            al_descargar_qb = enviar_qb

        self._guardar_config()
        self._agregar_log("")
        self._agregar_log(
            f"────── Nueva descarga · {datetime.now():%H:%M:%S} ──────", "tenue"
        )

        self._cancelar.clear()
        self._activa = True
        self._bloquear(True)
        self.barra.configure(maximum=100, value=0)
        self.lbl_conteo.configure(text="")
        self._actualizar_estado("Iniciando Chrome y resolviendo el challenge…")

        self._hilo = threading.Thread(
            target=self._trabajador,
            args=(urls, carpeta, bool(self.var_headless.get()),
                  navegador or None, timeout, al_descargar_qb),
            daemon=True,
        )
        self._hilo.start()

    def _detener(self):
        if not self._activa:
            return
        self._cancelar.set()
        self.btn_detener.configure(state="disabled")
        self._actualizar_estado(
            "Deteniendo… se cerrará Chrome al terminar la operación en curso."
        )

    def _trabajador(self, urls, carpeta, headless, navegador, timeout,
                    al_descargar=None):
        flujo = _FlujoCola(self._cola)
        stdout_previo, stderr_previo = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = flujo
        resumen = {
            "descargados": 0,
            "omitidos": 0,
            "errores": 0,
            "cancelado": False,
            "error": None,
        }
        try:
            resultado = descargar_torrents(
                urls,
                carpeta_salida=carpeta,
                headless=headless,
                executable=navegador,
                timeout=timeout,
                al_progreso=lambda hechos, total: self._cola.put(
                    ("progreso", hechos, total)
                ),
                cancelado=self._cancelar,
                al_descargar=al_descargar,
            )
            if isinstance(resultado, dict):
                resumen.update(resultado)
        except DescargaCancelada:
            resumen["cancelado"] = True
        except Exception as exc:
            resumen["error"] = str(exc)
            traceback.print_exc()
        finally:
            flujo.flush()
            sys.stdout, sys.stderr = stdout_previo, stderr_previo
            self._cola.put(("fin", resumen))

    # ------------------------------------------------------------------
    # Cola de eventos y estado
    # ------------------------------------------------------------------
    def _procesar_cola(self):
        self._after_id = None
        try:
            while True:
                evento = self._cola.get_nowait()
                tipo = evento[0]
                if tipo == "log":
                    self._agregar_log(evento[1])
                elif tipo == "progreso":
                    self._actualizar_progreso(evento[1], evento[2])
                elif tipo == "fin":
                    self._finalizar(evento[1])
        except queue.Empty:
            pass

        if not self._en_cierre and self.root.winfo_exists():
            self._after_id = self.root.after(80, self._procesar_cola)

    def _al_destruir(self, evento):
        """Cancela el sondeo pendiente para que Tk no invoque un callback muerto."""
        if evento.widget is not self.root or self._after_id is None:
            return
        try:
            self.root.after_cancel(self._after_id)
        except tk.TclError:
            pass
        self._after_id = None

    def _agregar_log(self, linea, nivel=None):
        if nivel is None:
            nivel = _nivel(linea)
        texto = _sanear(linea).rstrip("\r")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", texto + "\n", nivel)

        # Limita el crecimiento del registro en descargas muy largas.
        total_lineas = int(self.txt_log.index("end-1c").split(".")[0])
        if total_lineas > 4000:
            self.txt_log.delete("1.0", "501.0")

        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _actualizar_progreso(self, hechos, total):
        if total <= 0:
            self.barra.configure(maximum=1, value=0)
            self.lbl_conteo.configure(text="")
            return
        self.barra.configure(maximum=total, value=hechos)
        self.lbl_conteo.configure(text=f"{hechos} de {total}")

    def _actualizar_estado(self, texto):
        self.lbl_estado.configure(text=texto)

    def _finalizar(self, resumen):
        self._activa = False
        self._bloquear(False)
        self._hilo = None

        if not isinstance(resumen, dict):
            resumen = {}
        if resumen.get("cancelado"):
            self.barra.configure(value=0)
            self.lbl_conteo.configure(text="")
            self._actualizar_estado(
                f"Descarga detenida: {resumen.get('descargados', 0)} descargados, "
                f"{resumen.get('omitidos', 0)} omitidos, "
                f"{resumen.get('errores', 0)} errores."
            )
            return
        if resumen.get("error"):
            self._actualizar_estado(f"Error: {resumen['error']}")
            messagebox.showerror("downTorrent", resumen["error"], parent=self.root)
            return

        descargados = resumen.get("descargados", 0)
        omitidos = resumen.get("omitidos", 0)
        errores = resumen.get("errores", 0)
        self.barra.configure(value=self.barra.cget("maximum"))
        self._actualizar_estado(
            f"Listo: {descargados} descargados · {omitidos} omitidos · {errores} errores."
        )

    def _bloquear(self, activo):
        estado = "disabled" if activo else "normal"
        for control in self._controles:
            try:
                control.configure(state=estado)
            except tk.TclError:
                pass
        self.btn_descargar.configure(state=estado)
        self.btn_detener.configure(state="normal" if activo else "disabled")

    def _copiar_log(self):
        texto = self.txt_log.get("1.0", "end-1c")
        if not texto:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(texto)
        if not self._activa:
            self._actualizar_estado("Registro copiado al portapapeles.")

    def _limpiar_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # Configuración persistente y cierre
    # ------------------------------------------------------------------
    def _cargar_config(self):
        try:
            with open(RUTA_CONFIG, encoding="utf-8") as f:
                datos = json.load(f)
        except Exception:
            return
        if not isinstance(datos, dict):
            return

        if isinstance(datos.get("salida"), str) and datos["salida"].strip():
            self.var_salida.set(datos["salida"])
        if isinstance(datos.get("navegador"), str):
            self.var_navegador.set(datos["navegador"])
        if isinstance(datos.get("timeout"), int) and 5 <= datos["timeout"] <= 600:
            self.var_timeout.set(str(datos["timeout"]))
        if isinstance(datos.get("headless"), bool):
            self.var_headless.set(datos["headless"])
        if isinstance(datos.get("qb_activo"), bool):
            self.var_qb_activo.set(datos["qb_activo"])
        if isinstance(datos.get("qb_url"), str) and datos["qb_url"].strip():
            self.var_qb_url.set(datos["qb_url"])
        if isinstance(datos.get("qb_usuario"), str):
            self.var_qb_usuario.set(datos["qb_usuario"])
        if isinstance(datos.get("qb_clave"), str):
            self.var_qb_clave.set(datos["qb_clave"])
        if isinstance(datos.get("qb_categoria"), str):
            self.var_qb_categoria.set(datos["qb_categoria"])
        if isinstance(datos.get("qb_pausa"), bool):
            self.var_qb_pausa.set(datos["qb_pausa"])

        geometria = datos.get("geometria")
        if isinstance(geometria, str) and re.match(r"^\d+x\d+[+-]\d+[+-]\d+$", geometria):
            try:
                self.root.geometry(geometria)
            except tk.TclError:
                pass

    def _guardar_config(self):
        datos = {
            "salida": self.var_salida.get().strip(),
            "navegador": self.var_navegador.get().strip(),
            "timeout": self.var_timeout.get().strip(),
            "headless": bool(self.var_headless.get()),
            "qb_activo": bool(self.var_qb_activo.get()),
            "qb_url": self.var_qb_url.get().strip(),
            "qb_usuario": self.var_qb_usuario.get().strip(),
            "qb_clave": self.var_qb_clave.get(),
            "qb_categoria": self.var_qb_categoria.get().strip(),
            "qb_pausa": bool(self.var_qb_pausa.get()),
            "geometria": self.root.geometry(),
        }
        try:
            timeout = int(float(datos["timeout"].replace(",", ".")))
            datos["timeout"] = max(5, min(600, timeout))
        except (ValueError, AttributeError):
            datos["timeout"] = 60
        try:
            os.makedirs(os.path.dirname(RUTA_CONFIG), exist_ok=True)
            with open(RUTA_CONFIG, "w", encoding="utf-8") as f:
                json.dump(datos, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _al_cerrar(self):
        if self._activa:
            if not messagebox.askyesno(
                "downTorrent",
                "Hay una descarga en curso.\n¿Detenerla y cerrar la aplicación?",
                parent=self.root,
            ):
                return
            self._cancelar.set()
            self._en_cierre = True
            self._actualizar_estado("Deteniendo y cerrando Chrome…")
            self.btn_detener.configure(state="disabled")
            self.root.after(150, self._cerrar_cuando_termine)
            return
        self._guardar_config()
        self.root.destroy()

    def _cerrar_cuando_termine(self):
        if self._hilo is not None and self._hilo.is_alive():
            self._intentos_cierre += 1
            # Tras unos segundos se cierra igualmente; el hilo es daemon.
            if self._intentos_cierre < 60:
                self.root.after(150, self._cerrar_cuando_termine)
                return
        self._guardar_config()
        self.root.destroy()


def main():
    _activar_conciencia_dpi()
    root = tk.Tk()
    try:
        escala = root.winfo_fpixels("1i") / 72.0
        if escala > 0:
            root.tk.call("tk", "scaling", escala)
    except Exception:
        pass

    if _ERROR_IMPORT is not None:
        root.withdraw()
        messagebox.showerror(
            "downTorrent",
            "No se pudieron cargar las dependencias del proyecto.\n\n"
            f"{_ERROR_IMPORT}\n\n"
            "Instálalas desde la carpeta del proyecto con:\n\n"
            "    pip install -r requirements.txt",
            parent=root,
        )
        root.destroy()
        return

    app = Aplicacion(root)
    root.mainloop()


if __name__ == "__main__":
    main()
