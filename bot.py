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

STUDENTS_FILE = Path("data/students.json")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int
    sede_channel_id: int
    rules_channel_id: int
    verify_info_channel_id: int
    student_role_id: int
    sede_roles: dict[str, int]
    endpoint_url: str
    endpoint_limit: int
    request_timeout_seconds: int
    poll_interval_hours: float
    send_on_start: bool
    state_file: Path
    embed_color_hex: str


    @staticmethod
    def load() -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("Falta DISCORD_TOKEN en variables de entorno (o archivo .env)")

        config_path = Path("config.json")
        if not config_path.exists():
            raise FileNotFoundError("Falta config.json. Copia config.example.json a config.json y completalo.")

        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        channel_raw = str(data.get("CHANNEL_ID", "")).strip()
        sede_channel_raw = str(data.get("SEDE_CHANNEL_ID", "")).strip()
        rules_channel_raw = str(data.get("RULES_CHANNEL_ID", "")).strip()
        verify_info_raw = str(data.get("VERIFY_INFO_CHANNEL_ID", "")).strip()
        student_role_raw = str(data.get("STUDENT_ROLE_ID", "")).strip()
        endpoint_url = str(data.get("ENDPOINT_URL", "")).strip()

        if not channel_raw.isdigit():
            raise ValueError("CHANNEL_ID en config.json debe ser un numero valido")
        if not sede_channel_raw.isdigit():
            raise ValueError("SEDE_CHANNEL_ID en config.json debe ser un numero valido")
        if not rules_channel_raw.isdigit():
            raise ValueError("RULES_CHANNEL_ID en config.json debe ser un numero valido")
        if not verify_info_raw.isdigit():
            raise ValueError("VERIFY_INFO_CHANNEL_ID en config.json debe ser un numero valido")
        if not student_role_raw.isdigit():
            raise ValueError("STUDENT_ROLE_ID en config.json debe ser un numero valido")
        if not endpoint_url:
            raise ValueError("Falta ENDPOINT_URL en config.json")

        raw_roles = data.get("SEDE_ROLES", {})
        sede_roles = {name: int(role_id) for name, role_id in raw_roles.items()}

        return Config(
            discord_token=token,
            channel_id=int(channel_raw),
            sede_channel_id=int(sede_channel_raw),
            rules_channel_id=int(rules_channel_raw),
            verify_info_channel_id=int(verify_info_raw),
            student_role_id=int(student_role_raw),
            sede_roles=sede_roles,
            endpoint_url=endpoint_url,
            endpoint_limit=max(1, min(int(data.get("ENDPOINT_LIMIT", 5)), 20)),
            request_timeout_seconds=max(5, int(data.get("REQUEST_TIMEOUT_SECONDS", 20))),
            poll_interval_hours=max(0.1, float(data.get("POLL_INTERVAL_HOURS", 12.0))),
            send_on_start=bool(data.get("SEND_ON_START", False)),
            state_file=Path(data.get("STATE_FILE", "bot_state.json")),
            embed_color_hex=str(data.get("EMBED_COLOR_HEX", "#1F8B4C")).strip(),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    state_file.parent.mkdir(parents=True, exist_ok=True)
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


def fit_nickname(full_name: str, limit: int = 32) -> str:
    """Acorta un nombre para que quepa en el límite de Discord (32 chars).

    Estrategia:
    1. Si el nombre completo cabe, usarlo tal cual.
    2. Si no, intentar con "Nombre Apellido" (primero + último).
    3. Si aún no cabe, truncar a `limit` caracteres.
    """
    name = full_name.strip()
    if len(name) <= limit:
        return name

    parts = name.split()
    if len(parts) >= 2:
        short = f"{parts[0]} {parts[-1]}"
        if len(short) <= limit:
            return short

    return name[:limit]


# ---------------------------------------------------------------------------
# Students storage
# ---------------------------------------------------------------------------

def load_students() -> dict[str, dict[str, str]]:
    if not STUDENTS_FILE.exists():
        return {}
    try:
        return json.loads(STUDENTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("No se pudo leer students.json. Se inicia vacío.")
        return {}


def save_student(user_id: int, name: str, legajo: str) -> None:
    students = load_students()
    students[str(user_id)] = {
        "name": name,
        "legajo": legajo,
        "verified_at": datetime.now().isoformat(),
    }
    STUDENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STUDENTS_FILE.write_text(
        json.dumps(students, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_verified_count() -> int:
    return len(load_students())


def is_student_verified(user_id: int) -> bool:
    students = load_students()
    return str(user_id) in students


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_events_embed(config: Config, payload: dict[str, Any]) -> discord.Embed:
    events = payload.get("events", [])

    embed = discord.Embed(
        title="📅  Próximos eventos universitarios y del CODES",
        color=safe_embed_color(config.embed_color_hex),
    )

    embed.set_footer(text="⎯⎯  Centro de Estudiantes Codes++  •  Licenciatura en Sistemas  ⎯⎯")

    if not isinstance(events, list) or not events:
        embed.description = "\n> *No hay eventos próximos.*\n"
        return embed

    lines: list[str] = []

    for idx, event in enumerate(events[:10], start=1):
        title = truncate(str(event.get("title", "Sin título")), 80)
        description = truncate(str(event.get("description", "")).strip(), 300)
        date = format_event_date(event.get("date", "-"))

        lines.append(f"### {idx}.  {title}")

        if date and date != "-":
            lines.append(f"> 🗓️  `{date}`")

        if description and description.lower() not in {"sin descripción", "sin descripcion", ""}:
            lines.append(f"> {description}")

        lines.append("")

    embed.description = "\n".join(lines)

    return embed


def build_sede_embed(config: Config) -> discord.Embed:
    embed = discord.Embed(
        title="📍  Seleccioná tu Sede / Centro Regional",
        color=safe_embed_color(config.embed_color_hex),
    )

    lines = [
        "Elegí tu sede de cursada en el menú desplegable de abajo.",
        "Al seleccionar una sede se te asignará el rol correspondiente.",
        "",
        "> ⚠️ *Solo podés tener **una sede** asignada a la vez.*",
        "> *Si elegís otra, la anterior se reemplaza automáticamente.*",
        "",
        "### Sedes disponibles",
    ]

    for name in config.sede_roles:
        lines.append(f"  •  {name}")

    embed.description = "\n".join(lines)
    embed.set_footer(text="⎯⎯  Centro de Estudiantes Codes++  •  Licenciatura en Sistemas  ⎯⎯")
    return embed


def build_rules_embed(config: Config) -> discord.Embed:
    accepted = get_verified_count()

    embed = discord.Embed(
        title="📜  Normas de la Comunidad — Codes++",
        color=safe_embed_color(config.embed_color_hex),
    )

    lines = [
        "Para mantener un ambiente de aprendizaje y colaboración sano, todos los",
        "miembros de la **Licenciatura en Sistemas** debemos seguir estas pautas:",
        "",
        "### 1️⃣  Respeto y Cordialidad",
        "> Trata a tus compañeros y docentes con respeto. No se tolerará el acoso, la",
        "> discriminación ni los insultos de ningún tipo.",
        "",
        "### 2️⃣  Canales Temáticos",
        "> Utiliza los canales correspondientes para cada tema.",
        "",
        "### 3️⃣  Contenido Académico",
        "> Ayuda a otros, pero evita fomentar el plagio. Compartir material de estudio es",
        "> bienvenido, pero las resoluciones de exámenes en vivo están prohibidas.",
        "",
        "### 4️⃣  Spam y Publicidad",
        "> Prohibido el spam de otros servidores, productos o servicios sin autorización",
        "> previa del Centro de Estudiantes.",
        "",
        "### 5️⃣  Identidad Real",
        "> Al aceptar las reglas se te pedirá tu **nombre real** y **número de legajo**.",
        "> Tu apodo en el servidor se cambiará a tu nombre real para facilitar la",
        "> comunicación académica.",
        "",
        "*El desconocimiento de estas reglas no exime de su cumplimiento.*",
    ]

    embed.description = "\n".join(lines)
    embed.set_footer(
        text=f"✅ {accepted} estudiante{'s' if accepted != 1 else ''} verificado{'s' if accepted != 1 else ''}  •  Centro de Estudiantes Codes++"
    )
    return embed


def build_verification_info_embed(config: Config) -> discord.Embed:
    embed = discord.Embed(
        title="🛡️  Sistema de Verificación — Codes++",
        color=safe_embed_color(config.embed_color_hex),
    )

    lines = [
        "Para acceder al servidor y sus canales necesitás completar",
        "el proceso de verificación. Es rápido y solo se hace una vez.",
        "",
        "### 🤖  Paso 1 — Verificación por IP (Double Counter)",
        "> Justo abajo vas a ver un mensaje del bot **Double Counter**.",
        "> Seguí sus instrucciones para verificar tu IP. Esto evita cuentas",
        "> falsas. **Importante:** Desactivá cualquier VPN antes de hacerlo.",
        "",
        "### 📝  Paso 2 — Aceptar las reglas",
        "> Andá al canal de reglas y leé las normas de la comunidad.",
        "> Presioná el botón **✅ Aceptar reglas** cuando estés de acuerdo.",
        "",
        "### 🪧  Paso 3 — Verificar tu identidad",
        "> Se te abrirá un formulario donde debés ingresar:",
        "> •  Tu **nombre completo** real",
        "> •  Tu **número de legajo** universitario",
        "> Tu apodo en Discord se cambiará automáticamente a tu nombre real.",
        "",
        "### 🎓  Paso 4 — Obtener el rol de Estudiante",
        "> Al completar el formulario se te asigna automáticamente el rol",
        "> de **Estudiante**, que te da acceso a los canales del servidor.",
        "",
        "### 🏫  Paso 5 — Elegir tu sede",
        "> Una vez verificado, andá al canal de sedes y elegí tu sede",
        "> de cursada en el menú desplegable. Solo podés tener una a la vez.",
        "",
        "### ℹ️  Información adicional",
        "> •  Tu nombre y legajo quedan registrados para validación interna.",
        "> •  El bot verifica periódicamente que los datos sean consistentes.",
        "> •  Si cambiás tu apodo manualmente, el bot lo va a corregir.",
        "> •  Si necesitás actualizar tus datos, contactá a un administrador.",
    ]

    embed.description = "\n".join(lines)
    embed.set_footer(text="⎯⎯  Centro de Estudiantes Codes++  •  Licenciatura en Sistemas  ⎯⎯")
    return embed


# ---------------------------------------------------------------------------
# Rules: Modal + Button + View
# ---------------------------------------------------------------------------

class StudentInfoModal(discord.ui.Modal, title="Verificación de Estudiante"):
    nombre = discord.ui.TextInput(
        label="Nombre completo",
        placeholder="Ej: Juan Pérez",
        min_length=3,
        max_length=100,
        required=True,
    )
    legajo = discord.ui.TextInput(
        label="Número de legajo",
        placeholder="Ej: 12345",
        min_length=1,
        max_length=20,
        required=True,
    )

    def __init__(self, config: Config, bot: "CalendarWatcherBot"):
        super().__init__()
        self.config = config
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "No se pudo determinar tu usuario.", ephemeral=True
            )
            return

        name = self.nombre.value.strip()
        legajo = self.legajo.value.strip()

        # Validar que el legajo sea numérico
        if not legajo.isdigit():
            await interaction.response.send_message(
                "❌ El número de legajo debe contener solo números. Intentá de nuevo.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando solo funciona en un servidor.", ephemeral=True
            )
            return

        # Guardar datos del estudiante
        save_student(member.id, name, legajo)

        # Asignar rol de estudiante
        student_role = guild.get_role(self.config.student_role_id)
        if student_role and student_role not in member.roles:
            try:
                await member.add_roles(student_role, reason="Aceptó reglas y verificó identidad")
            except discord.HTTPException as exc:
                logger.warning("No se pudo asignar rol estudiante a %s: %s", member, exc)

        # Cambiar nickname (acortado si es necesario)
        nickname = fit_nickname(name)
        try:
            await member.edit(nick=nickname, reason="Verificación de identidad")
        except discord.HTTPException as exc:
            logger.warning("No se pudo cambiar nick de %s: %s", member, exc)

        await interaction.response.send_message(
            f"✅ ¡Bienvenido/a **{name}**! (Legajo: `{legajo}`)\n"
            f"Se te asignó el rol de estudiante y tu apodo fue actualizado. 🎓",
            ephemeral=True,
        )

        # Actualizar el contador en el embed de reglas
        await self.bot.update_rules_counter()


class AcceptRulesView(discord.ui.View):
    def __init__(self, config: Config, bot: "CalendarWatcherBot"):
        super().__init__(timeout=None)
        self.config = config
        self.bot = bot

    @discord.ui.button(
        label="✅ Aceptar reglas",
        style=discord.ButtonStyle.green,
        custom_id="accept_rules",
    )
    async def accept_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "No se pudo determinar tu usuario.", ephemeral=True
            )
            return

        # Si ya está verificado, informar
        if is_student_verified(member.id):
            students = load_students()
            data = students.get(str(member.id), {})
            await interaction.response.send_message(
                f"Ya estás verificado/a como **{data.get('name', '?')}** "
                f"(Legajo: `{data.get('legajo', '?')}`). ✅\n"
                f"Si necesitás actualizar tus datos, contactá a un administrador.",
                ephemeral=True,
            )
            return

        # Mostrar modal
        modal = StudentInfoModal(self.config, self.bot)
        await interaction.response.send_modal(modal)


# ---------------------------------------------------------------------------
# Sede: Select + View
# ---------------------------------------------------------------------------

class SedeSelect(discord.ui.Select):
    def __init__(self, config: Config):
        self.config = config
        self.sede_roles = config.sede_roles
        options = [
            discord.SelectOption(label=name, value=str(role_id))
            for name, role_id in config.sede_roles.items()
        ]
        super().__init__(
            custom_id="sede_selector",
            placeholder="Elegí tu sede...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "No se pudo determinar tu usuario.", ephemeral=True
            )
            return

        # Verificar que sea estudiante verificado
        if not is_student_verified(member.id):
            await interaction.response.send_message(
                "❌ Primero tenés que aceptar las reglas y verificar tu identidad "
                "en el canal de reglas para poder elegir sede.",
                ephemeral=True,
            )
            return

        selected_role_id = int(self.values[0])
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Este comando solo funciona en un servidor.", ephemeral=True
            )
            return

        all_sede_role_ids = set(self.sede_roles.values())

        # Quitar todos los roles de sede que tenga actualmente
        roles_to_remove = [
            role for role in member.roles
            if role.id in all_sede_role_ids and role.id != selected_role_id
        ]
        for role in roles_to_remove:
            try:
                await member.remove_roles(role, reason="Cambio de sede")
            except discord.HTTPException as exc:
                logger.warning("No se pudo quitar rol %s a %s: %s", role.name, member, exc)

        # Asignar el nuevo rol de sede
        new_role = guild.get_role(selected_role_id)
        if new_role is None:
            await interaction.response.send_message(
                "No se encontró el rol de esa sede.", ephemeral=True
            )
            return

        # Verificar si ya tiene el rol seleccionado
        if new_role in member.roles:
            await interaction.response.send_message(
                f"Ya tenés la sede **{new_role.name}** asignada. ✅",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(new_role, reason="Selección de sede")
        except discord.HTTPException as exc:
            logger.warning("No se pudo asignar rol %s a %s: %s", new_role.name, member, exc)
            await interaction.response.send_message(
                "Hubo un error al asignarte la sede. Intentá de nuevo.", ephemeral=True
            )
            return

        sede_name = next(
            (name for name, rid in self.sede_roles.items() if rid == selected_role_id),
            new_role.name,
        )
        await interaction.response.send_message(
            f"¡Listo! Tu sede fue cambiada a **{sede_name}**. 🏫",
            ephemeral=True,
        )


class SedeSelectView(discord.ui.View):
    def __init__(self, config: Config):
        super().__init__(timeout=None)
        self.add_item(SedeSelect(config))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

async def clear_channel(channel: discord.TextChannel) -> None:
    messages = [message async for message in channel.history(limit=None)]
    if not messages:
        return

    for message in messages:
        try:
            await message.delete()
        except discord.HTTPException as exc:
            logger.warning("No se pudo borrar mensaje %s: %s", message.id, exc)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class CalendarWatcherBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.members = True
        super().__init__(intents=intents)

        self.config = config
        self.tree = discord.app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None
        self.poll_task: asyncio.Task | None = None
        self.verify_task: asyncio.Task | None = None

    def _register_commands(self) -> None:
        @self.tree.command(name="verlegajo", description="Consulta el legajo y datos de un estudiante (solo Administradores).")
        @discord.app_commands.describe(usuario="El miembro del servidor que querés consultar")
        @discord.app_commands.checks.has_permissions(administrator=True)
        async def verlegajo(interaction: discord.Interaction, usuario: discord.Member) -> None:
            user_key = str(usuario.id)
            students = load_students()

            if user_key in students:
                data = students[user_key]
                name = data.get("name", "Desconocido")
                legajo = data.get("legajo", "Desconocido")
                verified_at_raw = data.get("verified_at", "")

                formatted_date = "-"
                if verified_at_raw:
                    try:
                        dt = datetime.fromisoformat(verified_at_raw)
                        formatted_date = dt.strftime("%d/%m/%Y %H:%M")
                    except ValueError:
                        formatted_date = verified_at_raw

                embed = discord.Embed(
                    title="🪪  Información de Verificación",
                    color=safe_embed_color(self.config.embed_color_hex),
                )
                embed.add_field(name="👤 Usuario", value=f"{usuario.mention} (`{usuario.id}`)", inline=False)
                embed.add_field(name="📝 Nombre completo", value=f"`{name}`", inline=True)
                embed.add_field(name="🎓 Número de legajo", value=f"`{legajo}`", inline=True)
                embed.add_field(name="📅 Fecha de verificación", value=f"`{formatted_date}`", inline=False)
                embed.set_footer(text="⎯⎯  Centro de Estudiantes Codes++  •  Consulta de Administrador  ⎯⎯")

                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                embed = discord.Embed(
                    title="⚠️  Alumno No Verificado",
                    description=f"El usuario {usuario.mention} no se encuentra registrado en la base de datos de verificaciones.",
                    color=discord.Color.gold(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)

        @verlegajo.error
        async def verlegajo_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
            if isinstance(error, discord.app_commands.MissingPermissions):
                await interaction.response.send_message(
                    "❌ No tenés permisos de Administrador para usar este comando.",
                    ephemeral=True,
                )
            else:
                logger.error("Error en comando /verlegajo: %s", error)
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Ocurrió un error al procesar la solicitud.",
                        ephemeral=True,
                    )

    async def setup_hook(self) -> None:
        # Registrar vistas persistentes para que sobrevivan reinicios del bot
        self.add_view(AcceptRulesView(self.config, self))
        self.add_view(SedeSelectView(self.config))

        # Registrar y sincronizar comandos slash
        self._register_commands()
        try:
            await self.tree.sync()
            logger.info("Comandos Slash sincronizados correctamente.")
        except Exception as exc:
            logger.exception("Error al sincronizar comandos Slash: %s", exc)

        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self.poll_task = asyncio.create_task(self.poll_loop())
        self.verify_task = asyncio.create_task(self.verify_members_loop())

    async def close(self) -> None:
        for task in (self.poll_task, self.verify_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Bot conectado como %s", self.user)
        await self.post_verification_info()
        await self.post_rules_embed()
        await self.post_sede_selector()

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        student_role_id = self.config.student_role_id
        had_student_role = any(r.id == student_role_id for r in before.roles)
        has_student_role = any(r.id == student_role_id for r in after.roles)

        # Si perdió el rol de estudiante y tiene un apodo asignado, se lo sacamos
        if had_student_role and not has_student_role:
            if after.nick is not None:
                try:
                    await after.edit(nick=None, reason="Se le quitó el rol de estudiante")
                    logger.info("Nick de %s reseteado a su apodo original tras perder rol de estudiante.", after)
                except discord.HTTPException as exc:
                    logger.warning("No se pudo resetear el nick de %s: %s", after, exc)

    # -- Endpoint URL builder --

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

    # -- Post updates --

    async def post_update(self, payload: dict[str, Any]) -> None:
        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.channel_id)

        if not isinstance(channel, discord.TextChannel):
            raise ValueError("El CHANNEL_ID no corresponde a un canal de texto")

        await clear_channel(channel)

        embed = build_events_embed(self.config, payload)
        await channel.send(embed=embed)

    # -- Verification info embed --

    async def post_verification_info(self) -> None:
        """Publica el embed informativo de verificación garantizando que sea el primer mensaje."""
        channel = self.get_channel(self.config.verify_info_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.verify_info_channel_id)
            except discord.HTTPException:
                logger.error("No se pudo acceder al canal de verificación (VERIFY_INFO_CHANNEL_ID).")
                return

        if not isinstance(channel, discord.TextChannel):
            logger.error("VERIFY_INFO_CHANNEL_ID no corresponde a un canal de texto.")
            return

        messages = [msg async for msg in channel.history(limit=50, oldest_first=True)]
        needs_repost = True

        if messages:
            first_msg = messages[0]
            if first_msg.author == self.user and first_msg.embeds:
                # Ya es el primer mensaje, solo lo editamos para mantenerlo actualizado
                embed = build_verification_info_embed(self.config)
                await first_msg.edit(embed=embed)
                logger.info("Embed de verificación actualizado (ya era el primer mensaje).")
                needs_repost = False

        if needs_repost:
            await clear_channel(channel)
            embed = build_verification_info_embed(self.config)
            await channel.send(embed=embed)
            logger.info("Canal limpiado y embed de verificación publicado como primer mensaje.")

    # -- Rules embed --

    async def post_rules_embed(self) -> None:
        """Publica el embed de reglas con botón de aceptación."""
        channel = self.get_channel(self.config.rules_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.rules_channel_id)
            except discord.HTTPException:
                logger.error("No se pudo acceder al canal de reglas (RULES_CHANNEL_ID).")
                return

        if not isinstance(channel, discord.TextChannel):
            logger.error("RULES_CHANNEL_ID no corresponde a un canal de texto.")
            return

        # Verificar si ya existe un mensaje con el botón para no duplicar
        async for message in channel.history(limit=10):
            if message.author == self.user and message.components:
                logger.info("El embed de reglas ya existe en el canal. No se re-publica.")
                return

        await clear_channel(channel)
        embed = build_rules_embed(self.config)
        view = AcceptRulesView(self.config, self)
        await channel.send(embed=embed, view=view)
        logger.info("Embed de reglas publicado.")

    async def update_rules_counter(self) -> None:
        """Edita el embed de reglas para actualizar el contador de aceptaciones."""
        channel = self.get_channel(self.config.rules_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.rules_channel_id)
            except discord.HTTPException:
                return

        if not isinstance(channel, discord.TextChannel):
            return

        async for message in channel.history(limit=10):
            if message.author == self.user and message.components:
                embed = build_rules_embed(self.config)
                await message.edit(embed=embed)
                return

    # -- Sede selector --

    async def post_sede_selector(self) -> None:
        """Publica (o re-publica) el embed de selección de sede con dropdown."""
        channel = self.get_channel(self.config.sede_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.sede_channel_id)
            except discord.HTTPException:
                logger.error("No se pudo acceder al canal de sedes (SEDE_CHANNEL_ID).")
                return

        if not isinstance(channel, discord.TextChannel):
            logger.error("SEDE_CHANNEL_ID no corresponde a un canal de texto.")
            return

        # Verificar si ya existe un mensaje con el dropdown para no duplicar
        async for message in channel.history(limit=10):
            if message.author == self.user and message.components:
                logger.info("El embed de sedes ya existe en el canal. No se re-publica.")
                return

        await clear_channel(channel)
        embed = build_sede_embed(self.config)
        view = SedeSelectView(self.config)
        await channel.send(embed=embed, view=view)
        logger.info("Embed de selección de sede publicado.")

    # -- Verification loop --

    async def verify_members_loop(self) -> None:
        """Verifica periódicamente la integridad de roles cada 1 hora."""
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                await self.run_verification()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Error durante la verificación de miembros: %s", exc)

            await asyncio.sleep(3600)  # Cada 1 hora

    async def run_verification(self) -> None:
        students = load_students()
        all_sede_role_ids = set(self.config.sede_roles.values())
        student_role_id = self.config.student_role_id

        for guild in self.guilds:
            student_role = guild.get_role(student_role_id)
            if student_role is None:
                logger.warning("No se encontró el rol de estudiante en el server %s", guild.name)
                continue

            async for member in guild.fetch_members(limit=None):
                if member.bot:
                    continue

                user_key = str(member.id)
                has_student_role = student_role in member.roles
                is_verified = user_key in students

                # Tiene rol de estudiante pero no está verificado → quitar rol
                if has_student_role and not is_verified:
                    try:
                        await member.remove_roles(student_role, reason="No verificado en students.json")
                        logger.info("Rol estudiante removido de %s (no verificado)", member)
                    except discord.HTTPException as exc:
                        logger.warning("No se pudo quitar rol estudiante a %s: %s", member, exc)

                # Está verificado y tiene rol → asegurar que el nick sea correcto
                if has_student_role and is_verified:
                    expected_name = students[user_key].get("name", "")
                    expected_nick = fit_nickname(expected_name) if expected_name else ""
                    if expected_nick and member.nick != expected_nick:
                        try:
                            await member.edit(nick=expected_nick, reason="Enforcement de nombre real")
                            logger.info("Nick de %s corregido a '%s'", member, expected_nick)
                        except discord.HTTPException as exc:
                            logger.warning("No se pudo corregir nick de %s: %s", member, exc)

                # Tiene rol de sede pero no tiene rol de estudiante → quitar sede
                member_sede_roles = [r for r in member.roles if r.id in all_sede_role_ids]
                if member_sede_roles and not has_student_role:
                    for sede_role in member_sede_roles:
                        try:
                            await member.remove_roles(sede_role, reason="No tiene rol de estudiante")
                            logger.info("Rol sede '%s' removido de %s (sin rol estudiante)", sede_role.name, member)
                        except discord.HTTPException as exc:
                            logger.warning("No se pudo quitar rol sede a %s: %s", member, exc)

            logger.info("Verificación de miembros completada para server '%s'", guild.name)

    # -- Polling loop --

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
    cfg = Config.load()
    bot = CalendarWatcherBot(cfg)
    bot.run(cfg.discord_token)
