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

Una interfaz web con una esfera animada que cambia de color según el estado (en espera, escuchando, pensando,
hablando) y late con la voz. Incluye subtítulos, historial, un panel de sesión (intercambios, latencia, pico,
tiempo por etapa, modelo y tools usadas) y la lista de recuerdos con botón para olvidar.

```bash
cd client
py jarvis_hud.py        # abre http://localhost:8766 (usa JARVIS_SERVER y JARVIS_TOKEN)
```

- **Para hablar**: mantén pulsada la **barra espaciadora** o la esfera. También puedes escribir abajo.
- **Cómo funciona**: `jarvis_hud.py` corre en tu PC, sirve la página y reenvía las peticiones al NAS.
  - El token **no llega al navegador**.
  - Las acciones del PC se ejecutan aquí, con `apps.json`. Los temporizadores avisan por voz también en el HUD.
  - Solo escucha en `127.0.0.1` y rechaza peticiones de otras webs (comprueba `Host` y `Origin`).
- **Micrófono**: el navegador pide permiso la primera vez. Funciona porque `localhost` cuenta como sitio seguro.
  Para usarlo desde el móvil hará falta HTTPS, que es el siguiente paso.
- **Opciones**: `--no-actions` (sin acciones en el PC), `--port 8766` y `--no-browser`.

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
  main.py       API HTTP (/health, /api/voice, /api/chat, /api/speak, /api/reset)
  memory/
    store.py      SQLite + FTS5: guardar, buscar, corregir, borrar; filtro de secretos
    retrieval.py  qué recuerdos se inyectan en cada turno (reglas) y por qué
  tools/
    registry.py   registro, validación, permisos, timeouts y auditoría
    memory.py     tools de memoria para el LLM
    basic.py      fecha/hora y tiempo (Open-Meteo)
    pc.py         acciones que ejecuta el cliente del PC
    truenas.py    estado del TrueNAS (API oficial, solo lectura)
client/
  jarvis_client.py   push-to-talk para PC (consola)
  jarvis_hud.py      HUD web local (proxy al NAS + acciones del PC)
  hud/               página del HUD (HTML/CSS/JS sin dependencias)
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
