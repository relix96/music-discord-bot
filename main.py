import os
import sys
import asyncio
import subprocess
import tempfile
import uuid
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

import discord
from discord.ext import commands

import yt_dlp

# ====== CONFIG ======
TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # ou mete o token diretamente (não recomendado)
COMMAND_PREFIX = "!"  # Ex: !play
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
        self.play_next = asyncio.Event()
        self.audio_task: Optional[asyncio.Task] = None
        self.current_ytdl_process: Optional[subprocess.Popen[bytes]] = None

guild_states: dict[int, GuildMusicState] = {}


def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildMusicState()
    return guild_states[guild_id]


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient:
    """Garante que o bot está no canal de voz do utilizador."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("Tens de estar num canal de voz para eu entrar.")

    voice = ctx.voice_client
    if voice and voice.is_connected():
        # Se já está ligado mas noutro canal, move
        if voice.channel != ctx.author.voice.channel:
            await voice.move_to(ctx.author.voice.channel)
        return voice

    return await ctx.author.voice.channel.connect()


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
    """Loop que consome a fila e toca música (download com yt-dlp → ficheiro → FFmpeg)."""
    state = get_state(guild.id)

    while True:
        state.play_next.clear()
        state.current_ytdl_process = None

        item = await state.queue.get()

        voice: discord.VoiceClient = guild.voice_client
        if voice is None or not voice.is_connected():
            continue

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


@bot.command(name="join")
async def join(ctx: commands.Context):
    await ensure_voice(ctx)
    await ctx.reply("Entrei no teu canal de voz ✅")


@bot.command(name="play")
async def play(ctx: commands.Context, *, query: str):
    """
    Uso:
    !play <link do youtube>
    !play <título / texto para pesquisar>
    """
    if not _is_ffmpeg_available():
        return await ctx.reply(
            "⚠️ FFmpeg não encontrado. Para tocar áudio:\n"
            "1. Descarrega: https://ffmpeg.org/download.html\n"
            "2. Adiciona a pasta **bin** ao PATH do sistema,\n"
            "   ou no `.env` define: `FFMPEG_PATH=C:\\caminho\\para\\ffmpeg.exe`"
        )
    voice = await ensure_voice(ctx)
    state = get_state(ctx.guild.id)

    # cria o loop do player uma vez por servidor
    if state.audio_task is None or state.audio_task.done():
        state.audio_task = bot.loop.create_task(player_loop(ctx.guild))

    try:
        info = await asyncio.to_thread(extract_info, query)
    except Exception as e:
        raise commands.CommandError(f"Não consegui obter esse áudio. Detalhes: {e}")

    await state.queue.put(info)

    msg = f"Adicionado à fila: **{info['title']}**"
    if info.get("webpage_url"):
        msg += f"\n{info['webpage_url']}"
    await ctx.reply(msg)

    # Se não está a tocar, força começar (às vezes o voice pode estar parado)
    if voice and not voice.is_playing() and not voice.is_paused():
        # o loop já vai puxar da fila; isto só ajuda em casos raros
        pass


@bot.command(name="skip")
async def skip(ctx: commands.Context):
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
    voice = ctx.voice_client
    if voice and voice.is_playing():
        voice.pause()
        await ctx.reply("⏸️ Pausado.")
    else:
        await ctx.reply("Não estou a tocar nada.")


@bot.command(name="resume")
async def resume(ctx: commands.Context):
    voice = ctx.voice_client
    if voice and voice.is_paused():
        voice.resume()
        await ctx.reply("▶️ Retomado.")
    else:
        await ctx.reply("Não está pausado.")


@bot.command(name="stop")
async def stop(ctx: commands.Context):
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

    if voice.is_playing() or voice.is_paused():
        voice.stop()

    await ctx.reply("⏹️ Parei e limpei a fila.")


@bot.command(name="leave")
async def leave(ctx: commands.Context):
    voice = ctx.voice_client
    if voice and voice.is_connected():
        await voice.disconnect()
        await ctx.reply("Saí do canal de voz 👋")
    else:
        await ctx.reply("Não estou ligado a nenhum canal de voz.")


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


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Define a variável de ambiente DISCORD_BOT_TOKEN com o token do teu bot.")
    bot.run(TOKEN)
