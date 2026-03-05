import os
import sys
import asyncio
import subprocess
import tempfile
import time
import uuid
import platform
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

import discord
from discord.ext import commands

import yt_dlp

# ====== CONFIG ======
TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # ou mete o token diretamente (não recomendado)
COMMAND_PREFIX = "!"  # Ex: !play
INACTIVITY_LEAVE_SECONDS = 10 * 60  # Auto !leave after 10 minutes of inactivity
# Caminho para o FFmpeg (obrigatório para voz). Se não estiver no PATH, define em .env:
# FFMPEG_PATH=C:\caminho\para\ffmpeg.exe
def _resolve_ffmpeg() -> str:
    import shutil
    path = os.getenv("FFMPEG_PATH", "").strip()
    if path and os.path.isfile(path):
        return path
    if path:
        return path  # user set it; usar mesmo que o ficheiro não exista (erro ao tocar)
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    # Locais comuns no Windows (winget, chocolatey, Stremio, instalação manual)
    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\ffmpeg\bin\ffmpeg.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\LNV\Stremio-4\ffmpeg.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return "ffmpeg"


FFMPEG_EXECUTABLE = _resolve_ffmpeg()

# Opções do yt-dlp: pega o melhor áudio
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch1",  # se não for link, faz search no YouTube
    "quiet": True,
    "no_warnings": True,
}

# FFmpeg: para ficheiro local usar opções mínimas (Stremio/outros builds podem falhar com -reconnect/-probesize)
FFMPEG_BEFORE_OPTS_FILE = "-nostdin"
FFMPEG_OPTS = "-vn"


class _FFmpegStderrSink:
    """Encaminha stderr do FFmpeg para o terminal (para ver erros)."""
    def write(self, data: bytes) -> None:
        if data:
            sys.stderr.buffer.write(data)
            sys.stderr.buffer.flush()
    def flush(self) -> None:
        sys.stderr.buffer.flush()

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

intents = discord.Intents.default()
intents.message_content = True  # necessário para comandos por mensagem

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# ====== Estado por servidor (guild) ======
class GuildMusicState:
    def __init__(self):
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.queue_list: list[dict] = []  # Lista para exibir a fila
        self.currently_playing: Optional[dict] = None  # Item atualmente a tocar
        self.play_next = asyncio.Event()
        self.audio_task: Optional[asyncio.Task] = None
        self.current_ytdl_process: Optional[subprocess.Popen[bytes]] = None
        self.last_activity_at: float = 0.0  # For inactivity auto-leave
        self.last_channel_id: Optional[int] = None  # To send auto-leave message
        self.voice_connect_lock = asyncio.Lock()  # Avoid concurrent connect/move races
    
    def get_queue_display(self) -> list[dict]:
        """Retorna a lista completa da fila (incluindo o que está a tocar)."""
        queue = []
        if self.currently_playing:
            queue.append(self.currently_playing)
        queue.extend(self.queue_list)
        return queue

guild_states: dict[int, GuildMusicState] = {}


def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in guild_states:
        st = GuildMusicState()
        st.last_activity_at = time.monotonic()
        guild_states[guild_id] = st
    return guild_states[guild_id]


def touch_activity(guild_id: int, channel_id: Optional[int] = None) -> None:
    """Update last activity time (and optionally last channel) for inactivity auto-leave."""
    state = get_state(guild_id)
    state.last_activity_at = time.monotonic()
    if channel_id is not None:
        state.last_channel_id = channel_id


async def inactivity_check_loop() -> None:
    """Background task: if in voice and inactive for INACTIVITY_LEAVE_SECONDS, run !leave."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(60)  # Check every minute
            now = time.monotonic()
            for guild in bot.guilds:
                voice = guild.voice_client
                if not voice or not voice.is_connected():
                    continue
                state = get_state(guild.id)
                if voice.is_playing() or voice.is_paused():
                    continue
                if state.queue_list or not state.queue.empty():
                    continue
                if (now - state.last_activity_at) < INACTIVITY_LEAVE_SECONDS:
                    continue
                # Auto leave (same as !leave)
                try:
                    await voice.disconnect()
                    if state.last_channel_id:
                        ch = guild.get_channel(state.last_channel_id)
                        if ch and isinstance(ch, discord.TextChannel):
                            await ch.send("Saí do canal de voz por inatividade (10 min). 👋")
                except Exception as e:
                    print(f"[INACTIVITY] Erro ao sair em {guild.name}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[INACTIVITY] {e}")


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient:
    """Garante que o bot está no canal de voz do utilizador."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("Tens de estar num canal de voz para eu entrar.")

    state = get_state(ctx.guild.id)
    target_channel = ctx.author.voice.channel

    async with state.voice_connect_lock:
        voice = ctx.guild.voice_client
        if voice and voice.is_connected():
            # Se já está ligado mas noutro canal, move
            if voice.channel != target_channel:
                try:
                    await voice.move_to(target_channel)
                except Exception as e:
                    raise commands.CommandError(f"Erro ao mover para o canal: {e}")
            return voice

        # Tenta limpar ligação "presa" antes de novo connect
        if voice:
            try:
                await voice.disconnect(force=True)
            except Exception:
                pass
            await asyncio.sleep(0.5)

        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                # Keep retries under our control; discord.py internal reconnect can loop on stale sessions.
                voice_client = await target_channel.connect(
                    timeout=45.0,
                    reconnect=False,
                    self_deaf=True,
                )
                if voice_client and voice_client.is_connected():
                    return voice_client
                try:
                    if voice_client:
                        await voice_client.disconnect(force=True)
                except Exception:
                    pass
                last_error = commands.CommandError("Conexão estabelecida mas não está ativa.")
            except asyncio.TimeoutError as e:
                last_error = e
            except discord.ClientException as e:
                error_msg = str(e)
                if "Already connected" in error_msg and ctx.guild.voice_client:
                    vc = ctx.guild.voice_client
                    if vc.is_connected():
                        return vc
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass
                last_error = e
            except (discord.errors.ConnectionClosed, discord.ConnectionClosed) as e:
                last_error = e
            except Exception as e:
                last_error = e

            # Reset entre tentativas para evitar estado de handshake antigo (ex: 4017)
            vc = ctx.guild.voice_client
            if vc:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
            await asyncio.sleep(min(2.0 * attempt, 6.0))

        if isinstance(last_error, asyncio.TimeoutError):
            raise commands.CommandError(
                "⏱️ Timeout ao conectar ao canal de voz.\n\n"
                "Possíveis soluções:\n"
                "1. Verifica permissões Connect/Speak\n"
                "2. Verifica firewall/UDP\n"
                "3. Reinicia o bot\n"
                "4. Confirma dependências de voz: `pip install PyNaCl davey`"
            )

        if isinstance(last_error, (discord.errors.ConnectionClosed, discord.ConnectionClosed)):
            code = getattr(last_error, "code", None)
            if code in (4006, 4017):
                raise commands.CommandError(
                    f"Sessão de voz inválida ({code}). Tenta `!leave` e depois `!play` novamente em 10-20s."
                )
            raise commands.CommandError(
                f"Conexão de voz fechada pelo Discord (código {code or '?'}). Tenta novamente em alguns segundos."
            )

        if isinstance(last_error, discord.ClientException):
            raise commands.CommandError(f"Erro ao conectar: {last_error}")

        error_type = type(last_error).__name__ if last_error else "UnknownError"
        raise commands.CommandError(
            f"Erro inesperado ao conectar ({error_type}): {last_error}\n"
            "Verifica dependências de voz (PyNaCl/davey), permissões do bot e conectividade de rede."
        )


def download_audio_to_file(url: str) -> Optional[str]:
    """
    Descarrega áudio com yt-dlp (API Python) para um ficheiro temporário.
    Devolve o caminho do ficheiro ou None em caso de erro.
    """
    base = os.path.join(tempfile.gettempdir(), "discord_bot_" + uuid.uuid4().hex)
    out_template = base + ".%(ext)s"
    # Preferir m4a (AAC): o FFmpeg do Stremio pode não suportar Opus/webm → return code 1
    opts = {
        **YTDL_OPTS,
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "no_warnings": True,
        "quiet": False,
        "no_check_certificates": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            print("[PLAYER] yt-dlp: sem informação do vídeo")
            return None
        # Caminho do ficheiro descarregado (yt-dlp preenche requested_downloads)
        req = info.get("requested_downloads") or []
        if req:
            path = req[0].get("filepath") or req[0].get("filename")
            if path and os.path.isfile(path):
                return path
        # Fallback: construir path a partir de ext
        ext = info.get("ext") or "m4a"
        path = base + "." + ext
        return path if os.path.isfile(path) else None
    except yt_dlp.utils.DownloadError as e:
        print(f"[PLAYER] yt-dlp: {e}")
        return None
    except Exception as e:
        print(f"[PLAYER] yt-dlp: {e}")
        return None


def _get_audio_url(info: dict) -> Optional[str]:
    """Extrai URL de áudio do resultado do yt-dlp (suporta DASH e formatos simples)."""
    if info.get("url"):
        return info["url"]
    requested = info.get("requested_formats")
    if requested:
        for f in requested:
            if f.get("url") and (f.get("vcodec") == "none" or f.get("acodec")):
                return f["url"]
        if requested[0].get("url"):
            return requested[0]["url"]
    for f in reversed(info.get("formats", [])):
        if f.get("url") and (f.get("vcodec") == "none" or not f.get("vcodec")):
            return f["url"]
    return None


def is_local_file(query: str) -> bool:
    """Verifica se a query é um caminho de ficheiro local."""
    # Remove aspas se existirem
    query = query.strip().strip('"').strip("'")
    # Verifica se parece um caminho de ficheiro (contém / ou \ ou começa com C: ou similar)
    if os.path.sep in query or (len(query) > 1 and query[1] == ':'):
        return os.path.isfile(query)
    return False


def get_file_info(file_path: str) -> Optional[dict]:
    """
    Retorna info de um ficheiro local MP3.
    Devolve None se o ficheiro não existir ou não for válido.
    """
    # Remove aspas se existirem
    file_path = file_path.strip().strip('"').strip("'")
    
    if not os.path.isfile(file_path):
        return None
    
    # Verifica extensão (aceita mp3, m4a, wav, flac, etc.)
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ['.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac']:
        return None
    
    # Usa o nome do ficheiro como título
    title = os.path.basename(file_path)
    
    # Marca como ficheiro local (sem webpage_url)
    return {
        "title": title,
        "webpage_url": None,
        "url": None,
        "file_path": file_path,  # Campo especial para ficheiros locais
        "duration": None,
    }


def extract_info(query: str) -> dict:
    """
    Retorna info de um vídeo.
    - Se query for link, usa direto
    - Se for texto, yt-dlp faz search por causa do default_search
    """
    info = ytdl.extract_info(query, download=False)

    # Se for search, vem uma lista em info["entries"]
    if "entries" in info:
        info = info["entries"][0]

    url = _get_audio_url(info) or info.get("url")
    webpage_url = info.get("webpage_url") or info.get("url")

    return {
        "title": info.get("title", "Sem título"),
        "webpage_url": webpage_url,
        "url": url,
        "duration": info.get("duration"),
    }


async def player_loop(guild: discord.Guild):
    """Loop que consome a fila e toca música (download com yt-dlp → ficheiro → FFmpeg ou ficheiro local)."""
    state = get_state(guild.id)

    while True:
        state.play_next.clear()
        state.current_ytdl_process = None
        state.currently_playing = None

        item = await state.queue.get()
        
        # Remove o item da lista de fila quando começa a tocar
        if item in state.queue_list:
            state.queue_list.remove(item)
        
        # Marca como atualmente a tocar (reseta inatividade)
        state.currently_playing = item
        touch_activity(guild.id)

        voice: discord.VoiceClient = guild.voice_client
        if voice is None or not voice.is_connected():
            continue

        # Verifica se é um ficheiro local
        file_path = item.get("file_path")
        if file_path:
            # Ficheiro local: usar diretamente
            if not os.path.isfile(file_path):
                print(f"[PLAYER] Ficheiro não encontrado: {file_path}")
                bot.loop.call_soon_threadsafe(state.play_next.set)
                continue
            
            try:
                source = discord.FFmpegPCMAudio(
                    file_path,
                    executable=FFMPEG_EXECUTABLE,
                    before_options=FFMPEG_BEFORE_OPTS_FILE,
                    options=FFMPEG_OPTS,
                    stderr=_FFmpegStderrSink(),
                )
            except Exception as e:
                print(f"[PLAYER] Erro ao criar source: {e}")
                bot.loop.call_soon_threadsafe(state.play_next.set)
                continue

            audio = discord.PCMVolumeTransformer(source, volume=0.7)

            # Verifica se é um ficheiro temporário de anexo (deve ser limpo após tocar)
            is_temp_attachment = (
                file_path.startswith(tempfile.gettempdir()) and 
                os.path.basename(file_path).startswith("discord_bot_")
            )

            def after_play_local(err):
                if err:
                    print(f"[PLAYER] Erro: {err}")
                # Limpa ficheiros temporários de anexos
                if is_temp_attachment:
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                    except OSError:
                        pass
                bot.loop.call_soon_threadsafe(state.play_next.set)

            voice.play(audio, after=after_play_local)
            await state.play_next.wait()
            continue

        # Ficheiro remoto: descarregar primeiro
        play_url = item.get("webpage_url") or item.get("url")
        if not play_url:
            print(f"[PLAYER] Sem URL para: {item.get('title', '?')}")
            bot.loop.call_soon_threadsafe(state.play_next.set)
            continue

        # Descarregar áudio para ficheiro temporário (mais fiável que stream/pipe)
        temp_path = await asyncio.to_thread(download_audio_to_file, play_url)
        if not temp_path or not os.path.isfile(temp_path):
            print(f"[PLAYER] Falha ao descarregar: {item.get('title', '?')}")
            bot.loop.call_soon_threadsafe(state.play_next.set)
            continue

        try:
            source = discord.FFmpegPCMAudio(
                temp_path,
                executable=FFMPEG_EXECUTABLE,
                before_options=FFMPEG_BEFORE_OPTS_FILE,
                options=FFMPEG_OPTS,
                stderr=_FFmpegStderrSink(),
            )
        except Exception as e:
            print(f"[PLAYER] Erro ao criar source: {e}")
            try:
                os.remove(temp_path)
            except OSError:
                pass
            bot.loop.call_soon_threadsafe(state.play_next.set)
            continue

        audio = discord.PCMVolumeTransformer(source, volume=0.7)

        def after_play(err, path: str):
            if err:
                print(f"[PLAYER] Erro: {err}")
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            bot.loop.call_soon_threadsafe(state.play_next.set)

        voice.play(audio, after=lambda e: after_play(e, temp_path))

        await state.play_next.wait()


@bot.event
async def on_ready():
    print(f"Logado como {bot.user} (ID: {bot.user.id})")
    bot.loop.create_task(inactivity_check_loop())


@bot.command(name="join")
async def join(ctx: commands.Context):
    touch_activity(ctx.guild.id, ctx.channel.id)
    missing_voice_libs = _missing_voice_libraries()
    if missing_voice_libs:
        pip_pkgs = " ".join("PyNaCl" if lib == "PyNaCl" else "davey" for lib in missing_voice_libs)
        return await ctx.reply(
            f"⚠️ Dependências de voz em falta: {', '.join(missing_voice_libs)}.\n"
            f"Instala com: `pip install {pip_pkgs}`"
        )
    try:
        await ensure_voice(ctx)
        await ctx.reply("Entrei no teu canal de voz ✅")
    except commands.CommandError as e:
        await ctx.reply(str(e))


@bot.command(name="play")
async def play(ctx: commands.Context, *, query: str = ""):
    """
    Uso:
    !play <link do youtube>
    !play <título / texto para pesquisar>
    !play <caminho para ficheiro MP3>
    Ou anexa um ficheiro de áudio com !play
    """
    touch_activity(ctx.guild.id, ctx.channel.id)
    if not _is_ffmpeg_available():
        return await ctx.reply(
            "⚠️ FFmpeg não encontrado. Para tocar áudio:\n"
            "1. Descarrega: https://ffmpeg.org/download.html\n"
            "2. Adiciona a pasta **bin** ao PATH do sistema,\n"
            "   ou no `.env` define: `FFMPEG_PATH=C:\\caminho\\para\\ffmpeg.exe`"
        )
    missing_voice_libs = _missing_voice_libraries()
    if missing_voice_libs:
        pip_pkgs = " ".join("PyNaCl" if lib == "PyNaCl" else "davey" for lib in missing_voice_libs)
        return await ctx.reply(
            f"⚠️ Dependências de voz em falta: {', '.join(missing_voice_libs)}.\n"
            f"Instala com: `pip install {pip_pkgs}`"
        )
    try:
        voice = await ensure_voice(ctx)
    except commands.CommandError as e:
        return await ctx.reply(str(e))
    state = get_state(ctx.guild.id)

    # cria o loop do player uma vez por servidor
    if state.audio_task is None or state.audio_task.done():
        state.audio_task = bot.loop.create_task(player_loop(ctx.guild))

    # Verifica se há anexos (ficheiros) na mensagem
    SUPPORTED_AUDIO_EXT = ('.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac')
    if ctx.message.attachments:
        infos: list[dict] = []
        for attachment in ctx.message.attachments:
            ext = os.path.splitext(attachment.filename)[1].lower() if attachment.filename else ""
            if ext not in SUPPORTED_AUDIO_EXT:
                continue  # skip unsupported attachments
            temp_path = os.path.join(tempfile.gettempdir(), f"discord_bot_{uuid.uuid4().hex}{ext}")
            try:
                await attachment.save(temp_path)
            except Exception as e:
                raise commands.CommandError(f"Erro ao descarregar o ficheiro {attachment.filename}: {e}")
            infos.append({
                "title": attachment.filename or "Ficheiro anexado",
                "webpage_url": None,
                "url": None,
                "file_path": temp_path,
                "duration": None,
            })
        if not infos:
            raise commands.CommandError(
                f"Nenhum ficheiro de áudio nos anexos. Formatos suportados: MP3, M4A, WAV, FLAC, OGG, OPUS, AAC"
            )
        # Add all to queue; we'll use the first as "info" for the legacy single-item path, then add the rest
        info = infos[0]
        for i in infos:
            await state.queue.put(i)
            state.queue_list.append(i)
        # Build reply and skip the single put/append below
        queue_display = state.get_queue_display()
        total_items = len(queue_display)
        if len(infos) == 1:
            msg = f"✅ Adicionado à fila: **{info['title']}**"
        else:
            msg = f"✅ Adicionados **{len(infos)}** ficheiros à fila:\n"
            for idx, it in enumerate(infos, 1):
                msg += f"  {idx}. {it['title']}\n"
        msg += f"\n📋 **Fila ({total_items} {'item' if total_items == 1 else 'itens'}):**"
        for idx, queue_item in enumerate(queue_display, 1):
            prefix = "▶️" if idx == 1 and state.currently_playing == queue_item else f"{idx}."
            title = queue_item.get('title', 'Sem título')
            msg += f"\n{prefix} {title}"
        await ctx.reply(msg)
        if voice and not voice.is_playing() and not voice.is_paused():
            pass
        return
    elif query.strip():
        # Verifica se é um ficheiro local primeiro
        if is_local_file(query):
            info = await asyncio.to_thread(get_file_info, query)
            if not info:
                raise commands.CommandError(f"Ficheiro não encontrado ou formato não suportado: {query}")
        else:
            # Tenta obter info do YouTube/outras fontes
            try:
                info = await asyncio.to_thread(extract_info, query)
            except Exception as e:
                raise commands.CommandError(f"Não consegui obter esse áudio. Detalhes: {e}")
    else:
        raise commands.CommandError("Fornece um link, pesquisa, caminho de ficheiro, ou anexa um ficheiro de áudio!")

    await state.queue.put(info)
    state.queue_list.append(info)

    # Constrói mensagem com a fila
    queue_display = state.get_queue_display()
    total_items = len(queue_display)
    
    msg = f"✅ Adicionado à fila: **{info['title']}**"
    if info.get("webpage_url"):
        msg += f"\n🔗 {info['webpage_url']}"
    
    # Mostra a fila se houver mais de 1 item
    if total_items > 1:
        msg += f"\n\n📋 **Fila ({total_items} {'item' if total_items == 1 else 'itens'}):**"
        for idx, queue_item in enumerate(queue_display, 1):
            prefix = "▶️" if idx == 1 and state.currently_playing == queue_item else f"{idx}."
            title = queue_item.get('title', 'Sem título')
            msg += f"\n{prefix} {title}"
    
    await ctx.reply(msg)

    # Se não está a tocar, força começar (às vezes o voice pode estar parado)
    if voice and not voice.is_playing() and not voice.is_paused():
        # o loop já vai puxar da fila; isto só ajuda em casos raros
        pass


@bot.command(name="skip")
async def skip(ctx: commands.Context):
    touch_activity(ctx.guild.id, ctx.channel.id)
    voice = ctx.voice_client
    if not voice or not voice.is_connected():
        return await ctx.reply("Não estou ligado a nenhum canal de voz.")
    if voice.is_playing():
        voice.stop()
        await ctx.reply("⏭️ Skip.")
    else:
        await ctx.reply("Não estou a tocar nada.")


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    touch_activity(ctx.guild.id, ctx.channel.id)
    voice = ctx.voice_client
    if voice and voice.is_playing():
        voice.pause()
        await ctx.reply("⏸️ Pausado.")
    else:
        await ctx.reply("Não estou a tocar nada.")


@bot.command(name="resume")
async def resume(ctx: commands.Context):
    touch_activity(ctx.guild.id, ctx.channel.id)
    voice = ctx.voice_client
    if voice and voice.is_paused():
        voice.resume()
        await ctx.reply("▶️ Retomado.")
    else:
        await ctx.reply("Não está pausado.")


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    touch_activity(ctx.guild.id, ctx.channel.id)
    voice = ctx.voice_client
    if not voice or not voice.is_connected():
        return await ctx.reply("Não estou ligado a nenhum canal de voz.")

    # esvazia a fila
    state = get_state(ctx.guild.id)
    while not state.queue.empty():
        try:
            state.queue.get_nowait()
            state.queue.task_done()
        except asyncio.QueueEmpty:
            break
    
    # Limpa a lista de fila também
    state.queue_list.clear()
    state.currently_playing = None

    if voice.is_playing() or voice.is_paused():
        voice.stop()

    await ctx.reply("⏹️ Parei e limpei a fila.")


@bot.command(name="leave")
async def leave(ctx: commands.Context):
    touch_activity(ctx.guild.id, ctx.channel.id)
    voice = ctx.voice_client
    if voice and voice.is_connected():
        await voice.disconnect()
        await ctx.reply("Saí do canal de voz 👋")
    else:
        await ctx.reply("Não estou ligado a nenhum canal de voz.")


@bot.command(name="queue")
async def queue_cmd(ctx: commands.Context):
    """Mostra a fila de música atual."""
    touch_activity(ctx.guild.id, ctx.channel.id)
    state = get_state(ctx.guild.id)
    queue_display = state.get_queue_display()
    
    if not queue_display:
        return await ctx.reply("📋 A fila está vazia.")
    
    total_items = len(queue_display)
    msg = f"📋 **Fila ({total_items} {'item' if total_items == 1 else 'itens'}):**\n\n"
    
    for idx, item in enumerate(queue_display, 1):
        prefix = "▶️" if idx == 1 and state.currently_playing == item else f"{idx}."
        title = item.get('title', 'Sem título')
        msg += f"{prefix} {title}\n"
    
    await ctx.reply(msg)


@bot.command(name="voiceinfo")
async def voiceinfo(ctx: commands.Context):
    """Comando de diagnóstico para verificar o estado da conexão de voz."""
    info_lines = []
    
    # Verifica bibliotecas de voz
    pynacl_ok = _is_pynacl_available()
    davey_ok = _is_davey_available()
    info_lines.append(f"PyNaCl: {'✅ Instalado' if pynacl_ok else '❌ Não instalado'}")
    info_lines.append(f"davey: {'✅ Instalado' if davey_ok else '❌ Não instalado'}")
    
    # Runtime (útil para problemas de voz)
    info_lines.append(f"Python: {sys.version.split()[0]} ({platform.architecture()[0]})")
    info_lines.append(f"discord.py: {discord.__version__}")
    if sys.version_info >= (3, 14):
        info_lines.append("⚠️ Python 3.14+ pode ter incompatibilidades com stack de voz.")
    if platform.architecture()[0] == "32bit":
        info_lines.append("⚠️ Runtime 32-bit pode causar instabilidade em voz; preferir 64-bit.")
    
    # Verifica FFmpeg
    ffmpeg_ok = _is_ffmpeg_available()
    info_lines.append(f"FFmpeg: {'✅ Disponível' if ffmpeg_ok else '❌ Não encontrado'}")
    
    # Verifica se o utilizador está num canal
    if ctx.author.voice and ctx.author.voice.channel:
        info_lines.append(f"Canal do utilizador: {ctx.author.voice.channel.name}")
        
        # Verifica permissões do bot
        bot_member = ctx.guild.get_member(bot.user.id)
        if bot_member:
            perms = ctx.author.voice.channel.permissions_for(bot_member)
            info_lines.append(f"Permissão Connect: {'✅' if perms.connect else '❌'}")
            info_lines.append(f"Permissão Speak: {'✅' if perms.speak else '❌'}")
    else:
        info_lines.append("❌ Não estás num canal de voz")
    
    # Verifica estado atual da conexão
    voice = ctx.voice_client
    if voice:
        info_lines.append(f"Estado da conexão: {'✅ Conectado' if voice.is_connected() else '❌ Desconectado'}")
        if voice.is_connected():
            info_lines.append(f"Canal conectado: {voice.channel.name}")
    else:
        info_lines.append("Estado da conexão: ❌ Sem conexão")
    
    await ctx.reply("```\n" + "\n".join(info_lines) + "\n```")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandError):
        await ctx.reply(f"⚠️ {error}")
    else:
        await ctx.reply("⚠️ Ocorreu um erro inesperado.")
        raise error


def _is_ffmpeg_available() -> bool:
    """True se o FFmpeg estiver acessível (PATH ou FFMPEG_PATH no .env)."""
    import shutil
    return os.path.isfile(FFMPEG_EXECUTABLE) or shutil.which(FFMPEG_EXECUTABLE) is not None


def _is_pynacl_available() -> bool:
    """True se o PyNaCl estiver instalado (necessário para voz)."""
    try:
        import nacl
        return True
    except ImportError:
        return False


def _is_davey_available() -> bool:
    """True se o davey estiver instalado (necessário para voz em discord.py 2.7+)."""
    try:
        import davey
        return True
    except ImportError:
        return False


def _missing_voice_libraries() -> list[str]:
    """Lista bibliotecas de voz em falta."""
    missing: list[str] = []
    if not _is_pynacl_available():
        missing.append("PyNaCl")
    if not _is_davey_available():
        missing.append("davey")
    return missing


def _validate_runtime_for_voice() -> None:
    """
    Valida runtime para estabilidade de voz no Discord.
    Nota: Python 3.14 e builds 32-bit têm mostrado falhas de handshake (ex: close code 4017).
    """
    py_ver = sys.version_info
    arch = platform.architecture()[0]
    if py_ver >= (3, 14) or arch == "32bit":
        msg = (
            "Runtime incompatível para voz do Discord.\n"
            f"- Python atual: {py_ver.major}.{py_ver.minor}.{py_ver.micro}\n"
            f"- Arquitetura: {arch}\n\n"
            "Recomendado:\n"
            "1. Instalar Python 3.12 ou 3.13 (64-bit)\n"
            "2. Recriar a venv\n"
            "3. Reinstalar dependências: pip install -r requirements.txt\n"
        )
        strict_runtime = os.getenv("STRICT_VOICE_RUNTIME", "").strip() == "1"
        if strict_runtime:
            raise RuntimeError(msg)
        print("[WARN] " + msg.replace("\n", " "))


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Define a variável de ambiente DISCORD_BOT_TOKEN com o token do teu bot.")
    _validate_runtime_for_voice()
    bot.run(TOKEN)

