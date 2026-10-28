"""
Dictado de notas de testing/QA, 100% offline.

Escucha el micrófono, corta cada nota al detectar un silencio, la transcribe
con faster-whisper y la agrega a notas/notas_testing_YYYY-MM-DD.md.
En paralelo abre una ventanita "Dejá tus capturas acá" (pegar, arrastrar o
Win+Shift+S) y monitorea capturas/ para sumar al log las imágenes nuevas,
ubicadas en orden cronológico entre las notas.
Esc, Ctrl+C o cerrar la ventana para terminar.
"""
import argparse
import collections
import ctypes
import os
import queue
import re
import shutil
import signal
import sys
import threading
import time
import warnings
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import numpy as np
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

try:
    import msvcrt  # Solo Windows: lectura de teclas sin bloquear
except ImportError:
    msvcrt = None

try:
    import tkinter as tk
    from PIL import Image, ImageDraw, ImageGrab, ImageTk
except ImportError:  # sin ventana de capturas, el resto funciona igual
    tk = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # arrastrar y soltar archivos
except Exception:
    TkinterDnD = None

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
warnings.filterwarnings("ignore", message=".*local_dir_use_symlinks.*")

BASE = Path(__file__).resolve().parent
CARPETA_NOTAS = BASE / "notas"
CARPETA_MODELOS = BASE / "modelos"
CARPETA_CAPTURAS = BASE / "capturas"

SR_WHISPER = 16000   # Whisper trabaja a 16 kHz mono
BLOQUE_S = 0.03      # Tamaño de cada bloque de audio analizado (30 ms)
PREROLL_S = 0.3      # Audio previo que se conserva para no cortar la primera sílaba

EXTENSIONES_IMAGEN = {".png", ".jpg", ".jpeg"}

# Ayuda a Whisper con el vocabulario típico de QA
PROMPT_INICIAL = (
    "Notas de testing de software: bug, error, login, endpoint, API, "
    "pantalla, botón, formulario, validación, ambiente de QA, staging."
)

# Frases que Whisper suele "alucinar" con ruido o silencio
ALUCINACIONES = {
    "gracias", "gracias por ver", "gracias por ver el video",
    "subtitulos realizados por la comunidad de amaraorg",
    "subtitulado por la comunidad de amaraorg", "suscribete",
    "musica", "aplausos",
}

PREFIJO_FALLIDO = "(sin transcribir"
AVISO_IMAGEN_LEJANA = "_(sin nota de voz cercana)_"

PLANTILLA_PROMPT = """\
Sos un analista QA senior. Abajo hay notas que dicté mientras testeaba \
(transcripción automática por voz: puede haber palabras mal reconocidas).

Organizalas en:
1. **Bugs**: título, pasos para reproducir, resultado esperado vs. obtenido y severidad sugerida.
2. **Hallazgos / mejoras** (UX, performance, textos, etc.).
3. **Dudas o puntos a confirmar** con el equipo.

Agrupá las notas que hablen del mismo problema e indicá qué números de nota usaste en cada ítem. \
Las entradas con 📷 son capturas de pantalla que tomé en ese momento: indicá qué capturas \
corresponden a cada bug o hallazgo. \
No inventes datos que no estén en las notas: si falta información, marcalo como "a completar".

Notas del {fecha}:

{notas}
"""


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Segmentación por silencio
# --------------------------------------------------------------------------- #
class Segmentador:
    """Agrupa bloques de audio en segmentos de voz separados por silencios."""

    def __init__(self, sr, umbral, silencio_s, min_voz_s, max_s):
        self.sr = sr
        self.umbral_inicio = umbral
        self.umbral_continuar = umbral * 0.6  # histéresis: cuesta más empezar que seguir
        self.silencio_s = silencio_s
        self.min_voz_s = min_voz_s
        self.max_s = max_s
        self.preroll = collections.deque(maxlen=max(1, int(PREROLL_S / BLOQUE_S)))
        self._reset()

    def _reset(self):
        self.bloques = []
        self.inicio = None
        self.voz_s = 0.0
        self.silencio_actual_s = 0.0

    @property
    def grabando(self):
        return self.inicio is not None

    def procesar(self, bloque):
        """Devuelve un segmento (audio, hora_inicio) si se cerró uno, o None."""
        dur = len(bloque) / self.sr
        rms = float(np.sqrt(np.mean(bloque ** 2)))

        if not self.grabando:
            if rms >= self.umbral_inicio:
                self.inicio = datetime.now() - timedelta(seconds=len(self.preroll) * dur)
                self.bloques = list(self.preroll) + [bloque]
                self.voz_s = dur
                self.preroll.clear()
            else:
                self.preroll.append(bloque)
            return None

        self.bloques.append(bloque)
        if rms >= self.umbral_continuar:
            self.voz_s += dur
            self.silencio_actual_s = 0.0
        else:
            self.silencio_actual_s += dur

        duracion_total = len(self.bloques) * dur
        if self.silencio_actual_s >= self.silencio_s or duracion_total >= self.max_s:
            return self.cerrar()
        return None

    def cerrar(self):
        """Cierra el segmento en curso (si lo hay y tiene voz suficiente)."""
        if not self.grabando:
            return None
        audio, inicio, voz_s = np.concatenate(self.bloques), self.inicio, self.voz_s
        self._reset()
        if voz_s < self.min_voz_s:  # clicks, golpes, tos corta
            return None
        return audio, inicio


def remuestrear(audio, sr_origen):
    if sr_origen == SR_WHISPER:
        return audio
    n = int(len(audio) * SR_WHISPER / sr_origen)
    x_viejo = np.linspace(0, 1, num=len(audio), endpoint=False)
    x_nuevo = np.linspace(0, 1, num=n, endpoint=False)
    return np.interp(x_nuevo, x_viejo, audio).astype(np.float32)


# --------------------------------------------------------------------------- #
# Archivo de notas
# --------------------------------------------------------------------------- #
@dataclass
class Entrada:
    hora: datetime
    tipo: str            # "nota", "imagen" o "fallida"
    texto: str           # texto transcripto, nombre de la imagen o detalle del fallo
    ruta: Path = None    # solo imágenes
    orden: int = 0       # desempata entradas con la misma hora (orden de llegada)


class ArchivoNotas:
    """Log del día.

    Las notas llegan con demora (hay que transcribirlas) y las imágenes al
    instante, así que el orden de llegada no es el cronológico. Por eso las
    entradas de la sesión se guardan en memoria y el bloque de la sesión se
    reescribe completo, ordenado por hora, cada vez que entra algo.
    """

    def __init__(self, carpeta, distancia_imagen_s):
        carpeta.mkdir(parents=True, exist_ok=True)
        self.inicio = datetime.now()
        self.ruta = carpeta / f"notas_testing_{self.inicio:%Y-%m-%d}.md"
        self.carpeta_fallidos = carpeta / "fallidos"
        self.distancia_imagen = timedelta(seconds=distancia_imagen_s)
        self.encabezado_sesion = f"## Sesión {self.inicio:%H:%M:%S}"
        self.entradas = []
        self.lock = threading.Lock()
        self.guardar()

    def contar(self, tipo):
        return sum(1 for e in self.entradas if e.tipo == tipo)

    def agregar(self, hora, texto):
        self._agregar(Entrada(hora, "nota", texto))

    def agregar_imagen(self, hora, ruta):
        self._agregar(Entrada(hora, "imagen", ruta.name, ruta))

    def agregar_fallida(self, hora, audio):
        """Guarda el audio de un segmento que no se pudo transcribir."""
        try:
            self.carpeta_fallidos.mkdir(exist_ok=True)
            wav = self.carpeta_fallidos / f"{hora:%Y-%m-%d_%H-%M-%S}.wav"
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR_WHISPER)
                w.writeframes(pcm.tobytes())
            detalle = f"audio guardado en fallidos/{wav.name}"
        except Exception as e:
            detalle = f"tampoco se pudo guardar el audio: {e}"
        self._agregar(Entrada(hora, "fallida", detalle))

    def _agregar(self, entrada):
        with self.lock:
            entrada.orden = len(self.entradas)
            self.entradas.append(entrada)
        self.guardar()

    def guardar(self):
        """Reescribe el archivo. Si falla, las entradas siguen en memoria y se
        vuelven a intentar con la próxima."""
        with self.lock:
            try:
                self._escribir_atomico(self._contenido())
                return True
            except OSError as e:
                log(f"ERROR guardando {self.ruta.name} (se reintenta con la próxima entrada): {e}")
                return False

    def _contenido(self):
        try:
            actual = self.ruta.read_text(encoding="utf-8")
        except FileNotFoundError:
            actual = f"# Notas de testing - {self.inicio:%d/%m/%Y}\n\n"

        # Se conserva todo lo anterior a esta sesión (sesiones previas del día o ediciones a mano)
        i = actual.rfind(self.encabezado_sesion)
        if i != -1:
            previo = actual[:i]
        else:
            previo = actual.rstrip("\n") + "\n\n" if actual.strip() else ""

        horas_notas = [e.hora for e in self.entradas if e.tipo == "nota"]
        ordenadas = sorted(self.entradas, key=lambda e: (e.hora, e.orden))
        bloques = [self.encabezado_sesion] + [self._renderizar(e, horas_notas) for e in ordenadas]
        return previo + "\n\n".join(bloques) + "\n\n"

    def _renderizar(self, e, horas_notas):
        hora = f"[{e.hora:%H:%M:%S}]"
        if e.tipo == "nota":
            return f"{hora} {e.texto}"
        if e.tipo == "fallida":
            return f"{hora} {PREFIJO_FALLIDO}: {e.texto})"

        cercana = any(abs(e.hora - h) <= self.distancia_imagen for h in horas_notas)
        aviso = "" if cercana else f" {AVISO_IMAGEN_LEJANA}"
        try:
            ruta = Path(os.path.relpath(e.ruta, self.ruta.parent)).as_posix()
        except ValueError:  # capturas en otra unidad de disco
            ruta = e.ruta.as_posix()
        return f"{hora} 📷 Imagen: {e.texto}{aviso}\n\n![{e.texto}]({quote(ruta, safe='/:')})"

    def _escribir_atomico(self, contenido):
        """Escribe a un temporal y lo reemplaza: si se corta a la mitad, el
        archivo anterior queda intacto."""
        tmp = self.ruta.with_name(self.ruta.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(contenido)
            f.flush()
            os.fsync(f.fileno())
        for intento in range(10):
            try:
                os.replace(tmp, self.ruta)
                return
            except PermissionError:  # algún editor tiene el archivo bloqueado
                if intento == 9:
                    raise
                time.sleep(0.1)

    def generar_prompt(self):
        """Arma un .md con todas las entradas del día numeradas, listo para pegar en una IA."""
        patron = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] (.+)$")
        notas = []
        for linea in self.ruta.read_text(encoding="utf-8").splitlines():
            m = patron.match(linea.strip())
            if m and not m.group(2).startswith(PREFIJO_FALLIDO):
                notas.append(f"{len(notas) + 1}. [{m.group(1)}] {m.group(2)}")
        if not notas:
            return None
        ruta = self.ruta.with_name(self.ruta.stem + "_prompt.md")
        ruta.write_text(
            PLANTILLA_PROMPT.format(fecha=f"{self.inicio:%d/%m/%Y}", notas="\n".join(notas)),
            encoding="utf-8",
        )
        return ruta, len(notas)


# --------------------------------------------------------------------------- #
# Capturas de pantalla
# --------------------------------------------------------------------------- #
def _clave(ruta):
    """Ruta normalizada para comparar (Windows no distingue mayúsculas)."""
    return os.path.normcase(os.path.abspath(ruta))


def esperar_archivo_completo(ruta, timeout_s=10):
    """Espera a que el programa que guarda la imagen termine de escribirla."""
    fin = time.time() + timeout_s
    tam_anterior = -1
    while time.time() < fin:
        try:
            tam = ruta.stat().st_size
            if tam > 0 and tam == tam_anterior:
                with open(ruta, "rb"):
                    return True
            tam_anterior = tam
        except FileNotFoundError:  # era un temporal que se renombró o se borró
            return False
        except OSError:  # todavía bloqueado por quien lo está guardando
            pass
        time.sleep(0.3)
    return False


def nombre_disponible(ruta):
    candidato, n = ruta, 2
    while candidato.exists():
        candidato = ruta.with_name(f"{ruta.stem}_{n}{ruta.suffix}")
        n += 1
    return candidato


class MonitorCapturas(FileSystemEventHandler):
    """Detecta imágenes nuevas en la carpeta de capturas y las registra en el
    log con su hora de modificación. Acepta imágenes en la subcarpeta del día
    (capturas/YYYY-MM-DD/) o sueltas en la raíz, que se mueven a la del día."""

    def __init__(self, raiz, archivo):
        super().__init__()
        self.raiz = Path(raiz).resolve()
        self.carpeta_dia = self.raiz / f"{archivo.inicio:%Y-%m-%d}"
        self.carpeta_dia.mkdir(parents=True, exist_ok=True)
        self.archivo = archivo
        self.cola = queue.Queue()
        self.vistas = set()
        self.observer = Observer()
        self.hilo = threading.Thread(target=self._procesar_cola, daemon=True)

    def iniciar(self):
        self.observer.schedule(self, str(self.raiz), recursive=True)
        self.observer.start()
        self.hilo.start()

    def detener(self):
        """Deja de escuchar eventos y termina de registrar lo que estaba en cola."""
        time.sleep(0.5)  # deja llegar el evento de una captura guardada justo antes de cerrar
        self.observer.stop()
        self.observer.join(timeout=2)
        self.cola.put(None)
        self.hilo.join(timeout=30)

    # Los eventos corren en el hilo de watchdog: solo encolan, el trabajo pesado va aparte
    def on_created(self, event):
        if not event.is_directory:
            self._encolar(event.src_path)

    def on_moved(self, event):  # programas que guardan a un temporal y después renombran
        if not event.is_directory:
            self._encolar(event.dest_path)

    def _encolar(self, ruta):
        ruta = Path(os.fsdecode(ruta))
        if ruta.suffix.lower() in EXTENSIONES_IMAGEN:
            self.cola.put(ruta)

    def _procesar_cola(self):
        while (ruta := self.cola.get()) is not None:
            try:
                self._registrar(ruta)
            except Exception as e:
                log(f"ERROR registrando la imagen {ruta.name}: {e}")

    def _registrar(self, ruta):
        if _clave(ruta) in self.vistas or not ruta.exists():
            return  # evento repetido, o ya se movió a la carpeta del día
        carpeta = _clave(ruta.parent)
        en_raiz = carpeta == _clave(self.raiz)
        if not en_raiz and carpeta != _clave(self.carpeta_dia):
            return  # carpeta de otro día u otra subcarpeta: no es de esta sesión
        if not esperar_archivo_completo(ruta):
            if ruta.exists():
                log(f"La imagen {ruta.name} no terminó de guardarse a tiempo; se ignora.")
            return

        hora = datetime.fromtimestamp(ruta.stat().st_mtime)
        if en_raiz:
            # En la raíz no se marca como vista: la herramienta de capturas puede
            # volver a guardar otra imagen con el mismo nombre una vez que esta se movió
            destino = nombre_disponible(self.carpeta_dia / ruta.name)
            self.vistas.add(_clave(destino))  # para ignorar el evento que genera el propio movimiento
            try:
                shutil.move(ruta, destino)
                ruta = destino
            except OSError as e:
                self.vistas.add(_clave(ruta))
                log(f"No se pudo mover {ruta.name} a {self.carpeta_dia.name}/ ({e}); queda donde está.")
        else:
            self.vistas.add(_clave(ruta))

        self.archivo.agregar_imagen(hora, ruta)
        log(f"📷 Imagen registrada: {ruta.name} (hora {hora:%H:%M:%S})")


def secuencia_portapapeles():
    """Número que Windows incrementa cada vez que cambia el portapapeles."""
    try:
        return ctypes.windll.user32.GetClipboardSequenceNumber()
    except AttributeError:  # no es Windows
        return 0


TEMA = {
    "fondo": "#1b1d21",
    "superficie": "#23262c",
    "superficie_activa": "#1d2a40",
    "borde": "#3b4049",
    "acento": "#4f8cff",
    "texto": "#e6e8eb",
    "texto_suave": "#9aa1ab",
    "texto_tenue": "#6b7280",
    "exito": "#4ade80",
    "error": "#f87171",
    "grabando": "#ef4444",
    "grabando_tenue": "#5a2323",
    "boton": "#2c2f36",
    "boton_hover": "#3a2a2d",
}
FUENTE = ("Segoe UI", 9)
FUENTE_NEGRITA = ("Segoe UI Semibold", 9)
FUENTE_TITULO = ("Segoe UI Semibold", 12)


def imagen_interruptor(encendido):
    """Switch redondeado dibujado con Pillow a 4x y achicado, para que tenga bordes suaves."""
    escala, ancho, alto = 4, 34, 18
    a, h = ancho * escala, alto * escala
    img = Image.new("RGBA", (a, h), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(img)
    dibujo.rounded_rectangle((0, 0, a - 1, h - 1), radius=h // 2,
                             fill=TEMA["acento"] if encendido else TEMA["borde"])
    margen = 3 * escala
    x = a - h if encendido else 0
    dibujo.ellipse((x + margen, margen, x + h - margen, h - margen), fill="#ffffff")
    return ImageTk.PhotoImage(img.resize((ancho, alto), Image.LANCZOS))


def imagen_icono():
    """Ícono de la ventana: punto de grabación sobre fondo oscuro."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(img)
    dibujo.rounded_rectangle((0, 0, 63, 63), radius=14, fill=TEMA["superficie"])
    dibujo.ellipse((18, 18, 46, 46), fill=TEMA["grabando"])
    return ImageTk.PhotoImage(img)


class Interruptor(tk.Frame):
    """Switch on/off con texto. Reemplaza al Checkbutton, que en Windows no se puede oscurecer bien."""

    def __init__(self, padre, texto, variable, al_cambiar=None):
        super().__init__(padre, bg=TEMA["fondo"], cursor="hand2")
        self.variable = variable
        self.al_cambiar = al_cambiar
        self.imagenes = {True: imagen_interruptor(True), False: imagen_interruptor(False)}
        self.switch = tk.Label(self, bg=TEMA["fondo"], cursor="hand2")
        self.etiqueta = tk.Label(self, text=texto, font=FUENTE, bg=TEMA["fondo"],
                                 fg=TEMA["texto"], cursor="hand2")
        self.switch.pack(side="left")
        self.etiqueta.pack(side="left", padx=(8, 0))
        for widget in (self, self.switch, self.etiqueta):
            widget.bind("<Button-1>", self._alternar)
        self.actualizar()

    def _alternar(self, _evento):
        self.variable.set(not self.variable.get())
        self.actualizar()
        if self.al_cambiar:
            self.al_cambiar()

    def actualizar(self):
        self.switch.config(image=self.imagenes[self.variable.get()])


class Boton(tk.Label):
    """Botón plano con hover (tk.Button no respeta bien los colores en Windows)."""

    def __init__(self, padre, texto, comando):
        super().__init__(padre, text=texto, font=FUENTE_NEGRITA, bg=TEMA["boton"],
                         fg=TEMA["error"], pady=7, cursor="hand2")
        self.bind("<Enter>", lambda e: self.config(bg=TEMA["boton_hover"]))
        self.bind("<Leave>", lambda e: self.config(bg=TEMA["boton"]))
        self.bind("<ButtonRelease-1>", lambda e: comando())


class VentanaCapturas:
    """Ventana flotante "Dejá tus capturas acá".

    Solo guarda las imágenes en la carpeta del día: de ahí las toma
    MonitorCapturas, que las ubica en el log en orden cronológico.
    Las capturas pegadas usan la hora en que se guardan; los archivos
    arrastrados conservan su fecha de modificación.
    """

    INTERVALO_MS = 300
    ANCHO_ZONA, ALTO_ZONA = 300, 200

    def __init__(self, carpeta, detener, resumen=None):
        if tk is None:
            raise RuntimeError("falta tkinter o Pillow")
        self.carpeta = carpeta
        self.detener = detener
        self.resumen = resumen  # función que devuelve (notas, imágenes) de la sesión
        self.ultima_seq = secuencia_portapapeles()  # lo que ya estaba copiado no cuenta
        self.miniatura = None
        self.ticks = 0

        self.root, con_arrastre = self._crear_raiz()
        self.root.withdraw()  # se muestra recién cuando está armada y en su lugar
        self.root.title("Capturas QA")
        self.root.configure(bg=TEMA["fondo"])
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._cerrar)
        self.icono = imagen_icono()
        self.root.iconphoto(True, self.icono)
        self.var_auto = tk.BooleanVar(value=True)
        self.var_adelante = tk.BooleanVar(value=True)

        # Encabezado: indicador de grabación y contadores
        cabecera = tk.Frame(self.root, bg=TEMA["fondo"])
        cabecera.pack(fill="x", padx=14, pady=(12, 8))
        self.punto = tk.Label(cabecera, text="●", font=("Segoe UI", 10),
                              bg=TEMA["fondo"], fg=TEMA["grabando"])
        self.punto.pack(side="left")
        tk.Label(cabecera, text="Escuchando", font=FUENTE_NEGRITA,
                 bg=TEMA["fondo"], fg=TEMA["texto"]).pack(side="left", padx=(4, 0))
        self.contadores = tk.Label(cabecera, font=FUENTE, bg=TEMA["fondo"], fg=TEMA["texto_suave"])
        self.contadores.pack(side="right")

        # Zona donde se sueltan las capturas
        a, h = self.ANCHO_ZONA, self.ALTO_ZONA
        self.zona = tk.Canvas(self.root, width=a, height=h, bg=TEMA["fondo"], highlightthickness=0)
        self.zona.pack(padx=14)
        self.zona.create_rectangle(2, 2, a - 2, h - 2, width=2, dash=(6, 4), tags="marco")
        self.zona.create_text(a / 2, 30, text="Dejá tus capturas acá",
                              font=FUENTE_TITULO, fill=TEMA["texto"])
        ayuda = "Sacá una con Win+Shift+S, pegá con Ctrl+V"
        if con_arrastre:
            ayuda += "\no arrastrá archivos hasta acá"
        self.zona.create_text(a / 2, 62, text=ayuda, font=FUENTE,
                              fill=TEMA["texto_suave"], justify="center")
        self.vacia = self.zona.create_text(a / 2, 140, text="Todavía no hay capturas",
                                           font=FUENTE, fill=TEMA["texto_tenue"])
        self.vista = self.zona.create_image(a / 2, 142)
        self._resaltar(False)

        self.estado = tk.Label(self.root, text="Lista para recibir capturas", font=FUENTE,
                               bg=TEMA["fondo"], fg=TEMA["texto_tenue"], anchor="w")
        self.estado.pack(fill="x", padx=14, pady=(8, 6))
        self.interruptor_auto = Interruptor(self.root, "Guardar sola cada imagen copiada", self.var_auto)
        self.interruptor_auto.pack(fill="x", padx=14, pady=3)
        Interruptor(self.root, "Siempre adelante", self.var_adelante,
                    self._aplicar_adelante).pack(fill="x", padx=14, pady=3)
        Boton(self.root, "■  Terminar sesión", self._cerrar).pack(fill="x", padx=14, pady=(12, 14))

        self.root.bind("<Control-v>", lambda e: self._revisar_portapapeles(manual=True))
        self.root.bind("<Control-V>", lambda e: self._revisar_portapapeles(manual=True))
        if con_arrastre:
            self.zona.drop_target_register(DND_FILES)
            self.zona.dnd_bind("<<Drop>>", self._al_soltar)
            self.zona.dnd_bind("<<DropEnter>>", lambda e: self._resaltar(True) or e.action)
            self.zona.dnd_bind("<<DropLeave>>", lambda e: self._resaltar(False) or e.action)

        self._aplicar_adelante()
        self._actualizar_contadores()
        # Abajo a la derecha, para que no tape lo que se está testeando
        self.root.update_idletasks()
        ancho, alto = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.geometry(f"+{self.root.winfo_screenwidth() - ancho - 24}"
                           f"+{self.root.winfo_screenheight() - alto - 72}")
        self._barra_de_titulo_oscura()
        self.root.deiconify()

    @staticmethod
    def _crear_raiz():
        if TkinterDnD is not None:
            try:
                return TkinterDnD.Tk(), True
            except Exception:  # no cargó tkdnd: sin arrastrar y soltar
                pass
        return tk.Tk(), False

    def _barra_de_titulo_oscura(self):
        """Pinta la barra de título de Windows en oscuro (Windows 10 1809+ y 11)."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dwm = ctypes.windll.dwmapi
            oscuro = ctypes.c_int(1)
            for atributo in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 en Windows 10 viejos)
                if dwm.DwmSetWindowAttribute(hwnd, atributo, ctypes.byref(oscuro),
                                             ctypes.sizeof(oscuro)) == 0:
                    break
            r, g, b = (int(TEMA["fondo"][i:i + 2], 16) for i in (1, 3, 5))
            color = ctypes.c_int(r | g << 8 | b << 16)  # DWMWA_CAPTION_COLOR, solo Windows 11
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
        except Exception:
            pass

    def ejecutar(self):
        self.root.after(self.INTERVALO_MS, self._tick)
        self.root.mainloop()

    def _cerrar(self):
        self.detener.set()
        self.root.destroy()

    def _tick(self):
        if self.detener.is_set():  # se terminó desde la consola (Esc o Ctrl+C)
            self.root.destroy()
            return
        self.ticks += 1
        if self.ticks % 2 == 0:  # el punto de grabación late
            tenue = self.ticks % 4 == 0
            self.punto.config(fg=TEMA["grabando_tenue"] if tenue else TEMA["grabando"])
        self._actualizar_contadores()
        if self.var_auto.get():
            self._revisar_portapapeles()
        self.root.after(self.INTERVALO_MS, self._tick)

    def _actualizar_contadores(self):
        if self.resumen is None:
            return
        notas, imagenes = self.resumen()
        self.contadores.config(text=f"{notas} nota{'s' * (notas != 1)}  ·  "
                                    f"{imagenes} captura{'s' * (imagenes != 1)}")

    def _aplicar_adelante(self):
        self.root.attributes("-topmost", self.var_adelante.get())

    def _resaltar(self, activo):
        self.zona.itemconfig("marco",
                             outline=TEMA["acento"] if activo else TEMA["borde"],
                             fill=TEMA["superficie_activa"] if activo else TEMA["superficie"])

    def _mostrar_estado(self, texto, error=False):
        self.estado.config(text=texto, fg=TEMA["error"] if error else TEMA["exito"])

    def _revisar_portapapeles(self, manual=False):
        seq = secuencia_portapapeles()
        if not manual and seq == self.ultima_seq:
            return
        try:
            contenido = ImageGrab.grabclipboard()
        except Exception:
            return  # otro programa tiene el portapapeles abierto: se reintenta en el próximo tick
        ya_vista = seq == self.ultima_seq
        self.ultima_seq = seq

        if isinstance(contenido, Image.Image):
            if manual and ya_vista:
                self._mostrar_estado("Esa captura ya está guardada")
            else:
                self._guardar_imagen(contenido)
        elif manual and isinstance(contenido, list):  # archivos copiados en el Explorador
            self._agregar_archivos(contenido)
        elif manual:
            self._mostrar_estado("El portapapeles no tiene ninguna imagen", error=True)

    def _al_soltar(self, event):
        self._resaltar(False)
        self._agregar_archivos(self.root.tk.splitlist(event.data))
        return event.action

    def _guardar_imagen(self, imagen):
        try:
            destino = nombre_disponible(self.carpeta / f"captura_{datetime.now():%H-%M-%S}.png")
            # Se escribe con otro nombre y se renombra, para que el monitor la vea completa
            tmp = destino.with_name(destino.name + ".tmp")
            imagen.save(tmp, format="PNG")
            os.replace(tmp, destino)
        except Exception as e:
            log(f"ERROR guardando la captura pegada: {e}")
            self._mostrar_estado(f"No se pudo guardar: {e}", error=True)
            return
        self._mostrar(destino, imagen)

    def _agregar_archivos(self, rutas):
        ignoradas = 0
        for ruta in map(Path, rutas):
            if ruta.suffix.lower() not in EXTENSIONES_IMAGEN or not ruta.is_file():
                ignoradas += 1
                continue
            if _clave(ruta.parent) == _clave(self.carpeta):
                continue  # ya está en la carpeta del día
            try:
                destino = nombre_disponible(self.carpeta / ruta.name)
                tmp = destino.with_name(destino.name + ".tmp")
                shutil.copy2(ruta, tmp)  # copy2 conserva la fecha de modificación
                os.replace(tmp, destino)
                with Image.open(destino) as imagen:
                    self._mostrar(destino, imagen)
            except Exception as e:
                log(f"ERROR copiando {ruta.name}: {e}")
                self._mostrar_estado(f"No se pudo copiar {ruta.name}", error=True)
        if ignoradas:
            self._mostrar_estado(f"{ignoradas} archivo(s) ignorado(s): solo PNG o JPG", error=True)

    def _mostrar(self, ruta, imagen):
        miniatura = imagen.copy()
        miniatura.thumbnail((self.ANCHO_ZONA - 40, 96))
        self.miniatura = ImageTk.PhotoImage(miniatura)  # hay que guardar la referencia o Tk la pierde
        self.zona.itemconfig(self.vista, image=self.miniatura)
        self.zona.itemconfig(self.vacia, state="hidden")
        self._mostrar_estado(f"✓ {ruta.name}")


# --------------------------------------------------------------------------- #
# Modelo y transcripción
# --------------------------------------------------------------------------- #
def ruta_modelo(nombre):
    """Devuelve la carpeta local del modelo; la descarga solo si no existe."""
    destino = CARPETA_MODELOS / nombre
    if (destino / "model.bin").exists():
        return str(destino)
    from faster_whisper import download_model
    log(f"Descargando modelo '{nombre}' (solo la primera vez, requiere internet)...")
    download_model(nombre, output_dir=str(destino))
    log(f"Modelo guardado en {destino}")
    return str(destino)


def cargar_modelo(nombre):
    from faster_whisper import WhisperModel
    ruta = ruta_modelo(nombre)
    log(f"Cargando modelo '{nombre}'...")
    return WhisperModel(ruta, device="cpu", compute_type="int8")


def es_alucinacion(texto):
    normalizado = re.sub(r"[^\w\s]", "", texto.lower())
    normalizado = normalizado.translate(str.maketrans("áéíóú", "aeiou")).strip()
    return not normalizado or normalizado in ALUCINACIONES or "amara" in normalizado


def transcribir(modelo, audio):
    segmentos, _ = modelo.transcribe(
        audio,
        language="es",
        beam_size=5,
        vad_filter=True,  # descarta ruido que no sea voz
        condition_on_previous_text=False,
        initial_prompt=PROMPT_INICIAL,
    )
    return " ".join(s.text.strip() for s in segmentos).strip()


def trabajador_transcripcion(modelo, cola, archivo):
    while True:
        item = cola.get()
        if item is None:
            break
        audio, hora = item
        try:
            texto = transcribir(modelo, audio)
        except Exception as e:
            log(f"ERROR transcribiendo el segmento de las {hora:%H:%M:%S}: {e}")
            archivo.agregar_fallida(hora, audio)
            continue
        if es_alucinacion(texto):
            continue
        archivo.agregar(hora, texto)
        print(f"  [{hora:%H:%M:%S}] {texto}", flush=True)


# --------------------------------------------------------------------------- #
# Micrófono
# --------------------------------------------------------------------------- #
def elegir_samplerate(sd, dispositivo):
    """Intenta 16 kHz; si el micrófono no lo soporta, usa su frecuencia nativa."""
    try:
        sd.check_input_settings(device=dispositivo, channels=1, samplerate=SR_WHISPER, dtype="float32")
        return SR_WHISPER
    except Exception:
        info = sd.query_devices(dispositivo, kind="input")
        return int(info["default_samplerate"])


def calibrar(cola, sr, segundos=1.0):
    """Mide el ruido ambiente para fijar el umbral de voz."""
    rms = []
    fin = time.time() + segundos
    while time.time() < fin:
        try:
            bloque = cola.get(timeout=0.5)
        except queue.Empty:
            continue
        rms.append(float(np.sqrt(np.mean(bloque ** 2))))
    return float(np.median(rms)) if rms else 0.0


def esc_presionado():
    if msvcrt is None:
        return False
    while msvcrt.kbhit():
        if msvcrt.getwch() == "\x1b":
            return True
    return False


def escuchar(cola_audio, sr, args, encolar, detener):
    """Hilo de audio: calibra, segmenta y encola notas hasta que se pida terminar."""
    log("Calibrando ruido ambiente, no hables por 1 segundo...")
    ruido = calibrar(cola_audio, sr)
    umbral = args.umbral if args.umbral is not None else max(ruido * 3.0, 0.01)
    log(f"Umbral de voz: {umbral:.4f} (ruido ambiente: {ruido:.4f})")
    segmentador = Segmentador(sr, umbral, args.silencio, min_voz_s=0.4, max_s=args.max_segundos)
    log("Escuchando. Hablá cuando quieras.")

    while not detener.is_set():
        if esc_presionado():
            detener.set()
            break
        try:
            bloque = cola_audio.get(timeout=0.1)
        except queue.Empty:
            continue
        segmento = segmentador.procesar(bloque)
        if segmento:
            encolar(segmento)

    # Lo que quedó grabando al momento de cortar también se transcribe
    segmento = segmentador.cerrar()
    if segmento:
        encolar(segmento)


# --------------------------------------------------------------------------- #
def parsear_args():
    p = argparse.ArgumentParser(description="Dictado offline de notas de testing/QA.")
    p.add_argument("--modelo", default="small", help="tiny, base, small, medium... (default: small)")
    p.add_argument("--dispositivo", default=None,
                   help="Índice o nombre del micrófono (ver --listar-dispositivos)")
    p.add_argument("--listar-dispositivos", action="store_true", help="Lista los micrófonos y sale")
    p.add_argument("--silencio", type=float, default=1.2,
                   help="Segundos de silencio que cierran una nota (default: 1.2)")
    p.add_argument("--umbral", type=float, default=None,
                   help="Umbral RMS de voz (default: automático según el ruido ambiente)")
    p.add_argument("--max-segundos", type=float, default=30.0,
                   help="Duración máxima de una nota antes de cortarla (default: 30)")
    p.add_argument("--carpeta-capturas", default=str(CARPETA_CAPTURAS),
                   help="Carpeta donde se dejan las imágenes (default: ./capturas)")
    p.add_argument("--distancia-imagen", type=float, default=120.0,
                   help="Segundos sin notas alrededor de una imagen para marcarla como "
                        "'sin nota de voz cercana' (default: 120)")
    p.add_argument("--sin-ventana", action="store_true",
                   help="No abrir la ventana de capturas (solo se vigila la carpeta)")
    p.add_argument("--solo-descargar", action="store_true", help="Descarga el modelo y sale")
    args = p.parse_args()
    if args.dispositivo is not None and args.dispositivo.isdigit():
        args.dispositivo = int(args.dispositivo)
    return args


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parsear_args()

    if args.solo_descargar:
        ruta_modelo(args.modelo)
        return 0

    try:
        import sounddevice as sd
    except OSError as e:
        log(f"No se pudo inicializar el audio (PortAudio): {e}")
        return 1

    if args.listar_dispositivos:
        print(sd.query_devices())
        return 0

    try:
        info_mic = sd.query_devices(args.dispositivo, kind="input")
    except Exception as e:
        log(f"No se detectó ningún micrófono ({e}).")
        log("Conectá uno o elegí otro con --dispositivo (ver --listar-dispositivos).")
        return 1

    try:
        modelo = cargar_modelo(args.modelo)
    except Exception as e:
        log(f"No se pudo cargar el modelo '{args.modelo}': {e}")
        log("Si es la primera vez, necesitás internet para descargarlo (corré instalar.bat).")
        return 1

    archivo = ArchivoNotas(CARPETA_NOTAS, args.distancia_imagen)
    cola_audio = queue.Queue()
    cola_transcripcion = queue.Queue()
    trabajador = threading.Thread(
        target=trabajador_transcripcion, args=(modelo, cola_transcripcion, archivo), daemon=True
    )
    trabajador.start()

    def callback(indata, frames, tiempo, status):
        cola_audio.put(indata[:, 0].copy())

    def encolar(segmento):
        audio, hora = segmento
        audio = remuestrear(audio, sr)
        log(f"Nota detectada ({len(audio) / SR_WHISPER:.1f} s), transcribiendo...")
        cola_transcripcion.put((audio, hora))

    sr = SR_WHISPER
    try:
        sr = elegir_samplerate(sd, args.dispositivo)
        stream = sd.InputStream(
            device=args.dispositivo, channels=1, samplerate=sr, dtype="float32",
            blocksize=int(sr * BLOQUE_S), callback=callback,
        )
        stream.start()
    except Exception as e:
        log(f"No se pudo abrir el micrófono '{info_mic['name']}': {e}")
        cola_transcripcion.put(None)
        return 1

    monitor = None
    try:
        monitor = MonitorCapturas(args.carpeta_capturas, archivo)
        monitor.iniciar()
    except Exception as e:
        log(f"No se pudo monitorear la carpeta de capturas ({e}); sigo solo con audio.")
        monitor = None

    detener = threading.Event()
    ventana = None
    if monitor and not args.sin_ventana:
        try:
            ventana = VentanaCapturas(monitor.carpeta_dia, detener, resumen=lambda: (
                archivo.contar("nota"), archivo.contar("imagen")))
        except Exception as e:
            log(f"No se pudo abrir la ventana de capturas ({e}); quedan la carpeta y el audio.")

    print(f"\n  Micrófono: {info_mic['name']}")
    print(f"  Las notas se guardan en: {archivo.ruta}")
    if ventana:
        print("  Capturas: usá la ventana 'Dejá tus capturas acá' (abajo a la derecha).")
    if monitor:
        print(f"  También podés dejar imágenes en: {monitor.raiz}")
    print("  Para terminar: Esc o Ctrl+C acá, o 'Terminar sesión' en la ventana.\n", flush=True)

    # Ctrl+C solo avisa que hay que terminar: el cierre ordenado lo hace este hilo
    signal.signal(signal.SIGINT, lambda *_: detener.set())
    hilo_escucha = threading.Thread(
        target=escuchar, args=(cola_audio, sr, args, encolar, detener), daemon=True
    )
    hilo_escucha.start()
    try:
        if ventana:
            ventana.ejecutar()  # Tk tiene que correr en el hilo principal
        while not detener.wait(0.2):
            pass
    except Exception as e:
        log(f"ERROR en la ventana de capturas: {e}")
        detener.set()
    hilo_escucha.join()
    stream.stop()
    stream.close()
    signal.signal(signal.SIGINT, signal.default_int_handler)  # un segundo Ctrl+C cancela lo pendiente

    try:
        if monitor:
            monitor.detener()
        pendientes = cola_transcripcion.qsize()
        if pendientes:
            log(f"Terminando de transcribir {pendientes} nota(s) pendiente(s)...")
        cola_transcripcion.put(None)
        while trabajador.is_alive():
            trabajador.join(timeout=0.2)
    except KeyboardInterrupt:
        log("Cancelado: las notas pendientes no se transcribieron.")
    archivo.guardar()

    print("\n" + "=" * 60)
    print(f"  Notas grabadas en esta sesión: {archivo.contar('nota')}")
    print(f"  Imágenes registradas: {archivo.contar('imagen')}")
    if archivo.contar("fallida"):
        print(f"  Segmentos que fallaron (audio en notas/fallidos): {archivo.contar('fallida')}")
    print(f"  Log: {archivo.ruta}")
    try:
        resultado = archivo.generar_prompt()
        if resultado:
            ruta_prompt, total = resultado
            print(f"  Prompt para IA ({total} entradas del día): {ruta_prompt}")
    except Exception as e:
        print(f"  No se pudo generar el archivo de prompt: {e}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
