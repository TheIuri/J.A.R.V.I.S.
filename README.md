# J.A.R.V.I.S.

Asistente personal de voz construido por niveles, siguiendo la guía *VISION CERO – Construye tu propio JARVIS*.
Arquitectura **híbrida**: lo que se repite cientos de veces (oído, voz, memoria, scripts) corre en local en un
TrueNAS SCALE; el "cerebro" (LLM) usa capas gratuitas en la nube, con un modelo local como opción.

> Regla de la guía: cada nivel funciona antes de montar el siguiente.

## Estado

| Nivel | Qué | Estado |
|---|---|---|
| 1 · Voz | micro → STT → LLM → TTS → altavoz, push-to-talk | ✅ |
| 2 · Tools | registro de herramientas tipadas + permisos | ✅ |
| 3 · Memoria | SQLite (+ Qdrant cuando haga falta) | ✅ |
| 4 · Sentidos | wake word, cámara bajo demanda, event bus | pendiente |
| 5 · Agentes | orquestador + especialistas | pendiente |
| 6 · OS personal | gateway, clientes móvil/escritorio | pendiente |

## Arquitectura (Nivel 1)

```
 PC Windows / Android (cliente)                 TrueNAS SCALE (jarvis-core, Docker)
 ┌───────────────────────────┐   HTTP + token   ┌─────────────────────────────────────────┐
 │ micro ── push-to-talk ────┼─────── WAV ─────▶│ STT  faster-whisper (CPU, o Groq)        │
 │                           │                  │  │                                       │
 │ altavoz / Echo (Bluetooth)│◀── texto + WAV ──┤ LLM  Groq → Gemini (gratis, con fallback)│──▶ cloud
 └───────────────────────────┘                  │  │                                       │
                                                │ TTS  Piper (CPU)                         │
                                                └─────────────────────────────────────────┘
```

El cliente es solo micrófono y altavoz; el cerebro vive en el NAS (la guía lo llama "superficies, no cerebros
duplicados"). Cada proveedor está detrás de una interfaz y se elige por variables de entorno:

| Capa | Opciones | Por defecto |
|---|---|---|
| STT | `local` (faster-whisper), `groq` | `local`, modelo `small` en CPU |
| LLM | `groq`, `gemini`, `openrouter`, `ollama` (todos vía API compatible con OpenAI) | `groq,gemini` en cadena |
| TTS | `piper`, `none` | `piper`, voz `es_ES-davefx-medium` |

Cada respuesta incluye la latencia por etapa (`stt`, `llm`, `tts`, `total`), que también queda en los logs.

## Notas sobre el hardware (i5-10400, 24 GB, GTX 1050 Ti)

- **RAM**: los 16 GB de "ZFS Cache" (ARC) no están ocupados de verdad; ZFS los libera cuando las apps los necesitan.
  El contenedor tiene un límite de 6 GB.
- **CPU primero**: el compose viene configurado para CPU (`WHISPER_DEVICE: "cpu"`). Whisper `small` en el i5-10400
  tarda ~1–2 s por frase; `STT_PROVIDER: "groq"` es más rápido si no te importa que el audio salga a la nube.
- **GPU (opcional)**: la GTX 1050 Ti (Pascal, 4 GB) solo sirve si `nvidia-smi` funciona en TrueNAS. Si `dmesg` dice
  `already bound to vfio-pci`, la GPU está reservada para una VM (quítala de la VM y reinicia). Además, las
  versiones recientes de TrueNAS usan los drivers NVIDIA *open*, que no soportan GPUs Pascal, así que puede que no
  funcione aunque se libere. Si `nvidia-smi` la ve, descomenta el bloque de GPU del compose y pon
  `WHISPER_DEVICE: "auto"`.
- **Piper** va sobrado en la CPU.

## Instalación en TrueNAS

1. **GPU (opcional)**: ver notas de hardware; no hace falta para empezar.
2. **Dataset**: crea uno para los modelos, p. ej. `TU_POOL/apps/jarvis`, con propietario el usuario `apps` (568).
3. **API keys gratuitas**:
   - Groq: <https://console.groq.com/keys>
   - Gemini (opcional, de reserva): <https://aistudio.google.com/apikey>. Ojo: en la capa gratuita Google puede usar
     las conversaciones para mejorar sus productos.
4. **Imagen**: el workflow `.github/workflows/ci.yml` publica `ghcr.io/theiuri/jarvis-core:latest` en cada push a
   `main`. La primera vez, en GitHub → *Packages → jarvis-core → Package settings*, ponlo como **público** (o
   configura credenciales de GHCR en TrueNAS).
5. **App**: *Apps → Discover Apps → Custom App → Install via YAML* y pega
   [`deploy/truenas/docker-compose.yml`](deploy/truenas/docker-compose.yml) tras editar lo marcado con `<<<`.
6. **Comprobación**: abre `http://IP-DEL-NAS:8765/health`. El primer arranque tarda un par de minutos porque
   descarga Whisper y la voz de Piper en el dataset.

> Seguridad: el servidor exige `API_TOKEN` y no arranca sin él. Úsalo solo en tu red local o por VPN
> (Tailscale/WireGuard); no abras el puerto a internet.

## Cliente en Windows

```powershell
cd client
py -m pip install -r requirements.txt
$env:JARVIS_SERVER = "http://IP-DEL-NAS:8765"
$env:JARVIS_TOKEN  = "tu-token"
py jarvis_client.py
```

- **Enter** empieza a grabar; **Enter** otra vez lo envía. También puedes escribir texto.
- `/reset` reinicia la conversación y `/salir` termina.
- **Echo Dot como altavoz**: di "Alexa, empareja Bluetooth", vincúlalo desde Windows y elige su índice con
  `py jarvis_client.py --list-devices` y `--output-device N`. El Echo no sirve como micrófono para un asistente
  propio: el micro será el del PC (o el móvil más adelante).

## Nivel 2: herramientas

El LLM no ejecuta nada: **propone** una llamada a una tool registrada y el servidor la valida
(tipos, rangos, lista permitida), la ejecuta con tiempo límite y la registra en el log `jarvis.audit`
(tool, argumentos, resultado, duración). El resultado vuelve al LLM, que responde en lenguaje natural.

| Tool | Dónde se ejecuta | Qué hace |
|---|---|---|
| `get_datetime` | NAS | Fecha y hora (`TZ`) |
| `get_weather` | NAS | Tiempo actual y previsión con Open-Meteo (gratis, sin clave). Ciudad por defecto: `HOME_CITY` (o coordenadas fijas con `HOME_LATITUDE`/`HOME_LONGITUDE`) |
| `truenas_status` | NAS | Sistema, pools, apps y alertas del TrueNAS, en solo lectura |
| `pc_open_app` | PC | Abre una app de `client/apps.json` (y solo esas) |
| `pc_open_url` | PC | Abre una web `http(s)` en el navegador |
| `pc_volume` | PC | Fijar volumen (0–100), subir, bajar o silenciar |
| `pc_media` | PC | Play/pausa, siguiente, anterior |
| `pc_timer` | PC | Temporizador que avisa por voz |

Ejemplos: *"¿Qué tiempo hará mañana?"*, *"Pon el volumen al 30"*, *"Abre Spotify"*,
*"Avísame en 10 minutos de la pasta"*, *"¿Cómo está el NAS?"*.

**Seguridad**
- **Acciones del PC con doble control**: el servidor solo las valida y las devuelve en la respuesta. El cliente
  las ejecuta únicamente si están en su propia lista permitida; nunca ejecuta comandos arbitrarios.
- **Interruptores para apagarlo**: `TOOLS_ENABLED=false` apaga todas las tools, `TOOLS_DISABLED` desactiva tools
  concretas y el cliente se puede arrancar con `--no-actions`.
- **Nada destructivo por ahora**: las tools marcadas como destructivas no se ofrecen al LLM mientras no exista
  confirmación humana.
- **Temporizadores**: viven en el cliente del PC; si cierras el cliente, se pierden.

**Apps del PC**: edita `client/apps.json` (nombre hablado → programa, ruta o URI) y reinicia el cliente.

**Estado del TrueNAS (opcional)**
1. **Usuario de solo lectura**: en *Credentials → Users → Add*, crea un usuario `jarvis` con el rol
   *Read-Only Administrator*.
2. **API key**: en *Credentials → API Keys* (o desde el menú de usuario), crea una API key para ese usuario.
3. **Variables en el compose**:
   - `TRUENAS_URL: "wss://IP-DEL-NAS/api/current"`. Tiene que ser `wss://`: TrueNAS revoca las keys usadas
     sin TLS.
   - `TRUENAS_USER: "jarvis"`
   - `TRUENAS_API_KEY`
   - `TRUENAS_VERIFY_SSL` queda en `false` por defecto, porque TrueNAS usa un certificado autofirmado.

### Información (sin cuentas ni claves)

| Tool | Qué hace | Servicio |
|---|---|---|
| `web_search` | busca en internet (resultados, precios, horarios...) | DuckDuckGo, o Brave si pones `BRAVE_API_KEY` |
| `wikipedia` | resumen de un tema | Wikipedia en español |
| `news` | titulares de hoy, o sobre un tema | Google Noticias (RSS) |
| `convert` | unidades (longitud, peso, volumen, temperatura, velocidad, datos...) y divisas | local; divisas con el cambio del BCE (frankfurter.dev) |

Ejemplos: *"¿Cómo quedó ayer el Barça?"*, *"¿Quién fue Ramón y Cajal?"*, *"¿Qué noticias hay hoy?"*,
*"¿Cuántas millas son 42 kilómetros?"*, *"¿Cuánto son 50 dólares en euros?"*.

### Agenda (Google Calendar y Outlook)

JARVIS lee tus calendarios con su **dirección secreta iCal**, sin OAuth ni proyectos en la nube (solo lectura):

- **Google Calendar**: en la web, ⚙️ → Configuración → (tu calendario) → *Integrar el calendario* →
  **Dirección secreta en formato iCal**.
- **Outlook / Microsoft 365**: Outlook web → ⚙️ → Calendario → *Calendarios compartidos* → **Publicar un
  calendario** → permiso "Puede ver todos los detalles" → copia el enlace **ICS**.

```yaml
CALENDARS: "personal=https://calendar.google.com/calendar/ical/.../basic.ics;trabajo=https://outlook.office365.com/owa/calendar/.../calendar.ics"
```

Esos enlaces dan acceso a tu agenda: trátalos como una contraseña (solo en las variables de la app, nunca en el
repositorio). Ejemplos: *"¿Qué tengo mañana?"*, *"¿Qué tengo esta semana en el trabajo?"*.

### Wake-on-LAN

Enciende otro equipo de casa: *"Enciende el sobremesa"*.

```yaml
WOL_DEVICES: "sobremesa=AA:BB:CC:DD:EE:FF"   # la MAC de su tarjeta de red (varios: separados por ';')
# WOL_BROADCAST: "192.168.1.255"             # por defecto 255.255.255.255
```

En el equipo a encender, activa Wake-on-LAN en la BIOS y en Windows (Administrador de dispositivos → tarjeta de red →
*Administración de energía*: "Permitir que este dispositivo reactive el equipo"; y en *Opciones avanzadas*,
"Wake on Magic Packet"). El paquete lo envía el NAS y, si tienes el HUD del PC abierto, también el PC. Si desde el
móvil no enciende pero desde el PC sí, es la red interna de Docker del NAS, que no deja pasar el broadcast.

### Home Assistant

Si algún día lo instalas: *"¿Qué luces hay encendidas?"*, *"Apaga la luz del salón"*, *"Pon la calefacción a
21 grados"*, *"Baja la persiana del dormitorio"*.

```yaml
HA_URL: "http://IP-DE-HOME-ASSISTANT:8123"
HA_TOKEN: "..."            # HA → tu perfil → Seguridad → Tokens de acceso de larga duración
# HA_ENTITIES: "light.,switch.salon,climate."   # opcional: solo estas entidades (por prefijo)
```

Solo luces, enchufes, ventiladores, persianas, clima, multimedia, escenas y scripts. **Cerraduras y alarmas
quedan fuera** a propósito.

### Spotify

Elige qué suena: *"Pon Viva la vida de Coldplay"*, *"Pon música de Rosalía"*, *"Pon mi playlist de
entrenar"*, *"¿Qué canción es esta?"*, *"Sube el volumen de Spotify al 60"*. Controlar la reproducción exige
**Spotify Premium** y tener Spotify abierto en algún dispositivo (PC, móvil, altavoz...).

1. En [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), crea una app (marca *Web API*)
   con Redirect URI `http://127.0.0.1:8888/callback`.
2. En el PC: `cd client` y `py spotify_login.py`. Pide el Client ID y el Secret, abre el navegador para aceptar
   y te da el refresh token.
3. En las variables de la app de TrueNAS:

```yaml
SPOTIFY_CLIENT_ID: "..."
SPOTIFY_CLIENT_SECRET: "..."
SPOTIFY_REFRESH_TOKEN: "..."
# SPOTIFY_DEVICE: "SOBREMESA"   # dispositivo preferido si no hay nada sonando
```

### Acciones con confirmación

Lo que tiene consecuencias (por ahora, reiniciar una app de TrueNAS) nunca se hace a la primera: JARVIS
pregunta *"¿Confirmas que reinicie Plex?"* y **solo se ejecuta si tu siguiente frase es un "sí"** (sí, vale,
adelante, hazlo, confirmo...). Esa comprobación la hace el código, no la IA, así que ni el modelo ni un texto
leído de internet pueden saltársela. Cualquier otra respuesta, o esperar más de 2 minutos, la cancela.

## Nivel 3: memoria

JARVIS recuerda entre sesiones y reinicios. SQLite (`/data/memory.db` en el dataset) es la fuente de verdad.
Cada recuerdo guarda tipo (`hecho`, `preferencia`, `proyecto`, `decision`, `evento`), contenido, fechas,
origen y confianza.

- **Qué guarda**: lo que le pides que recuerde, o datos duraderos que cambian respuestas futuras. No guarda charla
  trivial ni duplicados.
- **Qué nunca guarda**: contraseñas, claves, tokens, tarjetas ni IBAN. Se rechazan aunque el LLM lo intente.
- **Qué usa en cada respuesta**: como máximo `MEMORY_MAX_ITEMS` (8) recuerdos. Las preferencias entran siempre;
  el resto, por palabras en común con lo que dices (búsqueda FTS5, sin distinguir acentos). El log `jarvis.memory`
  dice por qué eligió cada recuerdo.
- **Cómo lo gestiona JARVIS**: con las tools `memory_save`, `memory_search`, `memory_update` y `memory_forget`.
  "Olvida lo del perro" borra de verdad.
- **Cómo lo gestionas tú**: en el cliente, `/memoria` lista los recuerdos y `/olvida N` borra uno. También por API:
  `GET /api/memories` y `DELETE /api/memories/{id}`.
- **Cómo apagarla**: `MEMORY_ENABLED=false`. Para empezar de cero, borra `memory.db` del dataset.
- **Siguiente paso** (cuando haya muchos recuerdos o notas largas): búsqueda semántica con embeddings y Qdrant,
  detrás de la misma interfaz `Retriever`.

Ejemplos: *"Recuerda que mi perro se llama Toby"*, *"Prefiero que me llames Ori"*, *"¿Cómo se llama mi perro?"*,
*"Olvida lo del perro"*.

## Interfaz (HUD) en el PC

Una interfaz web con un **cerebro 3D de neuronas** que gira sobre sí mismo y muestra el flujo de pensamiento en directo. Incluye
subtítulos, historial, un panel de sesión (intercambios, latencia, pico, tiempo por etapa, modelo y tools usadas),
el estado de cada región del córtex y la lista de recuerdos con botón para olvidar.

**Flujo de pensamiento**: el servidor emite cada paso del turno según ocurre (`/api/chat/stream` y
`/api/voice/stream`, una línea JSON por evento). Cada paso enciende su región y un impulso viaja desde la anterior:

| Región | Se enciende cuando… |
|---|---|
| AUDITIVO | te escucha / transcribe lo que dices |
| HIPOCAMPO | consulta la memoria (y las tools `memory_*`) |
| PREFRONTAL | el modelo razona (cada ronda) |
| ASOCIACIÓN | consulta algo: tiempo, hora, Obsidian (buscar/leer), TrueNAS |
| CÓRTEX MOTOR | actúa: PC (apps, volumen, música, temporizador) y escribir en Obsidian |
| LENGUAJE | redacta la respuesta |
| CEREBELO | pone la voz |
| VISUAL | reservada para la cámara (Nivel 4) |

Debajo del cerebro queda el recorrido del turno (p. ej. `AUDITIVO → HIPOCAMPO → PREFRONTAL → ASOCIACIÓN → LENGUAJE`).
Si el servidor es de una versión anterior sin streaming, el HUD usa los endpoints de siempre.

```bash
cd client
py jarvis_hud.py        # abre http://localhost:8766 (usa JARVIS_SERVER y JARVIS_TOKEN)
```

- **Para hablar**: mantén pulsada la **barra espaciadora** o el cerebro. También puedes escribir abajo.
- **Cómo funciona**: `jarvis_hud.py` corre en tu PC, sirve la página y reenvía las peticiones al NAS.
  - El token **no llega al navegador**.
  - Las acciones del PC se ejecutan aquí, con `apps.json`. Los temporizadores avisan por voz también en el HUD.
  - Solo escucha en `127.0.0.1` y rechaza peticiones de otras webs (comprueba `Host` y `Origin`).
- **Micrófono**: el navegador pide permiso la primera vez. Funciona porque `localhost` cuenta como sitio seguro.
- **Opciones**: `--no-actions` (sin acciones en el PC), `--port 8766` y `--no-browser`.

### "Hey Jarvis" (Nivel 4)

El HUD del PC puede escuchar la palabra de activación, sin pulsar nada. La detección es 100 % local con
[openWakeWord](https://github.com/dscripka/openWakeWord): el audio no sale del PC hasta que oye "Hey Jarvis".

```bash
cd client
py -m pip install -r requirements-wake.txt   # una vez (descarga el modelo la primera vez que arranca)
py jarvis_hud.py
```

- **Cómo decirlo**: en inglés, *"jei YAR-vis"*. Pronunciado a la española ("ei jarvis", con la "j" de jamón)
  no lo reconoce.
- **Uso**: di "Hey Jarvis", espera el pitido o dilo todo seguido (*"Hey Jarvis, ¿qué tiempo hará mañana?"*).
  Graba hasta que te callas un segundo.
- **Mientras piensa o habla** no escucha la palabra, para no activarse con su propia voz.
- **Sonido**: el navegador no deja sonar nada hasta el primer clic en la página; el HUD te lo recuerda.
- **Opciones**:
  - `--wake-threshold 0.4` para hacerlo más sensible (por defecto `0.5`; más alto, menos falsos positivos).
  - `--wake-device N` para elegir otro micrófono (lista con `py jarvis_client.py --list-devices`).
  - `--no-wake` para desactivarlo.
- **En el móvil** no está disponible: el navegador no puede escuchar en segundo plano. Allí sigues pulsando para hablar.

### Avisos proactivos (Nivel 4)

JARVIS habla sin que le preguntes. Los avisos salen en el HUD (PC y móvil), se dicen en voz alta y encienden el
**TÁLAMO** del cerebro. Si llegan mientras está pensando o hablando, esperan a que termine. Solo habla la pestaña
visible, así que no suenan a la vez el PC y el móvil.

| Vigilante | Qué avisa | Cada |
|---|---|---|
| Recordatorios | *"Recuérdame a las 18:00 llamar a mamá"*, *"...dentro de 20 minutos"*, *"...mañana a las 9"* | 20 s |
| Agenda (`CALENDARS`) | tus citas, 15 minutos antes (`CALENDAR_REMIND_MINUTES`) | 1 min |
| TrueNAS (`TRUENAS_*`) | pool con problemas, disco a ≥ 50 °C (`DISK_TEMP_WARN`), app caída / recuperada, alertas nuevas, copias fallidas | 5 min |

- Cada aviso se da **una sola vez**, también tras reiniciar el contenedor.
- `NOTIFY_QUIET: "23:00-08:00"` hace que esas horas los avisos se vean pero no se digan, salvo los **críticos**.
- `NOTIFY_ENABLED: "false"` lo desactiva todo, recordatorios incluidos.

**En el móvil aunque el HUD esté cerrado** (opcional), con [ntfy](https://ntfy.sh), que es gratis:
1. Instala la app **ntfy** en el móvil y suscríbete a un tema con un nombre **largo y difícil de adivinar**,
   por ejemplo `jarvis-ori-7f3k9x2q`. En ntfy.sh, quien sepa el nombre del tema puede leerlo; si prefieres,
   instala ntfy como app en TrueNAS y usa tu propio servidor.
2. Añade `NTFY_URL: "https://ntfy.sh/jarvis-ori-7f3k9x2q"` a las variables de la app.

## Nivel 6: rutinas

**Buenos días**: di *"Buenos días"* o *"¿Qué tengo hoy?"* y te resume el día: fecha, tiempo, agenda y
recordatorios de hoy, un par de titulares y problemas del servidor si los hay. Para que te lo diga **solo, cada
mañana**, en el HUD y como push al móvil:

```yaml
BRIEFING_AT: "08:00"
# BRIEFING_WEEKENDS: "false"   # solo de lunes a viernes
```

Se da una vez al día. Si el servidor arranca más de 2 horas tarde, ese día se salta.

## Nivel 5: agentes

**Agente investigador**: *"Investiga qué placas solares me convienen para un piso"*. JARVIS responde al momento
que se pone con ello, y un agente trabaja en segundo plano:
1. Busca en internet, lee de 2 a 5 páginas y contrasta con Wikipedia o noticias. Hace como máximo 10 rondas.
2. Escribe un informe con resumen, ideas principales y fuentes, y lo guarda en Obsidian, en
   `JARVIS/Investigaciones/AAAA-MM-DD tema.md`.
3. Avisa al terminar: lo dice en el HUD, y llega al móvil si tienes ntfy.

*"¿Cómo va la investigación?"* te da el estado. Puede haber 2 investigaciones a la vez, y `AGENTS_ENABLED: "false"`
lo desactiva.

**Seguridad**: el agente **solo tiene herramientas de lectura** (buscar, leer páginas, Wikipedia, noticias). Si
una web intenta darle órdenes, no tiene con qué cumplirlas; el informe lo guarda el código, no la IA. El lector
de páginas no abre direcciones de tu red (router, NAS…) ni `localhost`, y comprueba también cada redirección.
Por eso `web_read` es solo del agente, no del asistente principal, que sí puede actuar (PC, casa, Spotify…).

## HUD en el móvil (HTTPS con Tailscale)

El NAS también sirve la interfaz en `/hud/`. Para que el micrófono del móvil funcione hace falta HTTPS, y lo pone
Tailscale: un contenedor dentro de la app de JARVIS crea un nodo **propio** llamado `jarvis` en tu tailnet y publica
`https://jarvis.<tu-tailnet>.ts.net` con un certificado válido. Solo lo ven tus dispositivos de Tailscale; no se abre
ningún puerto a internet y funciona también fuera de casa.

**No interfiere con otros Tailscale del NAS** (por ejemplo, el de otras apps):
- tiene su propio estado (`<dataset>/tailscale`) y su propio nombre e IP;
- usa el modo *userspace*, así que no crea interfaces ni toca las rutas del host;
- no anuncia subredes ni hace de *exit node*;
- el puerto 443 es el de su propio nodo, no el del NAS.

**Pasos**
1. **Ajustes de la tailnet** (en https://login.tailscale.com/admin/dns): activa **MagicDNS** y **HTTPS Certificates**
   si no lo estaban. Esto solo permite pedir certificados y no cambia tus otros nodos. Ten en cuenta que el nombre
   `jarvis.<tailnet>.ts.net` aparecerá en los registros públicos de certificados (Certificate Transparency).
2. **Clave de acceso**: en *Settings → Keys → Generate auth key*, genera una de un solo uso. Solo se usa en el primer
   arranque; después el nodo recuerda su identidad en `<dataset>/tailscale`.
3. **Carpeta de estado**: en la shell del NAS, `sudo mkdir -p /mnt/Data/jarvis/tailscale`.
4. **YAML de la app**: en TrueNAS, edita la app, descomenta el servicio `tailscale:` y el bloque `configs:` del final,
   y pon tu `TS_AUTHKEY` y la ruta. Después guarda.
5. **Comprobación**: en el panel de Tailscale debe aparecer la máquina `jarvis`. Si algo falla:
   `sudo docker logs jarvis-tailscale`.
6. **En el móvil**: instala la app de Tailscale con la misma cuenta y abre `https://jarvis.<tu-tailnet>.ts.net`.
   Pedirá el token (`API_TOKEN`) una vez. Mantén pulsada la esfera para hablar. Con *Añadir a pantalla de inicio*
   queda como una app.

Si usas **ACLs** personalizadas en Tailscale, permite el acceso de tus dispositivos al nodo `jarvis` (puerto 443).
Desde el móvil no hay acciones de PC (abrir apps, volumen); esas siguen en `jarvis_hud.py`.

## Obsidian

JARVIS usa una bóveda de Obsidian que vive en un dataset del NAS. Tú la abres desde el PC por SMB (una carpeta
de red) y JARVIS la monta en su contenedor.

| Tool | Qué hace |
|---|---|
| `obsidian_search` | Busca en tus notas (título y contenido, sin distinguir acentos) |
| `obsidian_read` | Lee una nota |
| `obsidian_create_note` | Crea una nota nueva (por defecto en `Inbox/`) |
| `obsidian_append` | Añade texto al final de una nota existente |
| `obsidian_daily_note` | Apunta en la nota de hoy (`Diario/AAAA-MM-DD.md`) con la hora |

Además, `JARVIS/Memoria.md` muestra lo que JARVIS recuerda de ti y se regenera sola en cada cambio. Es de solo
lectura: para corregir algo, díselo a JARVIS o usa `/memoria`.

**Reglas**
- **Nunca borra ni sobrescribe notas.** Si al crear una nota el título ya existe, añade un sufijo: `Nota (2).md`.
- **No sale de la bóveda**, ni siquiera siguiendo enlaces simbólicos, y no toca las carpetas ocultas (`.obsidian`).
- **No escribe secretos**, igual que la memoria.

Ejemplos: *"Apunta que mañana llamo al fontanero"*, *"Añade huevos a la lista de la compra"*,
*"¿Qué tengo apuntado sobre la domótica?"*, *"Crea una nota con esta idea: …"*.

**Montaje (una vez)**
1. **Dataset**: en TrueNAS, crea el dataset `Data/obsidian` con el preset **SMB**.
2. **Permisos**: en el editor de ACL del dataset (*Datasets → obsidian → Permissions → Edit*), añade el usuario
   **`apps`** con permiso **Modify** y aplícalo de forma recursiva. JARVIS corre como `apps` (568).
3. **Carpeta compartida**: en *Shares → SMB → Add*, crea una con ruta `/mnt/Data/obsidian` y nombre `obsidian`.
   Tu usuario de TrueNAS debe tener acceso SMB.
4. **En el PC**: en Windows, *Este equipo → Conectar a unidad de red* → `\\IP-DEL-NAS\obsidian` (por ejemplo, como
   `O:`). Luego, en Obsidian, *Abrir carpeta como bóveda* → `O:\`.
5. **YAML de la app**: añade el volumen `- /mnt/Data/obsidian:/vault` y `OBSIDIAN_VAULT: "/vault"`.
6. **Comprobación**: en `/health` deben aparecer las tools `obsidian_*`, y en la bóveda la carpeta `JARVIS/`.

## Voz

Piper genera la voz en el NAS, sin coste. La voz se descarga sola la primera vez que se usa. Para cambiarla,
edita las variables de la app en TrueNAS y haz redeploy:

| Voz (`PIPER_VOICE`) | Acento | Notas |
|---|---|---|
| `es_ES-davefx-medium` | España | hombre (por defecto) |
| `es_ES-sharvard-medium` | España | dos locutores: `PIPER_SPEAKER` `0` hombre, `1` mujer |
| `es_MX-claude-high` | México | calidad alta |
| `es_AR-daniela-high` | Argentina | calidad alta, mujer |
| `es_MX-ald-medium` | México | hombre |

`PIPER_SPEED` cambia la velocidad (`1.2` = un 20 % más rápido; entre 0.5 y 2).

## Probar sin TrueNAS (desarrollo)

```bash
cd server
pip install -r requirements.txt
API_TOKEN=dev GROQ_API_KEY=... LLM_PROVIDERS=groq DATA_DIR=./data WHISPER_DEVICE=cpu \
  uvicorn jarvis.main:app --port 8765
```

Tests (sin modelos ni red): `pip install -r requirements-dev.txt && python -m pytest -q`

## Estructura

```
server/jarvis/
  config.py     variables de entorno y presets de proveedores
  stt.py        FasterWhisperSTT, GroqSTT
  llm.py        OpenAICompatLLM + FallbackLLM (con tool calling)
  tts.py        PiperTTS (descarga la voz automáticamente), NullTTS
  pipeline.py   Assistant: STT → LLM ⇄ tools → TTS con tiempos por etapa
  main.py       API HTTP (/health, /api/voice, /api/chat [+ /stream], /api/speak, /api/reset, /api/memories) + HUD en /hud/
  web/          HUD (HTML/CSS/JS sin dependencias), servido por el NAS y por jarvis_hud.py
  obsidian.py   bóveda de Obsidian: buscar, leer, crear, añadir, diario y nota de memoria
  memory/
    store.py      SQLite + FTS5: guardar, buscar, corregir, borrar; filtro de secretos
    retrieval.py  qué recuerdos se inyectan en cada turno (reglas) y por qué
  tools/
    registry.py   registro, validación, permisos, timeouts y auditoría
    memory.py     tools de memoria para el LLM
    obsidian.py   tools de Obsidian para el LLM
    basic.py      fecha/hora y tiempo (Open-Meteo)
    pc.py         acciones que ejecuta el cliente del PC
    truenas.py    estado del TrueNAS (API oficial, solo lectura)
client/
  jarvis_client.py   push-to-talk para PC (consola)
  jarvis_hud.py      HUD web local (proxy al NAS + acciones del PC; usa server/jarvis/web)
  wake.py            "Hey Jarvis": palabra de activación local (openWakeWord) y fin de frase
  spotify_login.py   obtiene el refresh token de Spotify (una vez)
  pc_actions.py      ejecuta las acciones permitidas en Windows
  apps.json          apps que JARVIS puede abrir
deploy/truenas/           docker-compose para "Install via YAML"
```

## Checklist antes de subir de nivel (de la guía)

Nivel 1:
- [x] La conversación funciona de principio a fin sin trucos manuales.
- [x] Puedes medir latencia y fallos (tiempos en cliente y logs).
- [x] Puedes cambiar de proveedor solo con variables de entorno.
- [x] Existe una forma de apagarlo (parar la app en TrueNAS).

Nivel 2:
- [x] Cada tool funciona sin LLM (tests en `server/tests/test_tools.py`).
- [x] Cada acción tiene permisos claros (lista permitida, `TOOLS_DISABLED`, `--no-actions`).
- [x] Cada llamada queda en el log de auditoría.

Nivel 3:
- [ ] Recuerda algo dicho en una sesión anterior (y tras reiniciar la app).
- [ ] "Olvida X" lo borra de verdad (`/memoria` ya no lo muestra).
- [ ] Se niega a guardar una contraseña.
- [ ] El log `jarvis.memory` explica por qué usó cada recuerdo.
