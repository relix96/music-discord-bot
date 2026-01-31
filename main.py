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
        self.queue_list: list[dict] = []  # Lista para exibir a fila
        self.currently_playing: Optional[dict] = None  # Item atualmente a tocar
        self.play_next = asyncio.Event()
        self.audio_task: Optional[asyncio.Task] = None
        self.current_ytdl_process: Optional[subprocess.Popen[bytes]] = None
    
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
            try:
                await voice.move_to(ctx.author.voice.channel)
            except Exception as e:
                raise commands.CommandError(f"Erro ao mover para o canal: {e}")
        return voice

    # Tenta conectar - aumenta o timeout e remove o wrapper duplo
    try:
        # Usa timeout maior (60s é o padrão do Discord.py, mas alguns casos precisam mais)
        voice_client = await ctx.author.voice.channel.connect(timeout=60.0, reconnect=True)
        # Espera um pouco para garantir que a conexão está estável
        await asyncio.sleep(1.0)
        
        # Verifica se realmente está conectado
        if not voice_client.is_connected():
            await voice_client.disconnect(force=True)
            raise commands.CommandError("Conexão estabelecida mas não está ativa. Tenta novamente.")
        
        return voice_client
    except asyncio.TimeoutError:
        raise commands.CommandError(
            "⏱️ Timeout ao conectar ao canal de voz.\n\n"
            "**Possíveis soluções:**\n"
            "1. Verifica se o bot tem permissões 'Connect' e 'Speak' no canal\n"
            "2. Verifica se há firewall bloqueando conexões UDP\n"
            "3. Tenta reiniciar o bot\n"
            "4. Verifica se o PyNaCl está instalado: `pip install PyNaCl`"
        )
    except discord.ClientException as e:
        error_msg = str(e)
        if "Already connected" in error_msg:
            # Já está conectado, retorna o voice client existente
            return ctx.voice_client
        raise commands.CommandError(f"Erro ao conectar: {error_msg}")
    except (discord.errors.ConnectionClosed, discord.ConnectionClosed) as e:
        raise commands.CommandError(
            f"Conexão fechada pelo Discord: {e}\n"
            "Isto pode ser um problema temporário. Tenta novamente em alguns segundos."
        )
    except Exception as e:
        error_type = type(e).__name__
        raise commands.CommandError(
            f"Erro inesperado ao conectar ({error_type}): {e}\n\n"
            "Verifica:\n"
            "- Se o PyNaCl está instalado: `pip install PyNaCl`\n"
            "- Se há problemas de rede/firewall\n"
            "- Se o bot tem as permissões necessárias"
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
        
        # Marca como atualmente a tocar
        state.currently_playing = item

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


@bot.command(name="join")
async def join(ctx: commands.Context):
    if not _is_pynacl_available():
        return await ctx.reply(
            "⚠️ PyNaCl não está instalado. É necessário para conexões de voz.\n"
            "Instala com: `pip install PyNaCl`"
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
    if not _is_ffmpeg_available():
        return await ctx.reply(
            "⚠️ FFmpeg não encontrado. Para tocar áudio:\n"
            "1. Descarrega: https://ffmpeg.org/download.html\n"
            "2. Adiciona a pasta **bin** ao PATH do sistema,\n"
            "   ou no `.env` define: `FFMPEG_PATH=C:\\caminho\\para\\ffmpeg.exe`"
        )
    if not _is_pynacl_available():
        return await ctx.reply(
            "⚠️ PyNaCl não está instalado. É necessário para conexões de voz.\n"
            "Instala com: `pip install PyNaCl`"
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
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
        
        # Verifica se é um ficheiro de áudio
        ext = os.path.splitext(attachment.filename)[1].lower() if attachment.filename else ""
        if ext not in ['.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac']:
            raise commands.CommandError(f"Formato de ficheiro não suportado: {ext}. Formatos suportados: MP3, M4A, WAV, FLAC, OGG, OPUS, AAC")
        
        # Descarrega o anexo para um ficheiro temporário
        temp_path = os.path.join(tempfile.gettempdir(), f"discord_bot_{uuid.uuid4().hex}{ext}")
        try:
            await attachment.save(temp_path)
        except Exception as e:
            raise commands.CommandError(f"Erro ao descarregar o ficheiro: {e}")
        
        # Cria info para o ficheiro descarregado
        info = {
            "title": attachment.filename or "Ficheiro anexado",
            "webpage_url": None,
            "url": None,
            "file_path": temp_path,
            "duration": None,
        }
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
    
    # Limpa a lista de fila também
    state.queue_list.clear()
    state.currently_playing = None

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


@bot.command(name="queue")
async def queue_cmd(ctx: commands.Context):
    """Mostra a fila de música atual."""
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
    
    # Verifica PyNaCl
    pynacl_ok = _is_pynacl_available()
    info_lines.append(f"PyNaCl: {'✅ Instalado' if pynacl_ok else '❌ Não instalado'}")
    
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


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Define a variável de ambiente DISCORD_BOT_TOKEN com o token do teu bot.")
    bot.run(TOKEN)
