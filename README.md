# CODES Discord Bot (Python)

Bot de Discord que consulta un endpoint de eventos cada 12 horas (configurable), detecta cambios y cuando hay actualizacion:

- Borra todos los mensajes del canal configurado.
- Publica un embed con color, logo y miniatura configurables.

## Requisitos

- Python 3.10+
- Un bot de Discord agregado a tu servidor con permisos:
  - Ver canal
  - Leer historial de mensajes
  - Administrar mensajes
  - Enviar mensajes
  - Insertar enlaces

## Instalacion

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuracion

1. Copiar `.env.example` a `.env`
2. Completar las variables

Variables importantes:

- `DISCORD_TOKEN`: token del bot.
- `CHANNEL_ID`: id del canal de texto donde se publicara.
- `ENDPOINT_URL`: URL del endpoint GET.
- `POLL_INTERVAL_HOURS`: cada cuantas horas consultar (default `12`).
- `EMBED_COLOR_HEX`: color del embed en formato `#RRGGBB`.
- `EMBED_LOGO_URL`: logo para autor/imagen del embed.
- `EMBED_THUMBNAIL_URL`: miniatura (por ejemplo, la de CODES).

Comportamiento de publicacion:

- Al iniciar, el bot siempre borra el canal configurado y publica el estado actual del endpoint.
- Luego, vuelve a publicar solo cuando detecta cambios en el endpoint.

Si el endpoint corre en local (por ejemplo `localhost:5173`), usa `http://` y no `https://` para evitar errores SSL como `WRONG_VERSION_NUMBER`.

## Ejecutar

```bash
python bot.py
```

## Como detecta cambios

- El bot guarda un hash SHA-256 de la respuesta JSON en `STATE_FILE` (default `bot_state.json`).
- Si el hash cambia respecto del ultimo guardado, considera que hubo actualizacion y publica nuevamente.

## Nota sobre el endpoint

Tu endpoint acepta `limit` entre `1` y `20`. El bot envia `?limit=ENDPOINT_LIMIT` en cada consulta.
