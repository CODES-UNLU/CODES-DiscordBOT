import asyncio
import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import discord
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("codes-discord-bot")


@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int
    endpoint_url: str
    endpoint_limit: int
    request_timeout_seconds: int
    poll_interval_hours: float
    send_on_start: bool
    state_file: Path
    embed_title: str
    embed_description_prefix: str
    embed_color_hex: str
    embed_logo_url: str
    embed_thumbnail_url: str
    embed_footer: str


    @staticmethod
    def from_env() -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        channel_raw = os.getenv("CHANNEL_ID", "").strip()
        endpoint_url = os.getenv("ENDPOINT_URL", "").strip()

        if not token:
            raise ValueError("Falta DISCORD_TOKEN en variables de entorno")
        if not channel_raw.isdigit():
            raise ValueError("CHANNEL_ID debe ser un numero valido")
        if not endpoint_url:
            raise ValueError("Falta ENDPOINT_URL en variables de entorno")

        return Config(
            discord_token=token,
            channel_id=int(channel_raw),
            endpoint_url=endpoint_url,
            endpoint_limit=max(1, min(int(os.getenv("ENDPOINT_LIMIT", "5")), 20)),
            request_timeout_seconds=max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))),
            poll_interval_hours=max(0.1, float(os.getenv("POLL_INTERVAL_HOURS", "12"))),
            send_on_start=os.getenv("SEND_ON_START", "false").strip().lower() in {"1", "true", "yes", "si"},
            state_file=Path(os.getenv("STATE_FILE", "bot_state.json")),
            embed_title=os.getenv("EMBED_TITLE", "Proximos eventos de CODES").strip(),
            embed_description_prefix=os.getenv(
                "EMBED_DESCRIPTION_PREFIX", "Se detectaron cambios en el calendario."
            ).strip(),
            embed_color_hex=os.getenv("EMBED_COLOR_HEX", "#1F8B4C").strip(),
            embed_logo_url=os.getenv("EMBED_LOGO_URL", "").strip(),
            embed_thumbnail_url=os.getenv("EMBED_THUMBNAIL_URL", "").strip(),
            embed_footer=os.getenv("EMBED_FOOTER", "UNLu CODES").strip(),
        )


def safe_embed_color(hex_color: str) -> discord.Color:
    color = hex_color.strip().lstrip("#")
    if len(color) != 6:
        return discord.Color.blue()
    try:
        return discord.Color(int(color, 16))
    except ValueError:
        return discord.Color.blue()


def stable_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_saved_hash(state_file: Path) -> str | None:
    if not state_file.exists():
        return None

    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        current_hash = raw.get("hash")
        return current_hash if isinstance(current_hash, str) else None
    except (json.JSONDecodeError, OSError):
        logger.warning("No se pudo leer el archivo de estado. Se vuelve a generar.")
        return None


def save_hash(state_file: Path, new_hash: str) -> None:
    state_file.write_text(json.dumps({"hash": new_hash}, ensure_ascii=True), encoding="utf-8")


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def format_event_date(value: Any) -> str:
    date_raw = str(value or "-").strip()
    if not date_raw or date_raw == "-":
        return "-"

    try:
        return datetime.strptime(date_raw, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return date_raw


def build_events_embed(config: Config, payload: dict[str, Any]) -> discord.Embed:
    events = payload.get("events", [])

    embed = discord.Embed(
        title=config.embed_title,
        description=config.embed_description_prefix,
        color=safe_embed_color(config.embed_color_hex),
    )

    if config.embed_thumbnail_url:
        # Usamos imagen en lugar de thumbnail para mostrarla debajo de los campos.
        embed.set_image(url=config.embed_thumbnail_url)

    embed.set_footer(text="Centro de Estudiantes Codes++ • Licenciatura en Sistemas")

    if not isinstance(events, list) or not events:
        embed.add_field(name="Eventos", value="No hay eventos proximos.", inline=False)
        return embed

    for idx, event in enumerate(events[:10], start=1):
        title = truncate(str(event.get("title", "Sin titulo")), 80)
        description = truncate(str(event.get("description", "Sin descripcion")), 300)
        date = format_event_date(event.get("date", "-"))

        value = description
        field_name = truncate(f"{idx}. {title} - {date}", 256)

        embed.add_field(name=field_name, value=truncate(value, 1024), inline=False)

    return embed


async def clear_channel(channel: discord.TextChannel) -> None:
    messages = [message async for message in channel.history(limit=None)]
    if not messages:
        return

    for message in messages:
        try:
            await message.delete()
        except discord.HTTPException as exc:
            logger.warning("No se pudo borrar mensaje %s: %s", message.id, exc)


class CalendarWatcherBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        super().__init__(intents=intents)

        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.poll_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self.poll_task = asyncio.create_task(self.poll_loop())

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poll_task
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Bot conectado como %s", self.user)

    def build_endpoint_url(self) -> str:
        parts = urlsplit(self.config.endpoint_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["limit"] = str(self.config.endpoint_limit)

        hostname = (parts.hostname or "").lower()
        is_local_host = hostname in {"localhost", "127.0.0.1", "::1"}
        scheme = parts.scheme.lower()

        # En desarrollo local, muchos servidores (ej. Vite) exponen HTTP sin TLS.
        if is_local_host and scheme == "https":
            logger.warning(
                "ENDPOINT_URL usa https en host local (%s). Se fuerza http para evitar error SSL.",
                hostname,
            )
            parts = parts._replace(scheme="http")

        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def fetch_calendar_payload(self) -> dict[str, Any]:
        if not self.session:
            raise RuntimeError("Sesion HTTP no inicializada")

        url = self.build_endpoint_url()

        async with self.session.get(url) as response:
            response.raise_for_status()
            payload = await response.json()
            if not isinstance(payload, dict):
                raise ValueError("Respuesta del endpoint invalida")
            return payload

    async def post_update(self, payload: dict[str, Any]) -> None:
        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.channel_id)

        if not isinstance(channel, discord.TextChannel):
            raise ValueError("El CHANNEL_ID no corresponde a un canal de texto")

        await clear_channel(channel)
        embed = build_events_embed(self.config, payload)
        await channel.send(embed=embed)

    async def poll_loop(self) -> None:
        state_file = self.config.state_file
        saved_hash = load_saved_hash(state_file)
        first_iteration = True

        await self.wait_until_ready()

        while not self.is_closed():
            try:
                payload = await self.fetch_calendar_payload()
                current_hash = stable_hash(payload)

                has_changed = saved_hash is not None and current_hash != saved_hash
                should_post_initial = first_iteration

                if should_post_initial:
                    logger.info("Arranque detectado. Se limpia canal y se publica estado actual.")
                    await self.post_update(payload)
                elif has_changed:
                    logger.info("Cambio detectado. Se actualiza canal.")
                    await self.post_update(payload)
                else:
                    logger.info("Sin cambios en el endpoint.")

                save_hash(state_file, current_hash)
                saved_hash = current_hash
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Error durante el polling: %s", exc)
            finally:
                first_iteration = False
                await asyncio.sleep(self.config.poll_interval_hours * 3600)


if __name__ == "__main__":
    cfg = Config.from_env()
    bot = CalendarWatcherBot(cfg)
    bot.run(cfg.discord_token)
