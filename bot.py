import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp
import discord
from dotenv import load_dotenv


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("codes-discord-bot")

BASE_DIR = Path(__file__).parent.resolve()


def resolve_path(p: Path | str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    if path.exists():
        return path
    return BASE_DIR / path


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
    sede_announcement_channels: dict[str, int]
    unlu_api_url: str
    endpoint_url: str
    endpoint_limit: int
    request_timeout_seconds: int
    poll_interval_hours: float
    send_on_start: bool
    state_file: Path
    embed_color_hex: str
    examenes_planes_file: Path


    @staticmethod
    def load() -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("Falta DISCORD_TOKEN en variables de entorno (o archivo .env)")

        config_path = resolve_path("config.json")
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

        raw_ann_channels = data.get("SEDE_ANNOUNCEMENT_CHANNELS", {})
        sede_announcement_channels = {name: int(ch_id) for name, ch_id in raw_ann_channels.items()}

        unlu_api_url = str(data.get("UNLU_API_URL", "")).strip().rstrip("/")

        return Config(
            discord_token=token,
            channel_id=int(channel_raw),
            sede_channel_id=int(sede_channel_raw),
            rules_channel_id=int(rules_channel_raw),
            verify_info_channel_id=int(verify_info_raw),
            student_role_id=int(student_role_raw),
            sede_roles=sede_roles,
            sede_announcement_channels=sede_announcement_channels,
            unlu_api_url=unlu_api_url,
            endpoint_url=endpoint_url,
            endpoint_limit=max(1, min(int(data.get("ENDPOINT_LIMIT", 5)), 20)),
            request_timeout_seconds=max(5, int(data.get("REQUEST_TIMEOUT_SECONDS", 20))),
            poll_interval_hours=max(0.1, float(data.get("POLL_INTERVAL_HOURS", 12.0))),
            send_on_start=bool(data.get("SEND_ON_START", False)),
            state_file=resolve_path(data.get("STATE_FILE", "bot_state.json")),
            embed_color_hex=str(data.get("EMBED_COLOR_HEX", "#1F8B4C")).strip(),
            examenes_planes_file=resolve_path(data.get("EXAMENES_PLANES_FILE", "planes_estudio.json")),
        )


# ---------------------------------------------------------------------------
# Constants: Exam sede mapping
# ---------------------------------------------------------------------------

# Maps API centroRegional values to config sede names.
# None means the exam is available for all sedes (distance / virtual).
SEDE_MAPPING: dict[str, str | None] = {
    "SEDE LUJAN": "Luján",
    "C.R. CHIVILCOY": "Chivilcoy",
    "C.R. SAN MIGUEL": "San Miguel",
    "A DISTANCIA": None,
    "AULA VIRTUAL": None,
}

ART_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


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


# ---------------------------------------------------------------------------
# Planes de estudio / Exámenes helpers
# ---------------------------------------------------------------------------

@dataclass
class ExamDate:
    """A single exam date entry from the API."""
    materia_codigo: str
    materia_nombre: str
    fecha: str            # dd-mm-yyyy
    horario: str
    centro_regional: str
    fecha_limite: str     # yyyy-mm-dd
    plan_tags: list[str]  # e.g. ["17.14", "17.13"]
    is_virtual: bool      # True for A DISTANCIA / AULA VIRTUAL


def load_planes_codigos(planes_file: Path) -> dict[str, list[str]]:
    """Load planes_estudio.json and return {codigo: [plan_ids]} for all subjects with a code.

    Returns a dict mapping subject code to the list of plans it belongs to.
    """
    resolved_file = resolve_path(planes_file)
    if not resolved_file.exists():
        logger.warning("[Exámenes] No se encontró %s (buscado en %s). No se consultarán exámenes.", planes_file, resolved_file)
        return {}

    try:
        data = json.loads(resolved_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("[Exámenes] Error al leer %s: %s", resolved_file, exc)
        return {}

    codigos: dict[str, list[str]] = {}
    for plan in data.get("planes", []):
        plan_id = plan.get("plan", "?")
        for materia in plan.get("materias", []):
            codigo = materia.get("codigo")
            if codigo is not None:
                codigos.setdefault(codigo, [])
                if plan_id not in codigos[codigo]:
                    codigos[codigo].append(plan_id)

    logger.info("Cargados %d códigos de materia desde %s", len(codigos), planes_file)
    return codigos


async def fetch_examenes_for_codigo(
    session: aiohttp.ClientSession,
    base_url: str,
    codigo: str,
    plan_tags: list[str],
) -> list[ExamDate]:
    """Fetch exam dates for a single subject code from the UNLu API."""
    url = f"{base_url}/api/examenes-finales/{codigo}"
    logger.debug("[Exámenes] Consultando API: %s", url)
    try:
        async with session.get(url) as resp:
            logger.debug("[Exámenes] Respuesta HTTP %d para código %s", resp.status, codigo)
            if resp.status != 200:
                logger.warning("[Exámenes] API respondió %d para código %s", resp.status, codigo)
                return []
            data = await resp.json()
    except Exception as exc:
        logger.warning("[Exámenes] Error consultando exámenes para %s: %s", codigo, exc)
        return []

    if not data.get("exitoso"):
        logger.info("[Exámenes] API respondió exitoso=false para código %s. Respuesta: %s", codigo, data)
        return []

    nombre_materia = str(data.get("nombreMateria", codigo)).strip()
    fechas_raw = data.get("fechas", [])
    logger.info(
        "[Exámenes] Código %s (%s): %d fecha(s) obtenidas de la API.",
        codigo, nombre_materia, len(fechas_raw),
    )
    results: list[ExamDate] = []

    for f in fechas_raw:
        centro = str(f.get("centroRegional", "")).strip().upper()
        is_virtual = SEDE_MAPPING.get(centro) is None and centro in {"A DISTANCIA", "AULA VIRTUAL"}
        exam = ExamDate(
            materia_codigo=codigo,
            materia_nombre=nombre_materia,
            fecha=str(f.get("fecha", "")),
            horario=str(f.get("horario", "")),
            centro_regional=centro,
            fecha_limite=str(f.get("fechaLimite", "")),
            plan_tags=plan_tags,
            is_virtual=is_virtual,
        )
        logger.debug(
            "[Exámenes]   -> %s | fecha=%s | horario=%s | centro=%s | virtual=%s | límite=%s",
            nombre_materia, exam.fecha, exam.horario, centro, is_virtual, exam.fecha_limite,
        )
        results.append(exam)

    return results


def parse_exam_fecha(fecha_str: str) -> datetime | None:
    """Parse dd-mm-yyyy exam date."""
    try:
        return datetime.strptime(fecha_str, "%d-%m-%Y")
    except ValueError:
        return None


def parse_fecha_limite(fl_str: str) -> datetime | None:
    """Parse yyyy-mm-dd deadline."""
    try:
        return datetime.strptime(fl_str, "%Y-%m-%d")
    except ValueError:
        return None


def classify_exams_by_sede(
    all_exams: list[ExamDate],
    sede_names: list[str],
) -> dict[str, dict[str, list[ExamDate]]]:
    """Group exams by sede and classify into 'abierta' / 'proxima'.

    Returns: {sede_name: {"abierta": [...], "proxima": [...]}}

    - abierta: inscription deadline hasn't passed yet (fechaLimite >= today)
    - proxima: inscription closed but exam date hasn't passed (fecha >= today)
    - past exams (fecha < today) are filtered out
    """
    today = datetime.now(ART_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_naive = today.replace(tzinfo=None)
    logger.info(
        "[Exámenes] Clasificando %d exámenes para sedes: %s (fecha de hoy: %s)",
        len(all_exams), sede_names, today_naive.strftime("%d/%m/%Y"),
    )

    grouped: dict[str, dict[str, list[ExamDate]]] = {
        sede: {"abierta": [], "proxima": []} for sede in sede_names
    }

    skipped_past = 0
    skipped_unknown = 0

    for exam in all_exams:
        exam_date = parse_exam_fecha(exam.fecha)
        deadline = parse_fecha_limite(exam.fecha_limite)

        # Skip past exams
        if exam_date is not None and exam_date < today_naive:
            skipped_past += 1
            logger.debug(
                "[Exámenes] Examen pasado (filtrado): %s — fecha=%s",
                exam.materia_nombre, exam.fecha,
            )
            continue

        # Determine status
        if deadline is not None and deadline >= today_naive:
            status = "abierta"
        else:
            status = "proxima"

        # Determine which sedes get this exam
        mapped_sede = SEDE_MAPPING.get(exam.centro_regional)

        if mapped_sede is not None and mapped_sede in grouped:
            # Specific sede exam
            grouped[mapped_sede][status].append(exam)
            logger.debug(
                "[Exámenes] %s -> sede=%s, status=%s",
                exam.materia_nombre, mapped_sede, status,
            )
        elif exam.is_virtual:
            # Virtual / distance → goes to all sedes
            for sede in sede_names:
                grouped[sede][status].append(exam)
            logger.debug(
                "[Exámenes] %s -> TODAS las sedes (virtual), status=%s",
                exam.materia_nombre, status,
            )
        else:
            # Unknown centro_regional — log and skip
            skipped_unknown += 1
            logger.warning(
                "[Exámenes] Centro regional desconocido (descartado): '%s' para materia %s",
                exam.centro_regional, exam.materia_nombre,
            )

    # Sort each list by exam date
    for sede in grouped:
        for status in ("abierta", "proxima"):
            grouped[sede][status].sort(
                key=lambda e: parse_exam_fecha(e.fecha) or datetime.max
            )

    # Log resumen por sede
    logger.info(
        "[Exámenes] Clasificación completada. Exámenes pasados descartados: %d. Centro regional desconocido: %d.",
        skipped_past, skipped_unknown,
    )
    for sede in sede_names:
        abierta_count = len(grouped[sede]["abierta"])
        proxima_count = len(grouped[sede]["proxima"])
        logger.info(
            "[Exámenes]   Sede %s: %d abierta(s), %d próxima(s)",
            sede, abierta_count, proxima_count,
        )

    return grouped


def _format_plan_badge(plan_tags: list[str]) -> str:
    """Format plan tags as a compact badge string."""
    if len(plan_tags) == 1:
        return f"[Plan {plan_tags[0]}]"
    return "[Ambos planes]"


def build_examenes_embeds(
    config: "Config",
    sede_name: str,
    exams_by_status: dict[str, list[ExamDate]],
) -> list[discord.Embed]:
    """Build paginated embeds for a sede's exam dates.

    Discord embeds have a 6000 char total limit.  We paginate if needed.
    """
    now_str = datetime.now(ART_TZ).strftime("%d/%m/%Y %H:%M")
    color = safe_embed_color(config.embed_color_hex)
    footer_text = f"⎯⎯  Centro de Estudiantes Codes++  •  Actualizado: {now_str}  ⎯⎯"

    abierta = exams_by_status.get("abierta", [])
    proxima = exams_by_status.get("proxima", [])

    logger.info(
        "[Exámenes] Construyendo embeds para sede '%s': %d abierta(s), %d próxima(s).",
        sede_name, len(abierta), len(proxima),
    )

    if not abierta and not proxima:
        logger.info("[Exámenes] Sin exámenes vigentes para sede '%s'. Se genera embed vacío.", sede_name)
        embed = discord.Embed(
            title=f"📋  Fechas de Finales — {sede_name}",
            description="\n> *No hay fechas de exámenes finales vigentes.*\n",
            color=color,
        )
        embed.set_footer(text=footer_text)
        return [embed]

    # Build content lines for both sections
    sections: list[str] = []

    if abierta:
        sections.append("## 🟢  Inscripción abierta\n")
        for exam in abierta:
            icon = "🌐 " if exam.is_virtual else ""
            plan_badge = _format_plan_badge(exam.plan_tags)
            dl = parse_fecha_limite(exam.fecha_limite)
            dl_str = dl.strftime("%d/%m/%Y") if dl else exam.fecha_limite
            sections.append(
                f"> {icon}**{exam.materia_nombre}** {plan_badge}\n"
                f"> 📅 `{exam.fecha}` a las `{exam.horario}` — "
                f"Inscripción hasta `{dl_str}`\n"
                f"> 📍 _{exam.centro_regional}_\n"
            )

    if proxima:
        sections.append("## 🟡  Próximamente (inscripción cerrada)\n")
        for exam in proxima:
            icon = "🌐 " if exam.is_virtual else ""
            plan_badge = _format_plan_badge(exam.plan_tags)
            sections.append(
                f"> {icon}**{exam.materia_nombre}** {plan_badge}\n"
                f"> 📅 `{exam.fecha}` a las `{exam.horario}`\n"
                f"> 📍 _{exam.centro_regional}_\n"
            )

    # Paginate: Discord embeds have a 4096 description limit
    MAX_DESC = 3900  # leave some margin
    pages: list[str] = []
    current_page: list[str] = []
    current_len = 0

    for section in sections:
        if current_len + len(section) > MAX_DESC and current_page:
            pages.append("\n".join(current_page))
            current_page = []
            current_len = 0
        current_page.append(section)
        current_len += len(section)

    if current_page:
        pages.append("\n".join(current_page))

    embeds: list[discord.Embed] = []
    total_pages = len(pages)
    logger.info("[Exámenes] Sede '%s': %d página(s) de embed generadas.", sede_name, total_pages)

    for i, page_content in enumerate(pages):
        title = f"📋  Fechas de Finales — {sede_name}"
        if total_pages > 1:
            title += f" ({i + 1}/{total_pages})"

        embed = discord.Embed(
            title=title,
            description=page_content,
            color=color,
        )
        if i == total_pages - 1:
            embed.set_footer(text=footer_text)
        embeds.append(embed)

    return embeds


# ---------------------------------------------------------------------------
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
        "> Se recomienda usar tu **nombre real** para facilitar la comunicación",
        "> académica, pero hacerlo es solo una sugerencia.",
        "> El servidor cuenta con mecanismos de seguridad para actuar si alguien hace un mal uso de la comunidad. 🔥",
        "",
        "*El desconocimiento de estas reglas no exime de su cumplimiento.*",
    ]

    embed.description = "\n".join(lines)
    embed.set_footer(text="⎯⎯  Centro de Estudiantes Codes++  •  Licenciatura en Sistemas  ⎯⎯")
    return embed


def build_verification_info_embed(config: Config) -> discord.Embed:
    embed = discord.Embed(
        title="🛡️  Información de ingreso — Codes++",
        color=safe_embed_color(config.embed_color_hex),
    )

    embed.description = "\n".join([
        "Para ingresar al servidor, seguí las instrucciones del bot **Double Counter**",
        "y completá la verificación por IP. Esto ayuda a mantener segura la comunidad.",
        "",
        "### 🏫  Elegir tu sede",
        "> Después de verificarte, andá al canal de sedes y elegí tu sede de cursada",
        "> en el menú desplegable. Solo podés tener una sede asignada a la vez.",
        "",
        "### 🪪  Nombre en Discord",
        "> Se recomienda colocar tu **nombre real** para facilitar la comunicación,",
        "> pero es solo una sugerencia. La verificación por IP ayuda a mantener",
        "> segura la comunidad si alguien hace un mal uso del servidor. 🔥",
    ])
    embed.set_footer(text="⎯⎯  Centro de Estudiantes Codes++  •  Licenciatura en Sistemas  ⎯⎯")
    return embed


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
        self.examenes_task: asyncio.Task | None = None

    def _register_commands(self) -> None:
        @self.tree.command(name="testexamenes", description="Ejecuta manualmente la actualización de fechas de exámenes finales (solo Administradores).")
        @discord.app_commands.checks.has_permissions(administrator=True)
        async def testexamenes(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                await self.post_examenes_update()
                await interaction.followup.send(
                    "✅ Actualización de exámenes finales ejecutada correctamente. "
                    "Revisá los canales de anuncios por sede.",
                    ephemeral=True,
                )
            except Exception as exc:
                logger.exception("Error en /testexamenes: %s", exc)
                await interaction.followup.send(
                    f"❌ Error al ejecutar la actualización: {exc}",
                    ephemeral=True,
                )

        @testexamenes.error
        async def testexamenes_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
            if isinstance(error, discord.app_commands.MissingPermissions):
                await interaction.response.send_message(
                    "❌ No tenés permisos de Administrador para usar este comando.",
                    ephemeral=True,
                )
            else:
                logger.error("Error en comando /testexamenes: %s", error)
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Ocurrió un error al procesar la solicitud.",
                        ephemeral=True,
                    )

    async def setup_hook(self) -> None:
        # Registrar la vista persistente de selección de sede.
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
        self.examenes_task = asyncio.create_task(self.examenes_loop())

    async def close(self) -> None:
        for task in (self.poll_task, self.examenes_task):
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
        """Edita el mensaje informativo existente o lo publica si falta."""
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

        embed = build_verification_info_embed(self.config)
        async for message in channel.history(limit=50, oldest_first=True):
            if message.author == self.user and message.embeds:
                await message.edit(embed=embed)
                logger.info("Texto de verificación actualizado mediante edición.")
                return

        await channel.send(embed=embed)
        logger.info("Texto de verificación publicado porque no existía.")

    # -- Rules embed --

    async def post_rules_embed(self) -> None:
        """Limpia el canal de reglas y publica las normas sin componentes."""
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

        await clear_channel(channel)
        embed = build_rules_embed(self.config)
        await channel.send(embed=embed)
        logger.info("Embed de reglas publicado.")

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

    # -- Exámenes finales loop --

    @staticmethod
    def _seconds_until_next_monday_7am() -> float:
        """Calculate seconds until the next Monday at 07:00 ART."""
        now = datetime.now(ART_TZ)
        # Find next Monday (weekday 0)
        days_ahead = (0 - now.weekday()) % 7  # 0 = Monday
        if days_ahead == 0:
            # If it's already Monday, check if 7 AM has passed
            target = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if now >= target:
                days_ahead = 7  # Next Monday
        else:
            target = now.replace(hour=7, minute=0, second=0, microsecond=0)

        target = (now + timedelta(days=days_ahead)).replace(
            hour=7, minute=0, second=0, microsecond=0
        )
        delta = (target - now).total_seconds()
        return max(0, delta)

    async def examenes_loop(self) -> None:
        """Weekly loop: every Monday at 07:00 ART, fetch and post exam dates per sede."""
        await self.wait_until_ready()

        while not self.is_closed():
            # Wait until next Monday 7:00 AM ART
            wait_seconds = self._seconds_until_next_monday_7am()
            logger.info(
                "Exámenes: próxima ejecución en %.1f horas (lunes 07:00 ART).",
                wait_seconds / 3600,
            )
            await asyncio.sleep(wait_seconds)

            try:
                await self.post_examenes_update()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Error durante el polling de exámenes: %s", exc)

            # Sleep at least 1 hour to avoid double-firing on the same Monday
            await asyncio.sleep(3600)

    async def post_examenes_update(self) -> None:
        """Fetch exam dates for all subjects and send embeds to sede channels."""
        logger.info("[Exámenes] === INICIO de post_examenes_update ===")

        if not self.session:
            logger.error("[Exámenes] Sesión HTTP no inicializada. Abortando.")
            return

        if not self.config.unlu_api_url:
            logger.warning("[Exámenes] UNLU_API_URL no configurada. Abortando.")
            return

        if not self.config.sede_announcement_channels:
            logger.warning("[Exámenes] No hay canales de anuncios configurados (SEDE_ANNOUNCEMENT_CHANNELS vacío). Abortando.")
            return

        logger.info(
            "[Exámenes] Config: UNLU_API_URL=%s | Canales de anuncio: %s",
            self.config.unlu_api_url,
            {k: v for k, v in self.config.sede_announcement_channels.items()},
        )

        # Load subject codes from planes_estudio.json
        codigos = load_planes_codigos(self.config.examenes_planes_file)
        if not codigos:
            logger.warning("[Exámenes] No se cargaron códigos de materia desde %s. Abortando.", self.config.examenes_planes_file)
            return

        # Fetch exam dates for all subjects (sequential with delay)
        logger.info("[Exámenes] Consultando exámenes para %d materias...", len(codigos))
        all_exams: list[ExamDate] = []

        for i, (codigo, plans) in enumerate(codigos.items()):
            logger.info("[Exámenes] [%d/%d] Consultando código %s (planes: %s)...", i + 1, len(codigos), codigo, plans)
            try:
                results = await fetch_examenes_for_codigo(
                    self.session, self.config.unlu_api_url, codigo, plans
                )
                logger.info("[Exámenes] [%d/%d] Código %s: %d resultado(s)", i + 1, len(codigos), codigo, len(results))
                all_exams.extend(results)
            except Exception as exc:
                logger.warning("[Exámenes] Error en fetch de examen %s: %s", codigo, exc)

            # Delay between requests (skip after the last one)
            if i < len(codigos) - 1:
                delay = random.uniform(15, 30)
                logger.debug("[Exámenes] Esperando %.1fs antes de la siguiente consulta...", delay)
                await asyncio.sleep(delay)

        logger.info("[Exámenes] === Recopilación finalizada: %d fechas totales ===", len(all_exams))

        # Classify by sede
        sede_names = list(self.config.sede_announcement_channels.keys())
        logger.info("[Exámenes] Clasificando exámenes por sede: %s", sede_names)
        grouped = classify_exams_by_sede(all_exams, sede_names)

        # Send embeds to each sede channel
        logger.info("[Exámenes] === Enviando embeds a los canales ===")
        for sede_name, channel_id in self.config.sede_announcement_channels.items():
            logger.info("[Exámenes] Procesando sede '%s' (canal ID: %d)...", sede_name, channel_id)

            channel = self.get_channel(channel_id)
            if channel is None:
                logger.info("[Exámenes] Canal %d no encontrado en caché, intentando fetch...", channel_id)
                try:
                    channel = await self.fetch_channel(channel_id)
                except discord.HTTPException as exc:
                    logger.error("[Exámenes] No se pudo acceder al canal de anuncios de %s (ID: %d): %s", sede_name, channel_id, exc)
                    continue

            if not isinstance(channel, discord.TextChannel):
                logger.error("[Exámenes] Canal de %s (ID: %d) no es TextChannel, es %s.", sede_name, channel_id, type(channel).__name__)
                continue

            logger.info("[Exámenes] Canal de %s resuelto: #%s (ID: %d)", sede_name, channel.name, channel.id)

            # Clear previous bot messages in the channel
            deleted_count = 0
            async for msg in channel.history(limit=50):
                if msg.author == self.user:
                    try:
                        await msg.delete()
                        deleted_count += 1
                    except discord.HTTPException as exc:
                        logger.warning("[Exámenes] No se pudo borrar mensaje %s en #%s: %s", msg.id, channel.name, exc)
            logger.info("[Exámenes] Borrados %d mensajes previos del bot en #%s.", deleted_count, channel.name)

            # Build and send embeds
            exams_data = grouped.get(sede_name, {"abierta": [], "proxima": []})
            embeds = build_examenes_embeds(self.config, sede_name, exams_data)

            logger.info("[Exámenes] Enviando %d embed(s) a #%s...", len(embeds), channel.name)
            for idx, embed in enumerate(embeds):
                try:
                    await channel.send(embed=embed)
                    logger.info("[Exámenes] Embed %d/%d enviado a #%s.", idx + 1, len(embeds), channel.name)
                except discord.HTTPException as exc:
                    logger.error("[Exámenes] ERROR al enviar embed %d/%d a #%s: %s", idx + 1, len(embeds), channel.name, exc)

            total = len(exams_data.get("abierta", [])) + len(exams_data.get("proxima", []))
            logger.info(
                "[Exámenes] ✅ Sede %s: %d embed(s) con %d fecha(s) enviados a #%s.",
                sede_name, len(embeds), total, channel.name,
            )

        logger.info("[Exámenes] === FIN de post_examenes_update ===")

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
