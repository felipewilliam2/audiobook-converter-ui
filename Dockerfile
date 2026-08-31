# Reaproveita a imagem já validada do epub_to_audiobook: Python, ffmpeg e o
# main.py com o motor de TTS (Edge TTS) já testados em produção.
FROM ghcr.io/p0n1/epub_to_audiobook:v0.8.7

# ebook-convert (Calibre) precisa de um display -- não roda headless por
# padrão -- por isso xvfb entra junto e é usado via `xvfb-run` no app.py.
# xauth é dependência do próprio xvfb-run (gerencia o arquivo Xauthority);
# sem ele, xvfb-run falha antes mesmo de tentar abrir o display.
RUN apt-get update && \
    apt-get install -y --no-install-recommends calibre xvfb xauth && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app.py /app_src/app.py

# entrypoint.sh da imagem base já reconhece "python3 <script>" como comando
# de passthrough (ver /entrypoint.sh), então não precisa sobrescrevê-lo.
CMD ["python3", "/app_src/app.py"]
