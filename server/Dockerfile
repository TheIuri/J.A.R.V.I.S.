FROM python:3.11-slim

# WITH_CUDA=true incluye las librerias para usar la GPU NVIDIA con Whisper.
# Si no hay GPU, la imagen sigue funcionando en CPU (WHISPER_DEVICE=auto).
ARG WITH_CUDA=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HF_HOME=/data/hf \
    XDG_CACHE_HOME=/data/cache \
    LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib

# Librerias de WeasyPrint, que convierte los informes en PDF (jarvis/docgen.py).
RUN apt-get update && apt-get install --no-install-recommends -y \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfribidi0 libcairo2 libgdk-pixbuf-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-cuda.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_CUDA" = "true" ]; then pip install --no-cache-dir -r requirements-cuda.txt; fi

# Claude Code (binario oficial de npm, sin Node) para el modo Claude con la membresia desde el NAS.
# Solo se usa si pones CLAUDE_CODE_OAUTH_TOKEN; WITH_CLAUDE=false lo deja fuera de la imagen.
ARG WITH_CLAUDE=true
ARG CLAUDE_CODE_VERSION=latest
COPY install_claude.py ./
RUN if [ "$WITH_CLAUDE" = "true" ]; then python install_claude.py "$CLAUDE_CODE_VERSION" /usr/local/bin/claude; fi \
    && rm install_claude.py

COPY jarvis ./jarvis

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["uvicorn", "jarvis.main:app", "--host", "0.0.0.0", "--port", "8765"]
