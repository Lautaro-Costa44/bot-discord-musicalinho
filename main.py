import os
import json
import time
import random
import difflib
import asyncio
import threading
import discord
from dotenv import load_dotenv
from discord.ext import commands
from mutagen.mp3 import MP3
import edge_tts
import yt_dlp
import google.generativeai as genai
import google.api_core.exceptions
import io
from PIL import Image

os.chdir(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

def cargar_personalidad(archivo="personalidad.txt"):
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"⚠️ No se encontró {archivo}, usando personalidad por defecto.")
        return "Sos un bot de Discord divertido y sarcástico, respondés corto."

personalidad_bot = cargar_personalidad()
conversaciones = {}  # user_id: chat_session de Gemini
MAX_HISTORIAL = 10   # turnos a mantener

intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True
intents.message_content = True
intents.messages = True

bot = commands.Bot(
    command_prefix="m!",
    intents=intents,
    help_command=None
)

FFMPEG_PATH = "C:/ffmpeg/bin/ffmpeg.exe"
CANAL_TEXTO_ID = 1304862792238760018

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

# ── Estado global por servidor ──────────────────────────────────────────────
guilds_state = {}

def get_state(guild_id):
    if guild_id not in guilds_state:
        guilds_state[guild_id] = {
            "queue":          [],
            "loop":           False,
            "volume":         1.0,
            "current":        None,
            "sugerencias":    [],
            "sugerencias_yt": [],
            "ultima_busqueda": "",
            "start_time":     None,
            "minuto_avisado": False,
            "mensaje_now_playing": None,
            "update_task":    None,
        }
    return guilds_state[guild_id]

# ── IA integrada ───────────────────────────────────────────
def get_chat_ia(user_id):
    if user_id not in conversaciones:
        modelo = genai.GenerativeModel(
            model_name="gemini-3.1-flash-lite",
            system_instruction=personalidad_bot
        )
        conversaciones[user_id] = modelo.start_chat(history=[])
    return conversaciones[user_id]

async def preguntar_ia(user_id, nombre, mensaje, imagenes=None, contexto_usuario=""):
    chat = get_chat_ia(user_id)
    mensaje_con_nombre = f"[{nombre} dice{contexto_usuario}]: {mensaje}"
    contenido = [mensaje_con_nombre] + (imagenes or [])
    try:
        respuesta = await asyncio.to_thread(chat.send_message, contenido)
        # Recortar historial si se pasa del límite
        if len(chat.history) > MAX_HISTORIAL * 2:
            chat.history = chat.history[-MAX_HISTORIAL * 2:]
        return respuesta.text
    except google.api_core.exceptions.ResourceExhausted:
        print("[ERROR IA] Límite gratuito de Gemini agotado (429)")
        return "😴 Me quedé sin cuota de IA por hoy, probá de nuevo más tarde."
    except Exception as e:
        print(f"[ERROR IA] {e}")
        return "❌ Tuve un problema para responder, probá de nuevo en un rato."
    
CANAL_PALABRAS_CLAVE = 778306961825071115

PALABRAS_ACCION = {
    "skip":   ["saltar", "salta", "próxima", "proxima", "siguiente", "next"],
    "pause":  ["pausa", "pausá", "pausar"],
    "resume": ["seguí", "segui", "seguir", "continuá", "continua", "resume"],
    "stop":   ["parar", "pará", "para", "detené", "detene", "stop"],
    "loop":   ["repetir", "loop", "en bucle"],
    "play":   ["poné", "pone", "poner", "reproduce", "reproducí", "reproduci"],
}

async def detectar_accion_musica(message):
    if message.channel.id != CANAL_PALABRAS_CLAVE:
        return False

    texto_lower = message.content.lower()
    ctx = await bot.get_context(message)
    if ctx.command is not None:
        return False  # ya es un comando m!, no pisarlo

    for accion, palabras in PALABRAS_ACCION.items():
        for palabra in palabras:
            if palabra in texto_lower:
                if accion == "play":
                    query = texto_lower.split(palabra, 1)[1].strip()
                    if not query:
                        return False
                    await ctx.invoke(play, query=query)
                else:
                    comando = {"skip": skip, "pause": pause, "resume": resume,
                               "stop": stop, "loop": loop}[accion]
                    await ctx.invoke(comando)
                return True
    return False


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if await detectar_accion_musica(message):
        return

    es_mencion = bot.user in message.mentions
    es_reply_al_bot = (
        message.reference is not None
        and message.reference.resolved is not None
        and getattr(message.reference.resolved, "author", None) == bot.user
    )

    if es_mencion or es_reply_al_bot:
        texto = message.content
        for m in message.mentions:
            texto = texto.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        texto = texto.strip()

        contexto_usuario = ""
        if message.guild:
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
                    datos = await adj.read()
                    imagenes.append(Image.open(io.BytesIO(datos)))
                except Exception as e:
                    print(f"[ERROR IMAGEN] {e}")

        if texto or imagenes:
            if not texto:
                texto = "(mandó una imagen sin texto, comentala)"
            async with message.channel.typing():
                respuesta = await preguntar_ia(message.author.id, message.author.display_name, texto, imagenes, contexto_usuario)
            await message.reply(respuesta)

    await bot.process_commands(message)

@bot.command()
async def resetia(ctx):
    conversaciones.pop(ctx.author.id, None)
    await ctx.send("🔄 Tu conversación con la IA fue reiniciada.")

# ── Búsqueda de canciones locales ───────────────────────────────────────────

def get_canciones():
    if not os.path.exists("data"):
        return []
    return [f[:-4] for f in os.listdir("data") if f.endswith(".mp3")]

def buscar_cancion(query):
    canciones = get_canciones()
    if not canciones:
        return None, []

    query_lower = query.lower()

    for c in canciones:
        if c.lower() == query_lower:
            return c, []

    parciales = [c for c in canciones if query_lower in c.lower()]
    if len(parciales) == 1:
        return parciales[0], []
    if len(parciales) > 1:
        return None, parciales

    aproximadas = difflib.get_close_matches(query_lower,
                                            [c.lower() for c in canciones],
                                            n=5, cutoff=0.4)
    aproximadas_orig = [c for c in canciones if c.lower() in aproximadas]
    return None, aproximadas_orig


# ── Búsqueda y extracción de YouTube ────────────────────────────────────────

def formatear_duracion(segundos):
    if not segundos:
        return "??:??"
    segundos = int(segundos)
    return f"{segundos // 60}:{segundos % 60:02d}"

def buscar_youtube(query, n=5):
    """Busca en YouTube y devuelve una lista de dicts {titulo, duracion, url} sin descargar nada."""
    opts = dict(YDL_OPTS_SEARCH)
    opts["default_search"] = f"ytsearch{n}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception:
        return []

    entradas = info.get("entries") or []
    resultados = []
    for e in entradas:
        if not e:
            continue
        video_id = e.get("id")
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else e.get("url")
        resultados.append({
            "titulo": e.get("title", "Desconocido"),
            "duracion": formatear_duracion(e.get("duration")),
            "url": url,
        })
    return resultados

def extraer_playlist(url):
    """Extrae todas las entradas de una playlist de YouTube sin descargar nada."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS_PLAYLIST) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
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
            "nombre": e.get("title", "Desconocido"),
            "url": video_url,
        })
    return nombre_playlist, items

def extraer_audio_youtube(url):
    """Resuelve la URL de stream de audio directo para un link/consulta de YouTube."""
    with yt_dlp.YoutubeDL(YDL_OPTS_STREAM) as ydl:
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


# ── Monitor de tiempo ────────────────────────────────────────────────────────

async def monitor_minuto(guild_id):
    CANCION_ESPECIFICA = "Dios es tecno"

    diego_presente = False
    diego_presente2 = False
    por_primera_vez = False
    voy_a_contar_la_verdad = False
    gol_fue_con_la_mano = False

    while True:
        state = get_state(guild_id)
        if state["start_time"] is None:
            return
        actual = state["current"]
        nombre_actual = actual["nombre"] if isinstance(actual, dict) else actual
        if nombre_actual != CANCION_ESPECIFICA:
            return

        transcurrido = time.time() - state["start_time"]
        canal = bot.get_channel(CANAL_TEXTO_ID)

        if transcurrido >= 31 and not diego_presente:
            await canal.send("ESTA EL DIEGO PRESENTE")
            diego_presente = True

        if transcurrido >= 40 and not diego_presente2:
            await canal.send("ESTA EL DIEGO PRESENTE")
            diego_presente2 = True

        if transcurrido >= 54 and not por_primera_vez:
            await canal.send("QUE DIEGO")
            por_primera_vez = True

        if transcurrido >= 59 and not voy_a_contar_la_verdad:
            await canal.send("QUE COSA DIEGO")
            voy_a_contar_la_verdad = True

        if transcurrido >= 66 and not gol_fue_con_la_mano:
            await canal.send("NOOOOO")
            await canal.send("NOOOOO DIEGO")
            await canal.send("NOOOOO")
            await canal.send("NOOOOO")
            gol_fue_con_la_mano = True
            state["minuto_avisado"] = True
            return

        await asyncio.sleep(1)


# ── Reproducción interna ─────────────────────────────────────────────────────

def get_duracion(ruta):
    try:
        audio = MP3(ruta)
        segundos = int(audio.info.length)
        return f"{segundos // 60}:{segundos % 60:02d}"
    except:
        return "??:??"

def get_duracion_segundos(ruta):
    try:
        audio = MP3(ruta)
        return int(audio.info.length)
    except:
        return None


NOW_PLAYING_FILE = "now_playing.json"

def guardar_now_playing_ref(guild_id, channel_id, message_id):
    datos = {}
    if os.path.exists(NOW_PLAYING_FILE):
        try:
            with open(NOW_PLAYING_FILE, "r") as f:
                datos = json.load(f)
        except (json.JSONDecodeError, OSError):
            datos = {}
    datos[str(guild_id)] = {"channel_id": channel_id, "message_id": message_id}
    with open(NOW_PLAYING_FILE, "w") as f:
        json.dump(datos, f)

def borrar_now_playing_ref(guild_id):
    if not os.path.exists(NOW_PLAYING_FILE):
        return
    try:
        with open(NOW_PLAYING_FILE, "r") as f:
            datos = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    datos.pop(str(guild_id), None)
    with open(NOW_PLAYING_FILE, "w") as f:
        json.dump(datos, f)

async def limpiar_now_playing_anteriores():
    if not os.path.exists(NOW_PLAYING_FILE):
        return
    try:
        with open(NOW_PLAYING_FILE, "r") as f:
            datos = json.load(f)
    except (json.JSONDecodeError, OSError):
        datos = {}

    for guild_id_str, ref in datos.items():
        canal = bot.get_channel(ref["channel_id"])
        if canal is None:
            continue
        try:
            mensaje = await canal.fetch_message(ref["message_id"])
            await mensaje.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    with open(NOW_PLAYING_FILE, "w") as f:
        json.dump({}, f)


# ── Interfaz "Now Playing" ───────────────────────────────────────────────────

def formatear_tiempo(segundos):
    if segundos is None:
        return "??:??"
    segundos = int(segundos)
    return f"{segundos // 60}:{segundos % 60:02d}"

def construir_barra_progreso(elapsed, total, longitud=18):
    if not total or total <= 0:
        return "▬" * longitud
    ratio = min(max(elapsed / total, 0), 1)
    pos = int(ratio * (longitud - 1))
    return "▬" * pos + "🔘" + "▬" * (longitud - pos - 1)

def construir_embed_now_playing(item, state):
    embed = discord.Embed(
        title="🎧 Tocando agora",
        description=f"**{item['nombre']}**",
        color=discord.Color.from_rgb(88, 101, 242)
    )

    agregado_por = item.get("agregado_por", "Desconhecido")
    embed.add_field(name="Adicionado por", value=agregado_por, inline=True)
    embed.add_field(name="Fila", value=str(len(state["queue"])), inline=True)
    embed.add_field(name="Volume", value=f"{int(state['volume'] * 100)}%", inline=True)

    loop_txt = "Ativado 🔁" if state["loop"] else "Desativado"
    embed.add_field(name="Loop", value=loop_txt, inline=True)

    elapsed = time.time() - state["start_time"] if state["start_time"] else 0
    total = item.get("duracion_seg")
    barra = construir_barra_progreso(elapsed, total)
    tiempos = f"{formatear_tiempo(elapsed)} / {formatear_tiempo(total)}"
    embed.add_field(name="\u200b", value=f"{barra}\n{tiempos}", inline=False)

    thumbnail = item.get("thumbnail")
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

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
        if not voice:
            await interaction.response.send_message("❌ Não estou em um canal de voz.", ephemeral=True)
            return

        if voice.is_playing():
            voice.pause()
            button.label = "Resume"
            button.emoji = "▶️"
        elif voice.is_paused():
            voice.resume()
            button.label = "Pause"
            button.emoji = "⏸️"
        else:
            await interaction.response.send_message("❌ Não há nada se reproduzindo.", ephemeral=True)
            return

        state = get_state(self.guild_id)
        embed = construir_embed_now_playing(state["current"], state)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip_button")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = interaction.guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
            await interaction.response.send_message("⏭️ Música pulada.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Não há nada se reproduzindo.", ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="stop_button")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)
        state["queue"].clear()
        state["current"] = None

        voice = interaction.guild.voice_client
        if voice:
            voice.stop()

        tarea = state.get("update_task")
        if tarea:
            tarea.cancel()
        state["update_task"] = None
        state["mensaje_now_playing"] = None
        borrar_now_playing_ref(self.guild_id)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="⏹️ Reprodução interrompida e fila esvaziada.", embed=None, view=self)

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
            if not (voice.is_playing() or voice.is_paused()):
                return
            embed = construir_embed_now_playing(state["current"], state)
            try:
                await mensaje.edit(embed=embed)
            except discord.NotFound:
                return
    except asyncio.CancelledError:
        return


async def enviar_now_playing(ctx, item, state):
    tarea_anterior = state.get("update_task")
    if tarea_anterior:
        tarea_anterior.cancel()

    mensaje_anterior = state.get("mensaje_now_playing")
    if mensaje_anterior:
        try:
            await mensaje_anterior.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    view = MusicControlView(ctx.guild.id)
    embed = construir_embed_now_playing(item, state)
    mensaje = await ctx.send(embed=embed, view=view)

    state["mensaje_now_playing"] = mensaje
    state["update_task"] = bot.loop.create_task(actualizar_progreso(ctx.guild.id, mensaje))
    guardar_now_playing_ref(ctx.guild.id, mensaje.channel.id, mensaje.id)


async def generar_tts(texto, archivo="tts_temp.mp3"):
    communicate = edge_tts.Communicate(texto, voice="pt-BR-AntonioNeural")
    await communicate.save(archivo)
    return archivo


def play_next(voice_client, state, ctx):
    if state["loop"] and state["current"]:
        item = state["current"]
    else:
        if not state["queue"]:
            state["current"] = None
            return
        item = state["queue"].pop(0)
        state["current"] = item

    if item["tipo"] == "youtube":
        # Si viene con loop, re-resolvemos el stream (las URLs de YT expiran)
        try:
            datos = extraer_audio_youtube(item["url"])
        except Exception:
            bot.loop.create_task(ctx.send(f"⚠️ No se pudo reproducir `{item.get('nombre', item['url'])}`, saltando..."))
            play_next(voice_client, state, ctx)
            return

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                datos["stream_url"],
                executable=FFMPEG_PATH,
                before_options=FFMPEG_BEFORE_OPTIONS_YT,
            ),
            volume=state["volume"]
        )
        item["nombre"] = datos["titulo"]
        item["duracion_seg"] = datos.get("duracion_seg")
        item["thumbnail"] = datos.get("thumbnail")

        def after(error):
            bot.loop.call_soon_threadsafe(play_next, voice_client, state, ctx)

        voice_client.play(source, after=after)
        state["start_time"] = time.time()
        state["minuto_avisado"] = False
        bot.loop.create_task(enviar_now_playing(ctx, item, state))
        bot.loop.create_task(monitor_minuto(ctx.guild.id))
        return

    # ── local ──
    archivo = item["nombre"]
    ruta = f"data/{archivo}.mp3"
    if not os.path.exists(ruta):
        bot.loop.create_task(ctx.send(f"⚠️ No se encontró `{archivo}.mp3`, saltando..."))
        play_next(voice_client, state, ctx)
        return

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(ruta, executable=FFMPEG_PATH),
        volume=state["volume"]
    )

    def after(error):
        async def borrar():
            await asyncio.sleep(1)
            if os.path.exists(archivo):
                try:
                    os.remove(archivo)
                except:
                    pass
        asyncio.run_coroutine_threadsafe(borrar(), bot.loop)
        bot.loop.call_soon_threadsafe(play_next, voice_client, state, ctx)

    voice_client.play(source, after=after)
    state["start_time"] = time.time()
    state["minuto_avisado"] = False
    item["duracion_seg"] = get_duracion_segundos(ruta)
    bot.loop.create_task(enviar_now_playing(ctx, item, state))
    bot.loop.create_task(monitor_minuto(ctx.guild.id))


@bot.command()
async def tts(ctx, *, texto: str):
    if not await ensure_voice(ctx):
        return

    archivo = "tts_temp.mp3"
    await generar_tts(texto, archivo)

    voice = ctx.voice_client
    if voice.is_playing():
        voice.stop()

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(archivo, executable=FFMPEG_PATH),
        volume=get_state(ctx.guild.id)["volume"]
    )

    def after(error):
        if os.path.exists(archivo):
            os.remove(archivo)

    voice.play(source, after=after)
    await ctx.send(f"🗣️ Falando: *{texto}*")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def ensure_voice(ctx):
    if ctx.author.voice is None:
        await ctx.send("❌ No estás en un canal de voz.")
        return False
    canal = ctx.author.voice.channel
    if ctx.voice_client is None:
        await canal.connect()
    else:
        await ctx.voice_client.move_to(canal)
    return True

def es_url_youtube(texto):
    return "youtube.com/watch" in texto or "youtu.be/" in texto

def es_url_playlist(texto):
    return "list=" in texto


# ── Eventos ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")
    print(f"Servidores conectados ({len(bot.guilds)}):")
    for guild in bot.guilds:
        print(f"  - {guild.name} (ID: {guild.id})")
    await limpiar_now_playing_anteriores()
    hilo = threading.Thread(target=consola, daemon=True)
    hilo.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        comando = ctx.invoked_with
        servidor = ctx.guild.name if ctx.guild else "DM"
        usuario = ctx.author.name
        print(f"[COMANDO NO ENCONTRADO] '{comando}' usado por '{usuario}' en '{servidor}'")
        return

@bot.event
async def on_guild_remove(guild):
    guilds_state.pop(guild.id, None)


# ── Comandos de conexión ─────────────────────────────────────────────────────

@bot.command()
async def join(ctx):
    if not await ensure_voice(ctx):
        return
    await ctx.send("✅ Conectado ao canal de voz.")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        state = get_state(ctx.guild.id)
        state["queue"].clear()
        state["current"] = None
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Eu saí do canal.")


# ── Comandos de reproducción ─────────────────────────────────────────────────

@bot.command()
async def play(ctx, *, query: str):
    if not await ensure_voice(ctx):
        return

    state = get_state(ctx.guild.id)

    # "m!play 0" con sugerencias locales activas -> forzar búsqueda en YouTube
    if query == "0" and state.get("sugerencias"):
        busqueda = state["ultima_busqueda"]
        state["sugerencias"] = []
        state["sugerencias_yt"] = []

        await ctx.send(f"🔍 Buscando `{busqueda}` no YouTube...")
        resultados = buscar_youtube(busqueda)
        if not resultados:
            await ctx.send("❌ Não consegui encontrar nada no YouTube.")
            return
        state["sugerencias_yt"] = resultados
        msg = "📺 Resultados do YouTube:\n"
        for i, r in enumerate(resultados, 1):
            msg += f"`{i}.` {r['titulo']} ({r['duracion']})\n"
        msg += "\n*Responda com `m!play <número>` para escolher.*"
        await ctx.send(msg)
        return

    # Selección numérica de una sugerencia previa (local o YouTube)
    if query.isdigit() and (state.get("sugerencias") or state.get("sugerencias_yt")):
        idx = int(query) - 1

        if state.get("sugerencias"):
            sugerencias = state["sugerencias"]
            if 0 <= idx < len(sugerencias):
                item = {"tipo": "local", "nombre": sugerencias[idx]}
                state["sugerencias"] = []
                state["sugerencias_yt"] = []
            else:
                await ctx.send(f"❌ Número inválido. Escolha entre 1 y {len(sugerencias)}, o 0 para buscar no YouTube.")
                return
        else:
            sugerencias_yt = state["sugerencias_yt"]
            if 0 <= idx < len(sugerencias_yt):
                elegido = sugerencias_yt[idx]
                item = {"tipo": "youtube", "url": elegido["url"], "nombre": elegido["titulo"]}
                state["sugerencias_yt"] = []
            else:
                await ctx.send(f"❌ Número inválido. Escolha entre 1 y {len(sugerencias_yt)}.")
                return
    else:
        state["sugerencias"] = []
        state["sugerencias_yt"] = []

        # Link de playlist de YouTube
        if es_url_playlist(query):
            await ctx.send("🔍 Extraindo playlist...")
            nombre_playlist, items = extraer_playlist(query)
            if not items:
                await ctx.send("❌ Não consegui extrair a playlist.")
                return

            for it in items:
                it["agregado_por"] = ctx.author.display_name

            voice = ctx.voice_client
            embed = discord.Embed(
                description=f"**\"{nombre_playlist}\"** com **{len(items)}** músicas adicionadas à fila.",
                color=discord.Color.green()
            )

            if voice.is_playing() or voice.is_paused():
                state["queue"].extend(items)
                await ctx.send(embed=embed)
            else:
                state["queue"] = items
                await ctx.send(embed=embed)
                play_next(voice, state, ctx)
            return

        # Link directo de YouTube
        if es_url_youtube(query):
            item = {"tipo": "youtube", "url": query, "nombre": query}
        else:
            exacta, sugerencias = buscar_cancion(query)

            if exacta:
                item = {"tipo": "local", "nombre": exacta}
            elif sugerencias:
                state["sugerencias"] = sugerencias
                state["ultima_busqueda"] = query
                msg = f"🔍 Não consegui encontrar a `{query}` exata. O que você quis dizer?\n"
                for i, s in enumerate(sugerencias, 1):
                    msg += f"`{i}.` {s}\n"
                msg += "\n*Responda com `m!play <número>` para escolher, ou `m!play 0` para buscar no YouTube.*"
                await ctx.send(msg)
                return
            else:
                # Nada local: buscamos en YouTube
                await ctx.send(f"🔍 Não encontrei `{query}` local. Buscando no YouTube...")
                resultados = buscar_youtube(query)
                if not resultados:
                    await ctx.send("❌ Não consegui encontrar nada no YouTube.")
                    return
                state["sugerencias_yt"] = resultados
                msg = "📺 Resultados do YouTube:\n"
                for i, r in enumerate(resultados, 1):
                    msg += f"`{i}.` {r['titulo']} ({r['duracion']})\n"
                msg += "\n*Responda com `m!play <número>` para escolher.*"
                await ctx.send(msg)
                return

    item["agregado_por"] = ctx.author.display_name

    voice = ctx.voice_client
    nombre_mostrar = item["nombre"]
    if voice.is_playing() or voice.is_paused():
        state["queue"].append(item)
        await ctx.send(f"➕ Adicionado à fila: `{nombre_mostrar}` (posición {len(state['queue'])})")
    else:
        state["queue"] = [item]
        play_next(voice, state, ctx)

@bot.command()
async def skip(ctx):
    voice = ctx.voice_client
    if voice and (voice.is_playing() or voice.is_paused()):
        voice.stop()
        await ctx.send("⏭️ Pular música...")
    else:
        await ctx.send("❌ Não há nada se reproduzindo.")

@bot.command()
async def stop(ctx):
    state = get_state(ctx.guild.id)
    state["queue"].clear()
    state["current"] = None
    if ctx.voice_client:
        ctx.voice_client.stop()

    tarea = state.get("update_task")
    if tarea:
        tarea.cancel()
    state["update_task"] = None
    state["mensaje_now_playing"] = None
    borrar_now_playing_ref(ctx.guild.id)

    await ctx.send("⏹️ A reprodução foi interrompida e a cauda esvaziada.")

@bot.command()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Pausado.")
    else:
        await ctx.send("❌ Não há nada se reproduzindo.")

@bot.command()
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Retomado.")
    else:
        await ctx.send("❌ Nada está em pausa.")


# ── Cola ─────────────────────────────────────────────────────────────────────

@bot.command()
async def queue(ctx):
    state = get_state(ctx.guild.id)

    if not state["current"] and not state["queue"]:
        await ctx.send("📭 A fila está vazia.")
        return

    msg = "**Cauda de reprodução:**\n"
    if state["current"]:
        loop_icon = " 🔁" if state["loop"] else ""
        msg += f"▶️ **Agora:** `{state['current']['nombre']}`{loop_icon}\n"
    if state["queue"]:
        msg += "\n**Seguindo:**\n"
        for i, item in enumerate(state["queue"], start=1):
            msg += f"{i}. `{item['nombre']}`\n"

    await ctx.send(msg)

@bot.command()
async def shuffle(ctx):
    state = get_state(ctx.guild.id)
    if len(state["queue"]) < 2:
        await ctx.send("❌ Você precisa de pelo menos 2 músicas na fila para mixar.")
        return
    random.shuffle(state["queue"])
    await ctx.send("🔀 Fila mista.")

@bot.command()
async def clear(ctx):
    state = get_state(ctx.guild.id)
    state["queue"].clear()
    await ctx.send("🗑️ Fila esvaziada (a música atual continua a tocar).")


# ── Loop ─────────────────────────────────────────────────────────────────────

@bot.command()
async def loop(ctx):
    state = get_state(ctx.guild.id)
    state["loop"] = not state["loop"]
    estado = "ativado 🔁" if state["loop"] else "desativado"
    await ctx.send(f"Loop {estado}.")


# ── Volumen ──────────────────────────────────────────────────────────────────

@bot.command()
async def volume(ctx, nivel: int):
    if not (0 <= nivel <= 200):
        await ctx.send("❌ O volume deve estar entre 0 e 200.")
        return

    state = get_state(ctx.guild.id)
    state["volume"] = nivel / 100

    voice = ctx.voice_client
    if voice and voice.source and isinstance(voice.source, discord.PCMVolumeTransformer):
        voice.source.volume = state["volume"]

    await ctx.send(f"🔊 Volume: {nivel}%")


# ── Lista de canciones ───────────────────────────────────────────────────────

@bot.command()
async def lista(ctx):
    carpeta = "data"
    if not os.path.exists(carpeta):
        await ctx.send("❌ La carpeta 'data' no existe.")
        return

    temas = sorted(f for f in os.listdir(carpeta) if f.endswith(".mp3"))
    if not temas:
        await ctx.send("❌ No hay canciones disponibles en la carpeta data.")
        return

    mensaje = "**Músicas disponíveis:**\n"
    for i, tema in enumerate(temas, start=1):
        nombre_limpio = tema[:-4]
        mensaje += f"{i}. `{nombre_limpio}`\n"

    await ctx.send(mensaje)


# ── Ayuda personalizada ───────────────────────────────────────────────────────

@bot.command(name="help", aliases=["hm"])
async def help_music(ctx):
    msg = (
        "**🎵 Comandos do bot**\n\n"
        "`m!join` – Entrar no canal de voz\n"
        "`m!leave` – Sair do canal de voz\n"
        "`m!lista` – Ver músicas disponíveis (locais)\n\n"
        "`m!play <música/link/consulta>` – Reproduzir / adicionar à fila (local ou YouTube)\n"
        "`m!skip` – Pular música atual\n"
        "`m!stop` – Parar e limpar a fila\n"
        "`m!pause` – Pausar\n"
        "`m!resume` – Retomar\n\n"
        "`m!queue` – Ver fila\n"
        "`m!shuffle` – Embaralhar fila\n"
        "`m!clear` – Limpar fila\n"
        "`m!loop` – Ativar/desativar loop\n"
        "`m!volume <0-200>` – Alterar volume\n"
    )
    await ctx.send(msg)


# ── Comandos Fun ─────────────────────────────────────────────────────────────

@bot.command()
async def mover(ctx, usuario: discord.Member, veces: int = 1):
    if ctx.channel.id != 1192314631537053716:
        return
    canal1 = bot.get_channel(1473064555998347327)
    canal2 = bot.get_channel(954987154612822047)
    canal3 = bot.get_channel(1514142933551415356)

    if not canal1 or not canal2:
        await ctx.send("❌ Uno de los canales no existe.")
        return
    if usuario.voice is None:
        await ctx.send(f"❌ {usuario.mention} no está en ningún canal de voz.")
        return

    for _ in range(veces//2):
        await usuario.move_to(canal1)
        await asyncio.sleep(0.3)
        await usuario.move_to(canal2)
        await asyncio.sleep(0.3)
    await usuario.move_to(canal3)

    await ctx.send(f"✅ Listo, se movió {veces} vez/veces.")

@bot.command()
async def general(ctx):
    msg = "Vejo que você está inerte. Vou te explicar, seu tolo ignorante: o canal geral é https://discord.com/channels/778128763899871234/1304862792238760018"
    await ctx.send(msg)

@bot.command()
async def hola(ctx):
    await ctx.send("67")

@bot.command()
async def hablar(ctx, *, mensaje: str):
    await bot.get_channel(CANAL_TEXTO_ID).send(mensaje)

@bot.command()
async def hablar_usuario(ctx, usuario: discord.Member, *, mensaje: str):
    await bot.get_channel(CANAL_TEXTO_ID).send(f"{usuario.mention} {mensaje}")


# ── Consola ───────────────────────────────────────────────────────────────────

consola_activa = True
modo_consola = "texto"

def construir_source(item, volumen, seek=0):
    seek_opts = f"-ss {seek}" if seek > 0 else ""

    if item["tipo"] == "local":
        ruta = f"data/{item['nombre']}.mp3"
        return discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(ruta, executable=FFMPEG_PATH,
                                    before_options=seek_opts or None),
            volume=volumen
        )
    else:
        datos = extraer_audio_youtube(item["url"])
        before_options = f"{seek_opts} {FFMPEG_BEFORE_OPTIONS_YT}".strip()
        return discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(datos["stream_url"], executable=FFMPEG_PATH,
                                    before_options=before_options),
            volume=volumen
        )


async def hablar_por_voz(texto):
    if not bot.voice_clients:
        print("⚠️ El bot no está en ningún canal de voz.")
        return

    voice = bot.voice_clients[0]
    state = get_state(voice.guild.id)

    item_actual = state["current"]
    estaba_sonando = (voice.is_playing() or voice.is_paused()) and item_actual is not None
    elapsed = 0
    if estaba_sonando and state["start_time"]:
        elapsed = time.time() - state["start_time"]
        voice.stop()

    archivo = "tts_consola.mp3"
    await generar_tts(texto, archivo)

    fin = asyncio.Event()

    def after_tts(error):
        bot.loop.call_soon_threadsafe(fin.set)
        if os.path.exists(archivo):
            try:
                os.remove(archivo)
            except:
                pass

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(archivo, executable=FFMPEG_PATH),
        volume=state["volume"]
    )
    voice.play(source, after=after_tts)
    await fin.wait()

    if estaba_sonando:
        try:
            nuevo_source = construir_source(item_actual, state["volume"], seek=max(0, int(elapsed) - 1))
        except Exception:
            print("⚠️ No se pudo reanudar la canción tras el TTS.")
            return

        voice.play(nuevo_source, after=lambda e: None)
        state["start_time"] = time.time() - elapsed


def consola():
    global modo_consola
    canal = bot.get_channel(CANAL_TEXTO_ID)
    while True:
        texto = input()
        if not consola_activa:
            continue
        if texto == "on":
            globals()["consola_activa"] = True
            print("Consola activada.")
        elif texto == "off":
            globals()["consola_activa"] = False
            print("Consola desactivada.")
        elif texto == "retruco":
            canal = bot.get_channel(778306961825071115)
        elif texto == "truco":
            canal = bot.get_channel(CANAL_TEXTO_ID)
        elif texto == "voz":
            modo_consola = "voz"
            print("Modo voz activado.")
        elif texto == "texto":
            modo_consola = "texto"
            print("Modo texto activado.")
        else:
            if modo_consola == "voz":
                asyncio.run_coroutine_threadsafe(hablar_por_voz(texto), bot.loop)
            else:
                asyncio.run_coroutine_threadsafe(canal.send(texto), bot.loop)


bot.run(TOKEN)