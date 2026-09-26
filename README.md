# J.A.R.V.I.S.

Asistente personal de voz construido por niveles, siguiendo la guía *VISION CERO – Construye tu propio JARVIS*.
Arquitectura **híbrida**: lo que se repite cientos de veces (oído, voz, memoria, scripts) corre en local en un
TrueNAS SCALE; el "cerebro" (LLM) usa capas gratuitas en la nube, con un modelo local como opción.

> Regla de la guía: cada nivel funciona antes de montar el siguiente.

## Estado

| Nivel | Qué | Estado |
|---|---|---|
| 1 · Voz | micro → STT → LLM → TTS → altavoz, push-to-talk | ✅ este repo |
| 2 · Tools | registro de herramientas tipadas + permisos | pendiente |
| 3 · Memoria | SQLite (+ Qdrant cuando haga falta) | pendiente |
| 4 · Sentidos | wake word, cámara bajo demanda, event bus | pendiente |
| 5 · Agentes | orquestador + especialistas | pendiente |
| 6 · OS personal | gateway, clientes móvil/escritorio | pendiente |

## Arquitectura (Nivel 1)

```
 PC Windows / Android (cliente)                 TrueNAS SCALE (jarvis-core, Docker)
 ┌───────────────────────────┐   HTTP + token   ┌─────────────────────────────────────────┐
 │ micro ── push-to-talk ────┼─────── WAV ─────▶│ STT  faster-whisper (GTX 1650, CUDA)     │
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
| STT | `local` (faster-whisper), `groq` | `local`, modelo `small` en GPU |
| LLM | `groq`, `gemini`, `openrouter`, `ollama` (todos vía API compatible con OpenAI) | `groq,gemini` en cadena |
| TTS | `piper`, `none` | `piper`, voz `es_ES-davefx-medium` |

Cada respuesta incluye la latencia por etapa (`stt`, `llm`, `tts`, `total`), que también queda en los logs.

## Notas sobre el hardware (i5-10400, 24 GB, GTX 1650)

- **RAM**: los 16 GB de "ZFS Cache" (ARC) no están ocupados de verdad; ZFS los libera cuando las apps los necesitan.
  El contenedor tiene un límite de 6 GB.
- **GPU**: comprueba la VRAM real con `nvidia-smi` en la shell de TrueNAS. La GTX 1650 de sobremesa tiene 4 GB
  (la "Ti" es de portátil). Con 4 GB caben Whisper `small`/`medium` holgadamente, o un LLM pequeño de ~3–4B con
  Ollama, pero no los dos a la vez con comodidad. Por eso la GPU se dedica a Whisper y el LLM va a la nube.
- **Piper** va sobrado en la CPU.

## Instalación en TrueNAS

1. **Drivers NVIDIA**: *Apps → Configuration → Settings → Install NVIDIA Drivers*.
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
  llm.py        OpenAICompatLLM + FallbackLLM
  tts.py        PiperTTS (descarga la voz automáticamente), NullTTS
  pipeline.py   Assistant: STT → LLM → TTS con tiempos por etapa
  main.py       API HTTP (/health, /api/voice, /api/chat, /api/reset)
client/jarvis_client.py   push-to-talk para PC
deploy/truenas/           docker-compose para "Install via YAML"
```

## Checklist antes del Nivel 2 (de la guía)

- [ ] La conversación funciona de principio a fin sin trucos manuales.
- [ ] Puedes medir latencia y fallos (tiempos en cliente y logs).
- [ ] Puedes cambiar de proveedor solo con variables de entorno.
- [ ] Existe una forma de apagarlo (parar la app en TrueNAS).
