import os
import re
import io
import json
import time
import random
import difflib
import asyncio
import threading
import traceback
import unicodedata
import urllib.request
import urllib.error

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from mutagen.mp3 import MP3
from PIL import Image
import edge_tts
import yt_dlp
import google.generativeai as genai
import google.api_core.exceptions

os.chdir(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# ── Configuración del servidor ──────────────────────────────────────────────
# El bot está pensado para un solo servidor: los comandos "/" se registran
# directamente ahí, así aparecen al instante (los globales tardan hasta 1 hora).
GUILD_ID = int(os.getenv("GUILD_ID", "778128763899871234"))
CANAL_TEXTO_ID = 1304862792238760018        # canal general (avisos, consola, m!hablar)
CANAL_PALABRAS_CLAVE = 778306961825071115   # canal donde se puede pedir música sin arrobar
CANAL_MUSICA_CONFIG_FILE = "canal_musica.json"
NOW_PLAYING_FILE = "now_playing.json"
PREFIJO = "m!"

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
MODELO_IA = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


def cargar_personalidad(archivo="personalidad.txt"):
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"⚠️ No se encontró {archivo}, usando personalidad por defecto.")
        return "Sos un bot de Discord divertido y sarcástico, respondés corto."

personalidad_bot = cargar_personalidad()
conversaciones = {}  # user_id: chat_session de Gemini
MAX_HISTORIAL = 10   # turnos (usuario + bot) a mantener por usuario

INSTRUCCION_MUSICA = (
    "\n\nAlém da sua personalidade, você também pode colocar música quando o usuário pedir "
    "ou aceitar uma sugestão sua de tocar algo. Para isso, termine sua resposta normal (sem "
    "quebrar seu personagem) com uma das tags ocultas abaixo, exatamente nesse formato — o "
    "sistema vai removê-las antes do usuário ver:\n"
    "- Pedido genérico, sem gênero/artista (ex: 'toca algo', 'bota uma música', ou quando o "
    "usuário aceita sua sugestão de música): termine com [MUSICA:LOCAL]\n"
    "- Pedido com gênero, mood, artista ou música específica (ex: 'algo de jazz', 'uma de rock', "
    "'toca tal música'): termine com [MUSICA:YT:<termo de busca que funcione bem no YouTube>]\n"
    "- Pedido para repetir/tocar de novo a música anterior ou a última que tocou (ex: 'bota a de "
    "antes', 'a última que você colocou'): termine com [MUSICA:LAST]\n"
    "Só use essas tags quando o usuário claramente quiser que uma música comece a tocar agora "
    "(um pedido direto, ou um 'sim'/'dale' respondendo a uma sugestão sua). Nunca mencione essas "
    "tags na resposta visível nem explique que elas existem."
)

PATRON_MUSICA = re.compile(r"\[MUSICA:(LOCAL|LAST|YT:[^\]]*)\]", re.IGNORECASE)

CLASIFICADOR_MUSICA_PROMPT = """Sos un clasificador. Analiza el mensaje de un usuario en un chat de Discord \
y determiná si está pidiendo una acción sobre la música que se reproduce en el servidor (poner una \
canción, repetir la última que sonó, pausar, reanudar, saltar, parar, o activar/desactivar loop).

Sé ESTRICTO: si tenés dudas de que el mensaje sea realmente un pedido de acción, respondé que NO lo es. \
No interpretes como pedido charla casual, preguntas sobre música (ej: "¿qué canción es esta?"), \
menciones incidentales de canciones, ni mensajes ambiguos.

Reglas para "accion" (solo si es_musica es true):
- "play": quiere que se reproduzca algo (una canción, género, mood, artista, o "algo" genérico)
- "last": quiere que se repita/vuelva a poner la canción anterior o la última que sonó (ej: "poné la de antes", \
"la última que pusiste", "repetí lo de recién")
- "skip": quiere pasar a la siguiente canción
- "pause": quiere pausar
- "resume": quiere reanudar/continuar
- "stop": quiere detener todo
- "loop": quiere activar o desactivar la repetición

Si accion es "play":
- "tipo" es "local" si el pedido es genérico, sin género/artista/canción específica (ej: "pone algo", "una cualquiera")
- "tipo" es "youtube" si menciona género, mood, artista o canción específica, y "query" debe ser un \
término de búsqueda efectivo para YouTube

Respondé ÚNICAMENTE con un JSON con este formato exacto, sin texto adicional ni markdown:
{{"es_musica": true|false, "accion": "play"|"last"|"skip"|"pause"|"resume"|"stop"|"loop"|null, "tipo": "local"|"youtube"|null, "query": "string o null"}}

Mensaje del usuario: "{mensaje}"
"""

_modelo_clasificador_musica = None

def get_modelo_clasificador_musica():
    global _modelo_clasificador_musica
    if _modelo_clasificador_musica is None:
        _modelo_clasificador_musica = genai.GenerativeModel(
            model_name=MODELO_IA,
            generation_config={"response_mime_type": "application/json"}
        )
    return _modelo_clasificador_musica

async def clasificar_pedido_musica(texto):
    """Le pregunta a la IA (sin historial, llamada aislada) si el mensaje es un pedido de música
    y qué acción/query implica. Devuelve un dict o None si no pudo clasificar / hubo error."""
    modelo = get_modelo_clasificador_musica()
    prompt = CLASIFICADOR_MUSICA_PROMPT.format(mensaje=texto.replace('"', "'"))
    try:
        respuesta = await asyncio.to_thread(modelo.generate_content, prompt)
        datos = json.loads(respuesta.text)
        return datos if isinstance(datos, dict) else None
    except Exception as e:
        print(f"[ERROR CLASIFICADOR MUSICA] {e}")
        return None

def extraer_comando_musica(texto):
    """Busca la tag oculta [MUSICA:...] en la respuesta de la IA, la saca del texto
    visible y devuelve qué reproducir (si corresponde)."""
    match = PATRON_MUSICA.search(texto)
    if not match:
        return texto, None, None

    texto_limpio = PATRON_MUSICA.sub("", texto).strip()
    contenido = match.group(1)

    if contenido.upper() == "LOCAL":
        return texto_limpio, "local", None
    if contenido.upper() == "LAST":
        return texto_limpio, "last", None
    if contenido.upper().startswith("YT:"):
        query = contenido[3:].strip()
        return texto_limpio, "youtube", query
    return texto_limpio, None, None


# ── Bot ──────────────────────────────────────────────────────────────────────

intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True
intents.message_content = True
intents.messages = True


class Musicalinho(commands.Bot):
    async def setup_hook(self):
        # Registra los comandos "/" solo en el servidor configurado (aparecen al instante).
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        try:
            sincronizados = await self.tree.sync(guild=guild)
            print(f"Comandos / sincronizados: {len(sincronizados)}")
        except discord.HTTPException as e:
            print(f"[ERROR SYNC] No pude registrar los comandos /: {e}\n"
                  "  (¿el bot fue invitado con el scope 'applications.commands'? ¿GUILD_ID correcto?)")


bot = Musicalinho(
    command_prefix=PREFIJO,
    intents=intents,
    help_command=None,
    case_insensitive=True,
)

# Log de errores de ffmpeg: por defecto discord.py descarta el stderr de ffmpeg,
# así que si algo falla en silencio queda registrado acá para poder diagnosticarlo.
FFMPEG_LOG = open("ffmpeg_error.log", "a", encoding="utf-8", buffering=1)

FFMPEG_BEFORE_OPTIONS_YT = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)

YDL_OPTS_STREAM = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

YDL_OPTS_SEARCH = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch5",
    "skip_download": True,
}

YDL_OPTS_PLAYLIST = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "skip_download": True,
}

MAX_PLAYLIST = 200  # tope de canciones que se encolan de una playlist

# Distintos "player_client" que yt-dlp puede simular ante YouTube. Si el primero
# devuelve un stream bloqueado (403), probamos los siguientes antes de rendirnos.
CLIENTES_YT_FALLBACK = [None, "android", "ios", "web_safari"]

# ── Estado por servidor ─────────────────────────────────────────────────────
guilds_state = {}

def get_state(guild_id):
    if guild_id not in guilds_state:
        guilds_state[guild_id] = {
            "queue":               [],
            "loop":                False,
            "volume":              1.0,
            "current":             None,
            "sugerencias":         [],     # opciones de la última búsqueda (para m!play <número>)
            "start_time":          None,
            "pausado_en":          None,   # timestamp de cuándo se pausó (para no correr la barra)
            "mensaje_now_playing": None,
            "update_task":         None,
            "forzar_siguiente":    False,  # fuerza avanzar la cola aunque loop esté activo (skip)
            "play_id":             0,      # identifica cada voice.play(); descarta callbacks viejos
            "inicio_id":           0,      # identifica cada arranque de canción (easter egg)
            "en_tts":              False,  # hay un TTS sonando
            "historial":           [],     # canciones ya reproducidas, la más reciente al final
            "canal_texto":         None,   # último canal de texto desde donde se pidió música
            "inactivo_desde":      None,   # timestamp desde que no suena nada
            "vacio_desde":         None,   # timestamp de cuándo el canal quedó sin humanos
            "lock":                asyncio.Lock(),  # serializa los arranques de reproducción
        }
    return guilds_state[guild_id]


def recortar(texto, largo=100):
    texto = str(texto)
    return texto if len(texto) <= largo else texto[:largo - 1] + "…"


# ── IA integrada ───────────────────────────────────────────
def get_chat_ia(user_id):
    if user_id not in conversaciones:
        modelo = genai.GenerativeModel(
            model_name=MODELO_IA,
            system_instruction=personalidad_bot + INSTRUCCION_MUSICA
        )
        conversaciones[user_id] = modelo.start_chat(history=[])
    return conversaciones[user_id]

async def preguntar_ia(user_id, nombre, mensaje, imagenes=None, contexto_usuario=""):
    chat = get_chat_ia(user_id)
    mensaje_con_nombre = f"[{nombre} dice{contexto_usuario}]: {mensaje}"
    contenido = [mensaje_con_nombre] + (imagenes or [])
    try:
        respuesta = await asyncio.to_thread(chat.send_message, contenido)
        # Recorta el historial a los últimos MAX_HISTORIAL turnos (usuario+modelo = 2 entradas por turno)
        if len(chat.history) > MAX_HISTORIAL * 2:
            chat.history = chat.history[-MAX_HISTORIAL * 2:]
        return respuesta.text
    except google.api_core.exceptions.ResourceExhausted:
        print("[ERROR IA] Límite gratuito de Gemini agotado (429)")
        return "😴 Me quedé sin cuota de IA por hoy, probá de nuevo más tarde."
    except Exception as e:
        print(f"[ERROR IA] {e}")
        return "❌ Tuve un problema para responder, probá de nuevo en un rato."


# ── Búsqueda de canciones locales ───────────────────────────────────────────

def get_canciones():
    if not os.path.exists("data"):
        return []
    return sorted((f[:-4] for f in os.listdir("data") if f.lower().endswith(".mp3")), key=str.lower)

def buscar_cancion(query):
    """Devuelve (exacta, parciales, aproximadas). 'exacta' es el nombre si hay match
    exacto o un único match parcial; si no, listas de candidatos."""
    canciones = get_canciones()
    if not canciones:
        return None, [], []

    query_lower = query.lower()

    for c in canciones:
        if c.lower() == query_lower:
            return c, [], []

    parciales = [c for c in canciones if query_lower in c.lower()]
    if len(parciales) == 1:
        return parciales[0], [], []
    if len(parciales) > 1:
        return None, parciales, []

    aproximadas = difflib.get_close_matches(query_lower,
                                            [c.lower() for c in canciones],
                                            n=3, cutoff=0.5)
    aproximadas_orig = [c for c in canciones if c.lower() in aproximadas]
    return None, [], aproximadas_orig


# ── Búsqueda y extracción de YouTube ────────────────────────────────────────
# OJO: todo lo de yt-dlp es bloqueante; siempre se llama con asyncio.to_thread()
# para no congelar el bot (heartbeat, audio, otros comandos) mientras busca.

def formatear_tiempo(segundos):
    if segundos is None:
        return "??:??"
    segundos = int(segundos)
    if segundos >= 3600:
        return f"{segundos // 3600}:{segundos % 3600 // 60:02d}:{segundos % 60:02d}"
    return f"{segundos // 60}:{segundos % 60:02d}"

def formatear_duracion(segundos):
    return formatear_tiempo(segundos) if segundos else "??:??"

def buscar_youtube(query, n=5):
    """Busca en YouTube y devuelve una lista de dicts {titulo, duracion, url} sin descargar nada."""
    opts = dict(YDL_OPTS_SEARCH)
    opts["default_search"] = f"ytsearch{n}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as e:
        print(f"[ERROR BUSQUEDA YT] {e}")
        return []

    entradas = info.get("entries") or []
    resultados = []
    for e in entradas:
        if not e:
            continue
        video_id = e.get("id")
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else e.get("url")
        resultados.append({
            "titulo": e.get("title") or "Desconocido",
            "duracion": formatear_duracion(e.get("duration")),
            "url": url,
        })
    return resultados

def extraer_playlist(url):
    """Extrae todas las entradas de una playlist de YouTube sin descargar nada."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS_PLAYLIST) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"[ERROR PLAYLIST YT] {e}")
        return None, []

    nombre_playlist = info.get("title", "Playlist")
    entradas = info.get("entries") or []
    items = []
    for e in entradas:
        if not e:
            continue
        video_id = e.get("id")
        video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else e.get("url")
        items.append({
            "tipo": "youtube",
            "nombre": e.get("title") or "Desconocido",
            "url": video_url,
        })
    return nombre_playlist, items

def extraer_audio_youtube(url, cliente=None):
    """Resuelve la URL de stream de audio directo para un link/consulta de YouTube.
    'cliente' permite forzar un player_client distinto de yt-dlp (android/ios/web_safari)."""
    opts = dict(YDL_OPTS_STREAM)
    if cliente:
        opts["extractor_args"] = {"youtube": {"player_client": [cliente]}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return {
            "stream_url": info["url"],
            "titulo": info.get("title", "Desconocido"),
            "duracion": formatear_duracion(info.get("duration")),
            "duracion_seg": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
        }

def probar_stream_url(url, timeout=5):
    """Chequeo liviano (HEAD) para detectar si YouTube está devolviendo 403 en esta URL."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        return e.code != 403
    except Exception:
        # Si la validación en sí falla (timeout, DNS, etc.) dejamos que ffmpeg lo intente igual.
        return True

def extraer_audio_youtube_con_reintento(url):
    """Extrae el audio probando distintos player_client de yt-dlp si YouTube devuelve 403."""
    ultimo_error = None
    for cliente in CLIENTES_YT_FALLBACK:
        try:
            datos = extraer_audio_youtube(url, cliente=cliente)
        except Exception as e:
            ultimo_error = e
            continue

        if probar_stream_url(datos["stream_url"]):
            return datos

        ultimo_error = RuntimeError(f"403 Forbidden con player_client={cliente or 'default'}")

    raise ultimo_error or RuntimeError("No se pudo obtener audio de YouTube.")


# Sugerencias de búsqueda de YouTube (las mismas que muestra su buscador). Es una
# llamada HTTP liviana (~100 ms), apta para el autocompletado de /play, que tiene
# que responder en menos de 3 segundos (una búsqueda real con yt-dlp no llega).
_http_session = None
_cache_sugerencias = {}

async def sugerencias_youtube(texto):
    global _http_session
    clave = texto.lower()
    if clave in _cache_sugerencias:
        return _cache_sugerencias[clave]
    try:
        if _http_session is None or _http_session.closed:
            _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5))
        async with _http_session.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "firefox", "ds": "yt", "hl": "es", "q": texto},
            headers={"User-Agent": "Mozilla/5.0"},
        ) as resp:
            crudo = await resp.read()
        try:
            datos = json.loads(crudo.decode("utf-8"))
        except UnicodeDecodeError:
            datos = json.loads(crudo.decode("latin-1"))
        sugerencias = [s for s in datos[1] if isinstance(s, str)]
    except Exception:
        return []
    if len(_cache_sugerencias) > 500:
        _cache_sugerencias.clear()
    _cache_sugerencias[clave] = sugerencias
    return sugerencias


# ── Persistencia simple (JSON) ───────────────────────────────────────────────

def _leer_json(archivo):
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            datos = json.load(f)
        return datos if isinstance(datos, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _escribir_json(archivo, datos):
    try:
        with open(archivo, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=4)
    except OSError as e:
        print(f"[ERROR JSON] No pude escribir {archivo}: {e}")

def guardar_now_playing_ref(guild_id, channel_id, message_id):
    datos = _leer_json(NOW_PLAYING_FILE)
    datos[str(guild_id)] = {"channel_id": channel_id, "message_id": message_id}
    _escribir_json(NOW_PLAYING_FILE, datos)

def borrar_now_playing_ref(guild_id):
    datos = _leer_json(NOW_PLAYING_FILE)
    if datos.pop(str(guild_id), None) is not None:
        _escribir_json(NOW_PLAYING_FILE, datos)

async def limpiar_now_playing_anteriores():
    """Al arrancar, borra los mensajes "Tocando agora" que quedaron de una ejecución anterior."""
    datos = _leer_json(NOW_PLAYING_FILE)
    if not datos:
        return
    for ref in datos.values():
        try:
            canal = bot.get_channel(ref["channel_id"])
            if canal is None:
                continue
            await canal.get_partial_message(ref["message_id"]).delete()
        except (discord.HTTPException, KeyError, TypeError):
            pass
    _escribir_json(NOW_PLAYING_FILE, {})

def guardar_canal_musica(guild_id, channel_id):
    datos = _leer_json(CANAL_MUSICA_CONFIG_FILE)
    datos[str(guild_id)] = channel_id
    _escribir_json(CANAL_MUSICA_CONFIG_FILE, datos)

def obtener_canal_musica(guild):
    channel_id = _leer_json(CANAL_MUSICA_CONFIG_FILE).get(str(guild.id))
    return guild.get_channel(channel_id) if channel_id else None


# ── Tiempo de reproducción ───────────────────────────────────────────────────

def posicion_actual(state):
    """Segundos transcurridos de la canción actual (descontando el tiempo en pausa)."""
    if state["start_time"] is None:
        return 0
    referencia = state["pausado_en"] or time.time()
    return max(0, referencia - state["start_time"])

def pausar(voice, state):
    if voice and voice.is_playing():
        voice.pause()
        state["pausado_en"] = time.time()
        return True
    return False

def reanudar(voice, state):
    if voice and voice.is_paused():
        voice.resume()
        if state["pausado_en"] and state["start_time"]:
            state["start_time"] += time.time() - state["pausado_en"]
        state["pausado_en"] = None
        return True
    return False

def get_duracion_segundos(ruta):
    try:
        return int(MP3(ruta).info.length)
    except Exception:
        return None


# ── Interfaz "Now Playing" ───────────────────────────────────────────────────

def construir_barra_progreso(elapsed, total, longitud=18):
    if not total or total <= 0:
        return "▬" * longitud
    ratio = min(max(elapsed / total, 0), 1)
    pos = int(ratio * (longitud - 1))
    return "▬" * pos + "🔘" + "▬" * (longitud - pos - 1)

def construir_embed_now_playing(item, state):
    if item is None:
        return discord.Embed(
            title="🎧 Nada tocando",
            description="A fila está vazia.",
            color=discord.Color.greyple()
        )
    titulo = "⏸️ Pausado" if state["pausado_en"] else "🎧 Tocando agora"
    descripcion = f"**{item['nombre']}**"
    if item.get("url"):
        descripcion = f"**[{item['nombre']}]({item['url']})**"
    embed = discord.Embed(
        title=titulo,
        description=descripcion,
        color=discord.Color.from_rgb(88, 101, 242)
    )

    embed.add_field(name="Adicionado por", value=item.get("agregado_por", "Desconhecido"), inline=True)
    embed.add_field(name="Fila", value=str(len(state["queue"])), inline=True)
    embed.add_field(name="Volume", value=f"{int(state['volume'] * 100)}%", inline=True)
    embed.add_field(name="Loop", value="Ativado 🔁" if state["loop"] else "Desativado", inline=True)
    if state["queue"]:
        embed.add_field(name="A seguir", value=recortar(state["queue"][0]["nombre"], 60), inline=True)

    elapsed = posicion_actual(state)
    total = item.get("duracion_seg")
    barra = construir_barra_progreso(elapsed, total)
    tiempos = f"{formatear_tiempo(elapsed)} / {formatear_tiempo(total)}"
    embed.add_field(name="​", value=f"{barra}\n{tiempos}", inline=False)

    if item.get("thumbnail"):
        embed.set_thumbnail(url=item["thumbnail"])

    return embed


class MusicControlView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        state = get_state(guild_id)
        guild = bot.get_guild(guild_id)
        voice = guild.voice_client if guild else None

        for child in self.children:
            if child.custom_id == "pause_button" and voice and voice.is_paused():
                child.label = "Resume"
                child.emoji = "▶️"
            elif child.custom_id == "loop_button" and state["loop"]:
                child.label = "Loop: On"
                child.style = discord.ButtonStyle.success

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary, custom_id="pause_button")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = interaction.guild.voice_client
        state = get_state(self.guild_id)
        if not voice:
            await interaction.response.send_message("❌ Não estou em um canal de voz.", ephemeral=True)
            return

        if pausar(voice, state):
            button.label = "Resume"
            button.emoji = "▶️"
        elif reanudar(voice, state):
            button.label = "Pause"
            button.emoji = "⏸️"
        else:
            await interaction.response.send_message("❌ Não há nada se reproduzindo.", ephemeral=True)
            return

        embed = construir_embed_now_playing(state["current"], state)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip_button")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if saltar(interaction.guild):
            await interaction.response.send_message(
                f"⏭️ Música pulada por {interaction.user.display_name}.", delete_after=10)
        else:
            await interaction.response.send_message("❌ Não há nada se reproduzindo.", ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="stop_button")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        detener(interaction.guild)

        # Este mismo mensaje queda como aviso final, así que lo soltamos del estado.
        tarea = state["update_task"]
        if tarea:
            tarea.cancel()
        state["update_task"] = None
        state["mensaje_now_playing"] = None
        borrar_now_playing_ref(self.guild_id)

        await interaction.response.edit_message(
            content=f"⏹️ Reprodução interrompida e fila esvaziada por {interaction.user.display_name}.",
            embed=None, view=None, delete_after=30)

    @discord.ui.button(label="Loop: Off", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="loop_button")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        state["loop"] = not state["loop"]
        button.label = "Loop: On" if state["loop"] else "Loop: Off"
        button.style = discord.ButtonStyle.success if state["loop"] else discord.ButtonStyle.secondary

        embed = construir_embed_now_playing(state["current"], state)
        await interaction.response.edit_message(embed=embed, view=self)


async def actualizar_progreso(guild_id, mensaje):
    try:
        while True:
            await asyncio.sleep(10)
            state = get_state(guild_id)
            guild = bot.get_guild(guild_id)
            voice = guild.voice_client if guild else None
            if state["current"] is None or state["start_time"] is None or not voice:
                return
            if state["mensaje_now_playing"] is not mensaje:
                return
            if voice.is_paused():
                continue  # nada cambia mientras está en pausa
            try:
                await mensaje.edit(embed=construir_embed_now_playing(state["current"], state))
            except discord.NotFound:
                return
            except discord.HTTPException:
                pass
    except asyncio.CancelledError:
        return


async def borrar_now_playing(guild_id):
    state = get_state(guild_id)
    tarea = state["update_task"]
    if tarea:
        tarea.cancel()
    state["update_task"] = None

    mensaje = state["mensaje_now_playing"]
    state["mensaje_now_playing"] = None
    if mensaje:
        try:
            await mensaje.delete()
        except discord.HTTPException:
            pass
    borrar_now_playing_ref(guild_id)


def canal_de_avisos(guild_id):
    """Canal de texto donde el bot avisa cosas de la música: el configurado con
    /canal_musica, si no el último desde donde se pidió algo, si no el general."""
    guild = bot.get_guild(guild_id)
    canal = obtener_canal_musica(guild) if guild else None
    return canal or get_state(guild_id)["canal_texto"] or bot.get_channel(CANAL_TEXTO_ID)

async def avisar(guild_id, texto):
    canal = canal_de_avisos(guild_id)
    if canal is None:
        return
    try:
        await canal.send(texto)
    except discord.HTTPException as e:
        print(f"[ERROR AVISO] {e}")

async def enviar_now_playing(guild_id, item):
    state = get_state(guild_id)
    await borrar_now_playing(guild_id)

    canal = canal_de_avisos(guild_id)
    if canal is None:
        return
    try:
        mensaje = await canal.send(
            embed=construir_embed_now_playing(item, state),
            view=MusicControlView(guild_id),
        )
    except discord.HTTPException as e:
        print(f"[ERROR NOW PLAYING] {e}")
        return

    state["mensaje_now_playing"] = mensaje
    state["update_task"] = bot.loop.create_task(actualizar_progreso(guild_id, mensaje))
    guardar_now_playing_ref(guild_id, mensaje.channel.id, mensaje.id)


# ── Easter egg ───────────────────────────────────────────────────────────────

CANCION_ESPECIFICA = "Dios es tecno"
EVENTOS_CANCION = [
    (31, ["ESTA EL DIEGO PRESENTE"]),
    (40, ["ESTA EL DIEGO PRESENTE"]),
    (54, ["QUE DIEGO"]),
    (59, ["QUE COSA DIEGO"]),
    (66, ["NOOOOO", "NOOOOO DIEGO", "NOOOOO", "NOOOOO"]),
]

async def monitor_minuto(guild_id, inicio_id):
    canal = bot.get_channel(CANAL_TEXTO_ID)
    if canal is None:
        return
    pendientes = list(EVENTOS_CANCION)
    while pendientes:
        state = get_state(guild_id)
        actual = state["current"]
        if state["inicio_id"] != inicio_id or not actual or actual["nombre"] != CANCION_ESPECIFICA:
            return
        if posicion_actual(state) >= pendientes[0][0]:
            for texto in pendientes.pop(0)[1]:
                try:
                    await canal.send(texto)
                except discord.HTTPException:
                    return
            continue
        await asyncio.sleep(1)


# ── Motor de reproducción ────────────────────────────────────────────────────

def resolver_fuente_audio(item, volumen, seek=0):
    """Construye el PCMVolumeTransformer de reproducción, tanto para canciones locales
    como de YouTube (con reintento automático ante 403). BLOQUEANTE: llamar siempre
    con asyncio.to_thread()."""
    seek_opts = f"-ss {int(seek)}" if seek > 0 else ""

    if item["tipo"] == "local":
        ruta = f"data/{item['nombre']}.mp3"
        if not os.path.exists(ruta):
            raise FileNotFoundError(f"No se encontró {ruta}")
        item["duracion_seg"] = get_duracion_segundos(ruta)
        return discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                ruta, executable=FFMPEG_PATH,
                before_options=seek_opts or None,
                options="-vn",
                stderr=FFMPEG_LOG,
            ),
            volume=volumen
        )

    # youtube (las URLs de stream expiran, por eso siempre se re-resuelven)
    datos = extraer_audio_youtube_con_reintento(item["url"])
    item["nombre"] = datos["titulo"]
    item["duracion_seg"] = datos.get("duracion_seg")
    item["thumbnail"] = datos.get("thumbnail")

    before_options = f"{seek_opts} {FFMPEG_BEFORE_OPTIONS_YT}".strip()
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(
            datos["stream_url"], executable=FFMPEG_PATH,
            before_options=before_options,
            options="-vn",
            stderr=FFMPEG_LOG,
        ),
        volume=volumen
    )


MAX_HISTORIAL_CANCIONES = 20

def agregar_al_historial(state, item):
    """Guarda una versión mínima del item para poder re-encolarlo después con
    'ultima' sin arrastrar datos que quedan viejos (duracion_seg, thumbnail)."""
    entrada = {"tipo": item["tipo"], "nombre": item["nombre"], "url": item.get("url")}
    state["historial"].append(entrada)
    del state["historial"][:-MAX_HISTORIAL_CANCIONES]

def obtener_ultima_cancion(state):
    """Devuelve la canción reproducida justo antes de la actual. Si no hay nada
    sonando ahora, devuelve la última que sonó. None si no hay historial suficiente."""
    historial = state["historial"]
    if state["current"] is not None:
        return historial[-2] if len(historial) >= 2 else None
    return historial[-1] if historial else None


def iniciar_reproduccion(voice, state, guild_id, fuente, al_terminar=None, limpieza=None):
    """Único punto donde se llama a voice.play(). Cada llamada recibe un play_id; cuando
    el audio termina, solo se actúa (avanzar la cola, o 'al_terminar') si ese play_id
    sigue siendo el vigente. Así un seek, un TTS o un stop reemplazan/cortan el audio
    sin que el callback del audio viejo avance la cola por error."""
    state["play_id"] += 1
    pid = state["play_id"]

    def after(error):
        if error:
            print(f"[ERROR REPRODUCCION] {error}")
        bot.loop.call_soon_threadsafe(_fin_reproduccion, guild_id, pid, al_terminar, limpieza)

    voice.play(fuente, after=after)

def _fin_reproduccion(guild_id, pid, al_terminar, limpieza):
    if limpieza:
        limpieza()
    state = get_state(guild_id)
    if pid != state["play_id"]:
        return  # fue reemplazado (seek/TTS) o detenido (stop/leave)
    bot.loop.create_task(al_terminar() if al_terminar else avanzar_cola(guild_id))


async def avanzar_cola(guild_id):
    """Arranca la siguiente canción (o repite la actual si hay loop). No hace nada
    si ya hay algo sonando."""
    state = get_state(guild_id)
    async with state["lock"]:
        guild = bot.get_guild(guild_id)
        voice = guild.voice_client if guild else None
        if voice is None or not voice.is_connected():
            state["current"] = None
            return
        if voice.is_playing() or voice.is_paused():
            return

        while True:
            forzar = state["forzar_siguiente"]
            state["forzar_siguiente"] = False

            es_repeticion = bool(state["loop"] and state["current"] and not forzar)
            if es_repeticion:
                item = state["current"]
            else:
                if not state["queue"]:
                    state["current"] = None
                    state["start_time"] = None
                    state["pausado_en"] = None
                    await borrar_now_playing(guild_id)
                    return
                item = state["queue"].pop(0)
                state["current"] = item

            try:
                fuente = await asyncio.to_thread(resolver_fuente_audio, item, state["volume"])
            except Exception as e:
                print(f"[ERROR AUDIO] {e}")
                if state["current"] is not item:
                    return  # lo pararon mientras resolvía
                await avisar(guild_id, f"⚠️ Não consegui reproduzir `{item.get('nombre', '?')}`, pulando...")
                state["forzar_siguiente"] = True  # no reintentar la misma canción rota (loop)
                continue

            # Mientras se resolvía el audio pudieron parar o desconectar al bot.
            if state["current"] is not item or not voice.is_connected():
                fuente.cleanup()
                return
            break

        if not es_repeticion:
            agregar_al_historial(state, item)

        iniciar_reproduccion(voice, state, guild_id, fuente)
        state["start_time"] = time.time()
        state["pausado_en"] = None
        state["inactivo_desde"] = None
        state["inicio_id"] += 1

        if es_repeticion and state["mensaje_now_playing"]:
            try:
                await state["mensaje_now_playing"].edit(embed=construir_embed_now_playing(item, state))
            except discord.HTTPException:
                await enviar_now_playing(guild_id, item)
        else:
            await enviar_now_playing(guild_id, item)

        if item["nombre"] == CANCION_ESPECIFICA:
            bot.loop.create_task(monitor_minuto(guild_id, state["inicio_id"]))


def nuevo_item(tipo, nombre, autor, url=None):
    return {"tipo": tipo, "nombre": nombre, "url": url, "agregado_por": autor}

def item_desde_opcion(opcion, autor):
    return nuevo_item(opcion["tipo"], opcion["nombre"], autor, opcion.get("url"))

def agregar_a_cola(guild, items):
    """Encola y, si no hay nada sonando, arranca. Devuelve la posición en la cola
    del primer item agregado (0 = empieza a sonar ya)."""
    state = get_state(guild.id)
    voice = guild.voice_client
    ocupado = bool(voice and (voice.is_playing() or voice.is_paused())) \
        or state["lock"].locked() or state["en_tts"]
    habia_pendientes = bool(state["queue"])  # otro pedido está por arrancar
    posicion = len(state["queue"]) + 1
    state["queue"].extend(items)
    if not ocupado:
        # Si ya había otro arranque en camino esto no hace nada (avanzar_cola se serializa).
        bot.loop.create_task(avanzar_cola(guild.id))
        if not habia_pendientes:
            return 0
        posicion -= 1  # el primero pendiente está por salir de la cola para sonar
    return posicion

def reemplazar_cancion(guild, vieja, nueva):
    """Cambia una canción recién pedida por otra (menú "no era esa")."""
    state = get_state(guild.id)
    voice = guild.voice_client
    if state["current"] is vieja:
        state["queue"].insert(0, nueva)
        if not saltar(guild):
            state["forzar_siguiente"] = True
        return f"🔁 Trocado por `{nueva['nombre']}`."
    for i, it in enumerate(state["queue"]):
        if it is vieja:
            state["queue"][i] = nueva
            return f"🔁 Trocado na fila por `{nueva['nombre']}`."
    posicion = agregar_a_cola(guild, [nueva])
    return texto_encolado(nueva, posicion)

def texto_encolado(item, posicion):
    if posicion == 0:
        return f"▶️ Tocando: `{item['nombre']}`"
    return f"➕ Adicionado à fila: `{item['nombre']}` (posição {posicion})"

def saltar(guild):
    voice = guild.voice_client
    if voice and (voice.is_playing() or voice.is_paused()):
        state = get_state(guild.id)
        if state["en_tts"]:
            return False
        state["forzar_siguiente"] = True  # avanza aunque loop esté activo
        voice.stop()
        return True
    return False

def detener(guild):
    """Corta todo: vacía la cola e invalida el callback del audio que estaba sonando."""
    state = get_state(guild.id)
    state["play_id"] += 1
    state["queue"].clear()
    state["current"] = None
    state["start_time"] = None
    state["pausado_en"] = None
    state["forzar_siguiente"] = False
    state["sugerencias"] = []
    voice = guild.voice_client
    if voice:
        voice.stop()


# ── Voz: conexión y TTS ──────────────────────────────────────────────────────

async def conectar_voz(guild, canal):
    voice = guild.voice_client
    if voice is None:
        await canal.connect(self_deaf=True)
        state = get_state(guild.id)
        state["inactivo_desde"] = None
        state["vacio_desde"] = None
    elif voice.channel != canal:
        await voice.move_to(canal)

async def ensure_voice(ctx):
    if ctx.guild is None:
        await ctx.send("❌ Isso só funciona dentro do servidor.")
        return False
    estado_voz = getattr(ctx.author, "voice", None)
    if estado_voz is None or estado_voz.channel is None:
        await ctx.send("❌ No estás en un canal de voz.", ephemeral=True)
        return False
    try:
        await conectar_voz(ctx.guild, estado_voz.channel)
    except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as e:
        print(f"[ERROR VOZ] {e}")
        await ctx.send("❌ Não consegui entrar no canal de voz.", ephemeral=True)
        return False
    get_state(ctx.guild.id)["canal_texto"] = ctx.channel
    return True


async def generar_tts(texto, archivo):
    communicate = edge_tts.Communicate(texto, voice="pt-BR-FranciscaNeural")
    await communicate.save(archivo)
    return archivo

def _borrar_archivo(ruta):
    try:
        os.remove(ruta)
    except OSError:
        pass

async def decir_por_voz(guild_id, texto):
    """Dice un texto por el canal de voz. Si había música, la interrumpe y después la
    retoma desde donde estaba. Devuelve False si no pudo (no conectado / ya hablando)."""
    guild = bot.get_guild(guild_id)
    voice = guild.voice_client if guild else None
    if voice is None or not voice.is_connected():
        return False
    state = get_state(guild_id)
    if state["en_tts"]:
        return False
    state["en_tts"] = True

    archivo = f"tts_{int(time.time() * 1000)}.mp3"
    try:
        await generar_tts(texto, archivo)
    except Exception as e:
        print(f"[ERROR TTS] {e}")
        state["en_tts"] = False
        _borrar_archivo(archivo)
        return False

    async with state["lock"]:
        item = state["current"]
        habia_musica = item is not None and (voice.is_playing() or voice.is_paused())
        posicion = posicion_actual(state)

        def limpieza():
            state["en_tts"] = False
            _borrar_archivo(archivo)

        async def al_terminar():
            retomar = habia_musica and state["current"] is item and not state["forzar_siguiente"]
            if retomar:
                async with state["lock"]:
                    try:
                        nueva = await asyncio.to_thread(
                            resolver_fuente_audio, item, state["volume"], max(0, int(posicion) - 1))
                    except Exception as e:
                        print(f"[ERROR TTS] No se pudo reanudar la canción: {e}")
                        nueva = None
                    if nueva is not None:
                        if state["current"] is item and voice.is_connected() and not voice.is_playing():
                            iniciar_reproduccion(voice, state, guild_id, nueva)
                            state["start_time"] = time.time() - posicion
                            state["pausado_en"] = None
                            return
                        nueva.cleanup()
            await avanzar_cola(guild_id)

        try:
            fuente = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(archivo, executable=FFMPEG_PATH, stderr=FFMPEG_LOG),
                volume=state["volume"]
            )
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            iniciar_reproduccion(voice, state, guild_id, fuente, al_terminar=al_terminar, limpieza=limpieza)
        except Exception as e:
            print(f"[ERROR TTS] {e}")
            limpieza()
            return False
    return True


# ── Pedidos de canciones (lo usan /play, las menciones y la IA) ──────────────

def es_url(texto):
    return re.match(r"https?://", texto) is not None

def es_url_youtube(texto):
    return re.match(r"https?://((www|m|music)\.)?(youtube\.com|youtu\.be)/", texto) is not None

def es_url_playlist(texto):
    return es_url_youtube(texto) and "list=" in texto


class ElegirCancionView(discord.ui.View):
    """Menú desplegable para elegir una canción entre varias opciones. Si 'reemplaza'
    viene con un item, la elegida lo sustituye (caso "no era esa")."""

    def __init__(self, guild_id, opciones, reemplaza=None):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.opciones = opciones[:25]
        self.reemplaza = reemplaza
        self.message = None

        self.select = discord.ui.Select(
            placeholder="Não era essa? Escolha outra..." if reemplaza else "Escolha uma música...",
            options=[
                discord.SelectOption(
                    label=recortar(o["nombre"], 100),
                    value=str(i),
                    description="Local" if o["tipo"] == "local" else f"YouTube · {o.get('duracion', '??:??')}",
                    emoji="💾" if o["tipo"] == "local" else "📺",
                )
                for i, o in enumerate(self.opciones)
            ],
        )
        self.select.callback = self.elegir
        self.add_item(self.select)

    async def elegir(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild.voice_client is None:
            estado_voz = interaction.user.voice
            if estado_voz is None or estado_voz.channel is None:
                await interaction.response.send_message("❌ No estás en un canal de voz.", ephemeral=True)
                return
            await conectar_voz(guild, estado_voz.channel)

        opcion = self.opciones[int(self.select.values[0])]
        item = item_desde_opcion(opcion, interaction.user.display_name)
        get_state(guild.id)["sugerencias"] = []

        if self.reemplaza is not None:
            texto = reemplazar_cancion(guild, self.reemplaza, item)
        else:
            texto = texto_encolado(item, agregar_a_cola(guild, [item]))

        self.stop()
        await interaction.response.edit_message(content=texto, view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def encolar_y_avisar(ctx, item, alternativas=None):
    posicion = agregar_a_cola(ctx.guild, [item])
    texto = texto_encolado(item, posicion)
    view = None
    if alternativas:
        view = ElegirCancionView(ctx.guild.id, alternativas, reemplaza=item)
    mensaje = await ctx.send(texto, view=view)
    if view:
        view.message = mensaje

async def procesar_play(ctx, query, autor=None, solo_youtube=False):
    """Resuelve un pedido de canción: número de una lista previa, link, playlist,
    canción local o búsqueda en YouTube. Asume que el bot ya está en el canal de voz."""
    state = get_state(ctx.guild.id)
    autor = autor or ctx.author.display_name
    query = query.strip()

    # Selección numérica de la última lista de opciones (m!play 2)
    if query.isdigit() and state["sugerencias"]:
        opciones = state["sugerencias"]
        idx = int(query) - 1
        if not (0 <= idx < len(opciones)):
            await ctx.send(f"❌ Número inválido. Escolha entre 1 e {len(opciones)}.", ephemeral=True)
            return
        state["sugerencias"] = []
        await encolar_y_avisar(ctx, item_desde_opcion(opciones[idx], autor))
        return
    state["sugerencias"] = []

    # Prefijo "yt:" -> saltear las canciones locales (lo usa el autocompletado)
    if query.lower().startswith("yt:"):
        solo_youtube = True
        query = query[3:].strip()
    if not query:
        await ctx.send("❌ Faltou dizer o que tocar.", ephemeral=True)
        return

    if es_url(query):
        if not es_url_youtube(query):
            await ctx.send("❌ Só aceito links do YouTube.", ephemeral=True)
            return

        if es_url_playlist(query):
            nombre_playlist, items = await asyncio.to_thread(extraer_playlist, query)
            if not items:
                await ctx.send("❌ Não consegui extrair a playlist.")
                return
            recortada = len(items) > MAX_PLAYLIST
            items = items[:MAX_PLAYLIST]
            for it in items:
                it["agregado_por"] = autor
            agregar_a_cola(ctx.guild, items)
            extra = f" (limitado às primeiras {MAX_PLAYLIST})" if recortada else ""
            await ctx.send(embed=discord.Embed(
                description=f"**\"{nombre_playlist}\"** com **{len(items)}** músicas adicionadas à fila{extra}.",
                color=discord.Color.green()
            ))
            return

        posicion = agregar_a_cola(ctx.guild, [nuevo_item("youtube", query, autor, url=query)])
        await ctx.send("▶️ Tocando o link." if posicion == 0
                       else f"➕ Link adicionado à fila (posição {posicion}).")
        return

    parciales, aproximadas = [], []
    if not solo_youtube:
        exacta, parciales, aproximadas = buscar_cancion(query)
        if exacta:
            await encolar_y_avisar(ctx, nuevo_item("local", exacta, autor))
            return

    resultados = await asyncio.to_thread(buscar_youtube, query, 5)
    opciones_yt = [
        {"tipo": "youtube", "nombre": r["titulo"], "url": r["url"], "duracion": r["duracion"]}
        for r in resultados if r.get("url")
    ]
    opciones_locales = [{"tipo": "local", "nombre": c} for c in (parciales or aproximadas)[:8]]

    if not opciones_yt and not opciones_locales:
        await ctx.send(f"❌ Não encontrei nada para `{query}`.")
        return

    # Varias canciones locales coinciden (o YouTube no devolvió nada): que elija.
    if parciales or not opciones_yt:
        opciones = opciones_locales + opciones_yt
        state["sugerencias"] = opciones
        view = ElegirCancionView(ctx.guild.id, opciones)
        lista = "\n".join(
            f"`{i}.` {'💾' if o['tipo'] == 'local' else '📺'} {recortar(o['nombre'], 70)}"
            for i, o in enumerate(opciones, 1)
        )
        view.message = await ctx.send(
            f"🔍 Achei várias opções para `{recortar(query, 80)}`. Escolha no menu "
            f"(ou `{PREFIJO}play <número>`):\n{lista}",
            view=view,
        )
        return

    # Caso normal: suena el primer resultado y queda un menú por si no era ese.
    primero = opciones_yt[0]
    await encolar_y_avisar(ctx, item_desde_opcion(primero, autor),
                           alternativas=opciones_locales + opciones_yt[1:])

async def reproducir_local_al_azar(ctx, autor=None):
    canciones = get_canciones()
    if not canciones:
        await ctx.send("❌ Não tenho músicas locais disponíveis agora.")
        return
    await encolar_y_avisar(ctx, nuevo_item("local", random.choice(canciones), autor or ctx.author.display_name))

async def reproducir_ultima(ctx, autor=None):
    entrada = obtener_ultima_cancion(get_state(ctx.guild.id))
    if not entrada:
        await ctx.send("❌ Ainda não toquei nada nesta sessão.")
        return
    await encolar_y_avisar(ctx, item_desde_opcion(entrada, autor or ctx.author.display_name))

async def ia_reproducir_musica(ctx, tipo, query=None):
    """Reproduce música a pedido de la IA: entra al canal de voz del usuario si
    hace falta, y elige la canción (local al azar, la última, o búsqueda en YouTube)."""
    if ctx.guild is None:
        return  # no hay voz en DMs
    if tipo == "youtube" and not (query or "").strip():
        return
    if not await ensure_voice(ctx):
        return

    autor = f"{ctx.author.display_name} (via IA)"
    if tipo == "local":
        await reproducir_local_al_azar(ctx, autor)
    elif tipo == "last":
        await reproducir_ultima(ctx, autor)
    else:
        async with ctx.typing():
            await procesar_play(ctx, query, autor=autor, solo_youtube=True)


# ── Pedidos en lenguaje natural (sin IA) ─────────────────────────────────────
# Atajo rápido: si el mensaje ES una orden clara ("pone X", "siguiente", "pará"),
# se ejecuta al toque sin gastar una llamada a la IA ni esperar su respuesta.

def normalizar(texto):
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w\s]", " ", texto).strip()

RE_PEDIDO_PLAY = re.compile(
    r"^(?:pon[eé](?:me|le|nos)?|pon|poner|play|reproduc[ií](?:r|me)?|reproduce|toc[aá](?:me)?|bot[aá])\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
PALABRAS_RELLENO = {"la", "el", "lo", "un", "una", "musica", "tema", "cancion", "por", "favor",
                    "porfa", "che", "ya", "ahora", "esa", "ese", "esto", "eso", "de", "a"}
ACCIONES_DIRECTAS = {
    "skip":   {"skip", "next", "salta", "saltar", "saltea", "saltala", "siguiente", "proxima", "pasala"},
    "pause":  {"pausa", "pausar", "pausala", "pause"},
    "resume": {"segui", "seguir", "continua", "continuar", "resume", "reanuda", "reanudar"},
    "stop":   {"stop", "para", "parar", "parala", "detene", "detener", "frena", "frenala"},
    "loop":   {"loop", "repetir", "repeti", "bucle", "en bucle"},
    "last":   {"anterior", "ultima", "antes", "ultima que pusiste", "que pusiste antes"},
}
PEDIDOS_GENERICOS = {"", "algo", "alguna", "cualquiera", "cualquier cosa", "que sea", "lo que quieras",
                     "algo copado", "algo bueno"}

def interpretar_pedido(texto):
    """Devuelve (accion, query) si el texto es una orden de música inequívoca, o None."""
    texto = texto.strip()
    if not texto or len(texto) > 150:
        return None

    m = RE_PEDIDO_PLAY.match(texto)
    if m:
        query = m.group(1).strip()
        esencial = " ".join(p for p in normalizar(query).split() if p not in PALABRAS_RELLENO)
        if esencial in ACCIONES_DIRECTAS["last"]:
            return "last", None
        if esencial in PEDIDOS_GENERICOS:
            return "local", None
        return "play", query

    esencial = " ".join(p for p in normalizar(texto).split() if p not in PALABRAS_RELLENO)
    for accion, palabras in ACCIONES_DIRECTAS.items():
        if esencial in palabras:
            return accion, None
    return None

async def ejecutar_accion_musica(ctx, accion, query=None):
    if accion == "play":
        await ctx.invoke(play, query=query)
    elif accion == "local":
        if await ensure_voice(ctx):
            await reproducir_local_al_azar(ctx)
    else:
        comando = {"skip": skip, "pause": pause, "resume": resume,
                   "stop": stop, "loop": loop, "last": ultima}[accion]
        await ctx.invoke(comando)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.invoke(ctx)  # comando m!
        return

    es_mencion = bot.user in message.mentions
    es_reply_al_bot = (
        message.reference is not None
        and message.reference.resolved is not None
        and getattr(message.reference.resolved, "author", None) == bot.user
    )
    dirigido_al_bot = es_mencion or es_reply_al_bot
    en_canal_musica = message.channel.id == CANAL_PALABRAS_CLAVE

    texto = message.content
    for m in message.mentions:
        texto = texto.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
    texto = texto.strip()

    if message.guild and (dirigido_al_bot or en_canal_musica):
        # 1) Orden clara -> se ejecuta directo, sin pasar por la IA.
        pedido = interpretar_pedido(texto)
        if pedido:
            await ejecutar_accion_musica(ctx, *pedido)
            return

        # 2) En el canal de música, la IA clasifica si el mensaje es un pedido.
        if en_canal_musica and texto:
            datos = await clasificar_pedido_musica(texto)
            if datos and datos.get("es_musica"):
                accion = datos.get("accion")
                if accion == "play":
                    tipo, query = datos.get("tipo"), datos.get("query")
                    if tipo == "local":
                        await ia_reproducir_musica(ctx, "local")
                        return
                    if tipo == "youtube" and query:
                        await ia_reproducir_musica(ctx, "youtube", query)
                        return
                elif accion in ("skip", "pause", "resume", "stop", "loop", "last"):
                    await ejecutar_accion_musica(ctx, accion)
                    return

    if not dirigido_al_bot:
        return

    # 3) Charla con la IA (que también puede poner música con sus tags ocultas).
    contexto_usuario = ""
    if message.guild and isinstance(message.author, discord.Member):
        roles = [r.name for r in message.author.roles if r.name != "@everyone"]
        if roles:
            contexto_usuario += f" | roles: {', '.join(roles)}"
        if message.author.joined_at:
            dias = (discord.utils.utcnow() - message.author.joined_at).days
            contexto_usuario += f" | está en el server hace {dias} días"

    imagenes = []
    for adj in message.attachments:
        if adj.content_type and adj.content_type.startswith("image/"):
            try:
                datos_img = await adj.read()
                imagenes.append(Image.open(io.BytesIO(datos_img)))
            except Exception as e:
                print(f"[ERROR IMAGEN] {e}")

    if not texto and not imagenes:
        return
    if not texto:
        texto = "(mandó una imagen sin texto, comentala)"

    async with message.channel.typing():
        respuesta = await preguntar_ia(message.author.id, message.author.display_name,
                                       texto, imagenes, contexto_usuario)

    respuesta_limpia, tipo_musica, query_musica = extraer_comando_musica(respuesta)
    await message.reply((respuesta_limpia or "🎶")[:2000])

    if tipo_musica:
        await ia_reproducir_musica(ctx, tipo_musica, query_musica)


# ── Eventos ──────────────────────────────────────────────────────────────────

_arranque_hecho = False

@bot.event
async def on_ready():
    global _arranque_hecho
    print(f"Conectado como {bot.user}")
    print(f"Servidores conectados ({len(bot.guilds)}):")
    for guild in bot.guilds:
        print(f"  - {guild.name} (ID: {guild.id})")

    # on_ready se dispara de nuevo en cada reconexión: lo de abajo va una sola vez.
    if _arranque_hecho:
        return
    _arranque_hecho = True

    await limpiar_now_playing_anteriores()
    threading.Thread(target=consola, daemon=True).start()
    bot.loop.create_task(vigilar_canales_voz())

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    error = getattr(error, "original", error)
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original

    if isinstance(error, commands.MissingRequiredArgument):
        texto = f"❌ Faltou o argumento `{error.param.name}`. Veja `/help`."
    elif isinstance(error, (commands.BadArgument, commands.UserInputError)):
        texto = "❌ Argumento inválido. Veja `/help`."
    elif isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        texto = "❌ Você não tem permissão para isso (ou não dá pra usar aqui)."
    else:
        nombre = ctx.command.qualified_name if ctx.command else "?"
        print(f"[ERROR COMANDO] {nombre}:")
        traceback.print_exception(type(error), error, error.__traceback__)
        texto = "❌ Deu ruim executando esse comando."

    try:
        await ctx.send(texto, ephemeral=True)
    except discord.HTTPException:
        pass

@bot.event
async def on_voice_state_update(member, before, after):
    # Si echan al bot del canal de voz (o se cae la conexión), limpiar el estado.
    if bot.user and member.id == bot.user.id and before.channel is not None and after.channel is None:
        state = get_state(member.guild.id)
        state["play_id"] += 1
        state["queue"].clear()
        state["current"] = None
        state["start_time"] = None
        state["pausado_en"] = None
        state["inactivo_desde"] = None
        state["vacio_desde"] = None
        await borrar_now_playing(member.guild.id)

@bot.event
async def on_guild_remove(guild):
    guilds_state.pop(guild.id, None)


# ── Salida automática del canal de voz ───────────────────────────────────────

async def desconectar_de_canal(guild):
    """Limpia el estado de reproducción y desconecta del canal de voz.
    La usan tanto 'leave' como el vigilante automático de inactividad."""
    detener(guild)
    await borrar_now_playing(guild.id)
    if guild.voice_client:
        await guild.voice_client.disconnect()


CANAL_VACIO_ESPERA_SEG = 60           # gracia antes de salir si el canal se queda sin humanos
INACTIVIDAD_MAXIMA_SEG = 60 * 60      # 1 hora conectado sin que suene nada
INTERVALO_VIGILANCIA_SEG = 20

async def vigilar_canales_voz():
    """Tarea de fondo: revisa periódicamente los canales de voz donde el bot está
    conectado y sale solo si el canal queda vacío o si pasó demasiado tiempo
    sin que suene nada (música o TTS)."""
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(INTERVALO_VIGILANCIA_SEG)
        for guild in bot.guilds:
            try:
                voice = guild.voice_client
                if voice is None or voice.channel is None:
                    continue

                state = get_state(guild.id)
                ahora = time.time()

                humanos = [m for m in voice.channel.members if not m.bot]
                if not humanos:
                    if state["vacio_desde"] is None:
                        state["vacio_desde"] = ahora
                    elif ahora - state["vacio_desde"] >= CANAL_VACIO_ESPERA_SEG:
                        await desconectar_de_canal(guild)
                        await avisar(guild.id, "👋 Saí do canal porque fiquei sozinho.")
                        continue
                else:
                    state["vacio_desde"] = None

                sonando = voice.is_playing() or voice.is_paused() or state["lock"].locked()
                if sonando or state["queue"]:
                    state["inactivo_desde"] = None
                elif state["inactivo_desde"] is None:
                    state["inactivo_desde"] = ahora
                elif ahora - state["inactivo_desde"] >= INACTIVIDAD_MAXIMA_SEG:
                    await desconectar_de_canal(guild)
                    await avisar(guild.id, "👋 Já faz mais de uma hora que estou aqui sem nada pra tocar, vou sair.")
            except Exception as e:
                print(f"[ERROR VIGILANCIA] {e}")


# ── Comandos de conexión ─────────────────────────────────────────────────────
# Los comandos híbridos funcionan igual como "/comando" y como "m!comando".

@bot.hybrid_command(description="Entrar no seu canal de voz")
@commands.guild_only()
async def join(ctx):
    if not await ensure_voice(ctx):
        return
    await ctx.send("✅ Conectado ao canal de voz.")

@bot.hybrid_command(description="Sair do canal de voz")
@commands.guild_only()
async def leave(ctx):
    if ctx.voice_client:
        await desconectar_de_canal(ctx.guild)
        await ctx.send("👋 Eu saí do canal.")
    else:
        await ctx.send("❌ Não estou em um canal de voz.", ephemeral=True)


# ── Comandos de reproducción ─────────────────────────────────────────────────

@bot.hybrid_command(description="Tocar uma música: nome local, busca ou link do YouTube")
@app_commands.describe(query="Nome da música, busca do YouTube ou link (vídeo ou playlist)")
@commands.guild_only()
async def play(ctx, *, query: str):
    if not await ensure_voice(ctx):
        return
    await ctx.defer()
    await procesar_play(ctx, query)

@play.autocomplete("query")
async def play_autocomplete(interaction: discord.Interaction, current: str):
    actual = current.strip()
    if es_url(actual):
        return []

    canciones = get_canciones()
    if not actual:
        return [app_commands.Choice(name=recortar(f"💾 {c}"), value=recortar(c)) for c in canciones[:25]]

    actual_lower = actual.lower()
    empiezan = [c for c in canciones if c.lower().startswith(actual_lower)]
    contienen = [c for c in canciones if actual_lower in c.lower() and c not in empiezan]
    opciones = [app_commands.Choice(name=recortar(f"💾 {c}"), value=recortar(c))
                for c in (empiezan + contienen)[:10]]

    if len(actual) >= 2:
        vistos = set()
        for s in [actual] + await sugerencias_youtube(actual):
            if s.lower() in vistos or len(s) > 95:
                continue
            vistos.add(s.lower())
            opciones.append(app_commands.Choice(name=recortar(f"🔎 {s}"), value=f"yt:{s}"))
    return opciones[:25]

@bot.hybrid_command(name="ultima", aliases=["last", "anterior"], description="Tocar de novo a música anterior")
@commands.guild_only()
async def ultima(ctx):
    if not await ensure_voice(ctx):
        return
    await reproducir_ultima(ctx)

@bot.hybrid_command(description="Pular a música atual")
@commands.guild_only()
async def skip(ctx):
    if saltar(ctx.guild):
        await ctx.send("⏭️ Pulando música...")
    else:
        await ctx.send("❌ Não há nada se reproduzindo.", ephemeral=True)

@bot.hybrid_command(description="Parar a música e esvaziar a fila")
@commands.guild_only()
async def stop(ctx):
    detener(ctx.guild)
    await borrar_now_playing(ctx.guild.id)
    await ctx.send("⏹️ A reprodução foi interrompida e a fila esvaziada.")

@bot.hybrid_command(description="Pausar a música")
@commands.guild_only()
async def pause(ctx):
    if pausar(ctx.voice_client, get_state(ctx.guild.id)):
        await ctx.send("⏸️ Pausado.")
    else:
        await ctx.send("❌ Não há nada se reproduzindo.", ephemeral=True)

@bot.hybrid_command(description="Retomar a música pausada")
@commands.guild_only()
async def resume(ctx):
    if reanudar(ctx.voice_client, get_state(ctx.guild.id)):
        await ctx.send("▶️ Retomado.")
    else:
        await ctx.send("❌ Nada está em pausa.", ephemeral=True)


def parsear_tiempo(texto):
    """Acepta '90', '1:30' o '1:02:03' y devuelve segundos (None si no es válido)."""
    partes = texto.strip().split(":")
    if not (1 <= len(partes) <= 3) or not all(p.isdigit() for p in partes):
        return None
    segundos = 0
    for p in partes:
        segundos = segundos * 60 + int(p)
    return segundos

@bot.hybrid_command(description="Pular para um ponto da música atual")
@app_commands.describe(tiempo="Segundos (90) ou minuto:segundo (1:30)")
@commands.guild_only()
async def seek(ctx, tiempo: str):
    state = get_state(ctx.guild.id)
    voice = ctx.voice_client
    item = state["current"]

    if not voice or not item or state["en_tts"]:
        await ctx.send("❌ Não há nada tocando agora.", ephemeral=True)
        return
    segundos = parsear_tiempo(tiempo)
    if segundos is None:
        await ctx.send("❌ Tempo inválido. Use segundos (`90`) ou `1:30`.", ephemeral=True)
        return

    await ctx.defer()
    async with state["lock"]:
        try:
            nueva_fuente = await asyncio.to_thread(resolver_fuente_audio, item, state["volume"], segundos)
        except Exception as e:
            print(f"[ERROR SEEK] {e}")
            await ctx.send("⚠️ Não consegui pular para esse ponto.")
            return

        if state["current"] is not item or not voice.is_connected() or state["en_tts"]:
            nueva_fuente.cleanup()
            await ctx.send("❌ A música mudou enquanto eu procurava esse ponto.")
            return

        # El stop() dispara el callback del audio anterior, pero iniciar_reproduccion()
        # ya le asignó un play_id nuevo, así que ese callback se descarta solo.
        if voice.is_playing() or voice.is_paused():
            voice.stop()
        iniciar_reproduccion(voice, state, ctx.guild.id, nueva_fuente)
        state["start_time"] = time.time() - segundos
        state["pausado_en"] = None
    await ctx.send(f"⏩ Pulado para {formatear_tiempo(segundos)}.")


# ── Cola ─────────────────────────────────────────────────────────────────────

@bot.hybrid_command(aliases=["q", "fila"], description="Ver a fila de reprodução")
@commands.guild_only()
async def queue(ctx):
    state = get_state(ctx.guild.id)

    if not state["current"] and not state["queue"]:
        await ctx.send("📭 A fila está vazia.")
        return

    msg = "**Fila de reprodução:**\n"
    if state["current"]:
        loop_icon = " 🔁" if state["loop"] else ""
        msg += f"▶️ **Agora:** `{recortar(state['current']['nombre'], 90)}`{loop_icon}\n"
    if state["queue"]:
        msg += "\n**Seguindo:**\n"
        for i, item in enumerate(state["queue"][:10], start=1):
            msg += f"{i}. `{recortar(item['nombre'], 90)}`\n"
        restantes = len(state["queue"]) - 10
        if restantes > 0:
            msg += f"\n*...e mais {restantes} música(s) na fila.*"

    await ctx.send(msg)

@bot.hybrid_command(description="Remover uma música da fila")
@app_commands.describe(indice="Posição na fila")
@commands.guild_only()
async def remove(ctx, indice: int):
    state = get_state(ctx.guild.id)
    if not state["queue"]:
        await ctx.send("📭 A fila está vazia.", ephemeral=True)
        return
    if not (1 <= indice <= len(state["queue"])):
        await ctx.send(f"❌ Número inválido. Escolha entre 1 e {len(state['queue'])}.", ephemeral=True)
        return

    item = state["queue"].pop(indice - 1)
    await ctx.send(f"🗑️ Removido da fila: `{item['nombre']}`")

@remove.autocomplete("indice")
async def remove_autocomplete(interaction: discord.Interaction, current: str):
    cola = get_state(interaction.guild_id)["queue"]
    actual = str(current).lower()
    return [
        app_commands.Choice(name=recortar(f"{i}. {item['nombre']}"), value=i)
        for i, item in enumerate(cola, start=1)
        if actual in f"{i}. {item['nombre']}".lower()
    ][:25]

@bot.hybrid_command(description="Embaralhar a fila")
@commands.guild_only()
async def shuffle(ctx):
    state = get_state(ctx.guild.id)
    if len(state["queue"]) < 2:
        await ctx.send("❌ Você precisa de pelo menos 2 músicas na fila para embaralhar.", ephemeral=True)
        return
    random.shuffle(state["queue"])
    await ctx.send("🔀 Fila embaralhada.")

@bot.hybrid_command(description="Esvaziar a fila (a música atual continua)")
@commands.guild_only()
async def clear(ctx):
    get_state(ctx.guild.id)["queue"].clear()
    await ctx.send("🗑️ Fila esvaziada (a música atual continua a tocar).")

@bot.hybrid_command(description="Ativar/desativar a repetição da música atual")
@commands.guild_only()
async def loop(ctx):
    state = get_state(ctx.guild.id)
    state["loop"] = not state["loop"]
    estado = "ativado 🔁" if state["loop"] else "desativado"
    await ctx.send(f"Loop {estado}.")

@bot.hybrid_command(aliases=["vol"], description="Alterar o volume (0 a 200)")
@app_commands.describe(nivel="Volume em porcentagem, de 0 a 200")
@commands.guild_only()
async def volume(ctx, nivel: commands.Range[int, 0, 200]):
    state = get_state(ctx.guild.id)
    state["volume"] = nivel / 100

    voice = ctx.voice_client
    if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
        voice.source.volume = state["volume"]

    await ctx.send(f"🔊 Volume: {nivel}%")

@bot.hybrid_command(description="Ver as músicas locais disponíveis")
async def lista(ctx):
    canciones = get_canciones()
    if not canciones:
        await ctx.send("❌ No hay canciones disponibles en la carpeta data.")
        return

    mensaje = f"**Músicas disponíveis ({len(canciones)}):**\n"
    for i, nombre in enumerate(canciones, start=1):
        linea = f"{i}. `{nombre}`\n"
        if len(mensaje) + len(linea) > 1900:
            mensaje += f"*...e mais {len(canciones) - i + 1}. Use `/play` e comece a digitar o nome.*"
            break
        mensaje += linea
    await ctx.send(mensaje)


# ── Otros comandos ───────────────────────────────────────────────────────────

@bot.hybrid_command(description="Falar um texto no canal de voz")
@app_commands.describe(texto="O que o bot deve falar")
@commands.guild_only()
async def tts(ctx, *, texto: str):
    if not await ensure_voice(ctx):
        return
    await ctx.defer()
    if await decir_por_voz(ctx.guild.id, texto):
        await ctx.send(f"🗣️ Falando: *{recortar(texto, 1500)}*")
    else:
        await ctx.send("❌ Não consegui falar agora (já estou falando ou deu erro no TTS).")

@bot.hybrid_command(description="Reiniciar a sua conversa com a IA")
async def resetia(ctx):
    conversaciones.pop(ctx.author.id, None)
    await ctx.send("🔄 Tu conversación con la IA fue reiniciada.", ephemeral=True)

@bot.hybrid_command(name="canal_musica", description="Definir o canal onde aparece o painel 'Tocando agora'")
@app_commands.describe(canal="Canal de texto para o painel de música")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def canal_musica(ctx, canal: discord.TextChannel):
    guardar_canal_musica(ctx.guild.id, canal.id)
    await ctx.send(
        f"🎧 **Canal de música configurado:** {canal.mention}\n"
        "La interfaz de **Tocando agora** aparecerá ahí automáticamente "
        "cuando haya música reproduciéndose.",
        ephemeral=True, delete_after=15,
    )

@bot.hybrid_command(name="help", aliases=["hm", "ayuda"], description="Ver os comandos do bot")
async def help_music(ctx):
    msg = (
        "**🎵 Comandos do bot** — todos funcionam com `/` ou com `m!`\n\n"
        "`/play <música/link/busca>` – Tocar ou adicionar à fila (local ou YouTube). "
        "Com `/` vai sugerindo músicas enquanto você digita\n"
        "`/skip` · `/pause` · `/resume` · `/stop`\n"
        "`/seek <tempo>` – Pular para um ponto (`90` ou `1:30`)\n"
        "`/ultima` – Tocar de novo a música anterior\n\n"
        "`/queue` – Ver fila · `/remove <número>` · `/shuffle` · `/clear`\n"
        "`/loop` – Repetir a música atual · `/volume <0-200>`\n"
        "`/lista` – Músicas locais · `/tts <texto>` – Falar no canal de voz\n"
        "`/join` · `/leave` · `/resetia`\n\n"
        "**Sem comandos:** me marque e peça (`@bot pone duki`, `@bot siguiente`, `@bot pará`), "
        f"ou escreva direto no <#{CANAL_PALABRAS_CLAVE}>."
    )
    await ctx.send(msg)


# ── Comandos Fun (solo con m!) ───────────────────────────────────────────────

@bot.command()
async def mover(ctx, usuario: discord.Member, veces: int = 1):
    if ctx.channel.id != 1192314631537053716:
        return
    canal1 = bot.get_channel(1473064555998347327)
    canal2 = bot.get_channel(954987154612822047)
    canal3 = bot.get_channel(1514142933551415356)

    if not canal1 or not canal2 or not canal3:
        await ctx.send("❌ Uno de los canales no existe.")
        return
    if usuario.voice is None:
        await ctx.send(f"❌ {usuario.mention} no está en ningún canal de voz.")
        return

    veces = max(1, min(veces, 20))  # tope para no comerse un rate limit de Discord
    for _ in range(veces // 2):
        await usuario.move_to(canal1)
        await asyncio.sleep(0.3)
        await usuario.move_to(canal2)
        await asyncio.sleep(0.3)
    await usuario.move_to(canal3)

    await ctx.send(f"✅ Listo, se movió {veces} vez/veces.")

@bot.command()
async def general(ctx):
    msg = (f"Vejo que você está inerte. Vou te explicar, seu tolo ignorante: o canal geral é "
           f"https://discord.com/channels/{GUILD_ID}/{CANAL_TEXTO_ID}")
    await ctx.send(msg)

@bot.command()
async def hola(ctx):
    await ctx.send("67")

@bot.command()
async def hablar(ctx, *, mensaje: str):
    canal = bot.get_channel(CANAL_TEXTO_ID)
    if canal:
        await canal.send(mensaje)

@bot.command()
async def hablar_usuario(ctx, usuario: discord.Member, *, mensaje: str):
    canal = bot.get_channel(CANAL_TEXTO_ID)
    if canal:
        await canal.send(f"{usuario.mention} {mensaje}")


# ── Consola ───────────────────────────────────────────────────────────────────
# Permite escribir desde la terminal del servidor: el texto se manda al canal
# (modo "texto") o se dice por voz (modo "voz"). En Docker solo funciona si el
# contenedor corre con stdin abierto (docker run -i / stdin_open: true).

def consola():
    activa = True
    modo = "texto"
    canal_id = CANAL_TEXTO_ID
    while True:
        try:
            texto = input()
        except (EOFError, OSError):
            return  # sin stdin (Docker sin -i, systemd, etc.): no hay consola
        texto = texto.strip()
        if not texto:
            continue

        if texto == "on":
            activa = True
            print("Consola activada.")
        elif texto == "off":
            activa = False
            print("Consola desactivada.")
        elif not activa:
            continue
        elif texto == "retruco":
            canal_id = CANAL_PALABRAS_CLAVE
        elif texto == "truco":
            canal_id = CANAL_TEXTO_ID
        elif texto == "voz":
            modo = "voz"
            print("Modo voz activado.")
        elif texto == "texto":
            modo = "texto"
            print("Modo texto activado.")
        elif modo == "voz":
            if not bot.voice_clients:
                print("⚠️ El bot no está en ningún canal de voz.")
                continue
            guild_id = bot.voice_clients[0].guild.id
            asyncio.run_coroutine_threadsafe(decir_por_voz(guild_id, texto), bot.loop)
        else:
            canal = bot.get_channel(canal_id)
            if canal is None:
                print("⚠️ No encuentro el canal de texto.")
                continue
            asyncio.run_coroutine_threadsafe(canal.send(texto), bot.loop)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    bot.run(TOKEN)
