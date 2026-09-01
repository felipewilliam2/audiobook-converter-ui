"""Interface simples de conversão EPUB/PDF -> audiolivro (Edge TTS pt-BR).

Feita para ser usada por alguém sem perfil técnico: só título, autor, arquivo
e voz. Sem opções de TTS/idioma/engine expostas.

A conversão roda numa thread em background; o navegador acompanha via
polling com fetch() puro (JS embutido no HTML retornado, ver
status_box_html) contra uma rota FastAPI própria (/job_status/<id>) --
fora do sistema de fila/SSE do Gradio inteiramente. Necessário porque o
Gradio atual entrega TODO evento (mesmo com queue=False) pela mesma
conexão /queue/data (SSE) de longa duração da sessão, e o Cloudflare
Tunnel (planos free/pro) corta ela em ~100s -- uma conversão real
(Calibre + TTS) passa disso com frequência. Um fetch() comum não usa
essa conexão, então o polling nunca esbarra nesse limite, não importa
quanto tempo a conversão leve.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
import uuid

import gradio as gr
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError
from mutagen.mp3 import MP3

MAIN_PY = "/app_src/main.py"
LIBRARY_ROOT = "/audiobooks"

VOICES = {
    "Antônio (masculina)": "pt-BR-AntonioNeural",
    "Francisca (feminina)": "pt-BR-FranciscaNeural",
}

PDF_WARNING = (
    "PDF funciona melhor quando é só texto (sem páginas escaneadas). "
    "PDFs digitalizados ou com colunas duplas podem sair com erros ou em "
    "ordem errada — quando disponível, prefira o EPUB."
)

# job_id -> {"message": str, "done": bool}. Em memória, processo único --
# suficiente para o uso doméstico deste app (não sobrevive a um restart do
# pod, mas o job também não sobreviveria mesmo com persistência).
JOBS: dict[str, dict] = {}


def sanitize_path_component(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^\w\s-]", "", value).strip()
    value = re.sub(r"[\s]+", " ", value)
    return value or "Sem título"


def pdf_to_epub(pdf_path: str, work_dir: str) -> str:
    epub_path = os.path.join(work_dir, "livro.epub")
    result = subprocess.run(
        ["xvfb-run", "-a", "ebook-convert", pdf_path, epub_path],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=work_dir,
        env={**os.environ, "HOME": work_dir},
    )
    if result.returncode != 0 or not os.path.exists(epub_path):
        raise RuntimeError(
            "Não consegui converter esse PDF para EPUB. "
            f"Detalhe técnico: {result.stderr[-500:]}"
        )
    return epub_path


def run_tts(epub_path: str, out_dir: str, voice_name: str, work_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    result = subprocess.run(
        [
            "python3",
            MAIN_PY,
            epub_path,
            out_dir,
            "--tts",
            "edge",
            "--language",
            "pt-BR",
            "--voice_name",
            voice_name,
            "--no_prompt",
            "--log",
            "INFO",
        ],
        capture_output=True,
        text=True,
        timeout=3600,
        # main.py cria "logs/" relativo ao cwd. O WORKDIR da imagem base é
        # /app (raiz, dono root) -- como rodamos como UID 1000 (ver
        # deployment.yaml), não tem permissão de escrever lá. work_dir é a
        # nossa própria pasta temporária, gravável.
        cwd=work_dir,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "A geração de áudio falhou. "
            f"Detalhe técnico: {result.stderr[-800:]}"
        )


def tag_and_move_chapters(out_dir: str, autor: str, titulo: str) -> int:
    mp3_files = sorted(f for f in os.listdir(out_dir) if f.lower().endswith(".mp3"))
    if not mp3_files:
        raise RuntimeError("A conversão terminou mas nenhum arquivo de áudio foi gerado.")

    dest_dir = os.path.join(
        LIBRARY_ROOT,
        sanitize_path_component(autor),
        sanitize_path_component(titulo),
    )
    os.makedirs(dest_dir, exist_ok=True)

    for track_number, filename in enumerate(mp3_files, start=1):
        src = os.path.join(out_dir, filename)
        chapter_title = re.sub(r"^\d+_", "", os.path.splitext(filename)[0]).replace("_", " ")

        try:
            tags = EasyID3(src)
        except ID3NoHeaderError:
            audio = MP3(src)
            audio.add_tags()
            audio.save()
            tags = EasyID3(src)

        tags["title"] = chapter_title
        tags["artist"] = autor
        tags["album"] = titulo
        tags["tracknumber"] = str(track_number)
        tags.save()

        shutil.move(src, os.path.join(dest_dir, filename))

    return len(mp3_files)


def run_job(job_id: str, arquivo: str, titulo: str, autor: str, voz_label: str):
    voice_name = VOICES.get(voz_label, next(iter(VOICES.values())))
    ext = os.path.splitext(arquivo)[1].lower()

    try:
        with tempfile.TemporaryDirectory() as work_dir:
            if ext == ".pdf":
                JOBS[job_id]["message"] = "⏳ Convertendo PDF para EPUB..."
                epub_path = pdf_to_epub(arquivo, work_dir)
            else:
                epub_path = arquivo

            JOBS[job_id]["message"] = (
                "⏳ Gerando áudio (isso pode levar alguns minutos)..."
            )
            out_dir = os.path.join(work_dir, "saida")
            run_tts(epub_path, out_dir, voice_name, work_dir)

            JOBS[job_id]["message"] = "⏳ Organizando os arquivos na estante..."
            total = tag_and_move_chapters(out_dir, autor.strip(), titulo.strip())

            JOBS[job_id]["message"] = (
                f"✅ Pronto! **{titulo.strip()}** já está na sua estante "
                f"({total} capítulo(s))."
            )
    except subprocess.TimeoutExpired:
        JOBS[job_id]["message"] = (
            "❌ A conversão demorou demais e foi cancelada. "
            "Tente um livro menor ou tente de novo."
        )
    except RuntimeError as exc:
        JOBS[job_id]["message"] = f"❌ {exc}"
    except Exception as exc:  # noqa: BLE001 - mostrar qualquer falha inesperada pra quem vai debugar
        JOBS[job_id]["message"] = f"❌ Algo deu errado: {exc}"
    finally:
        JOBS[job_id]["done"] = True


# JS que efetivamente inicia o polling -- roda como função de verdade
# (evento js= do Gradio), não como <script> injetado via innerHTML, que os
# navegadores simplesmente ignoram (é assim que HTML.innerHTML sempre
# funcionou -- proteção padrão contra XSS, não é bug do Gradio). Recebe o
# job_id retornado pela função Python anterior e, se não for vazio, começa
# a consultar /job_status/<id> a cada 3s via fetch() puro, direto no DOM,
# sem passar pelo sistema de componentes/fila do Gradio de forma alguma.
POLL_JS = """
(job_id) => {
    if (!job_id) return;
    const box = document.getElementById("status-box");
    const iv = setInterval(() => {
        fetch("/job_status/" + job_id)
            .then((r) => r.json())
            .then((data) => {
                if (box) box.innerHTML = data.message;
                if (data.done) clearInterval(iv);
            })
            .catch(() => {});
    }, 3000);
}
"""


def status_box(message: str) -> str:
    # A div com este id precisa existir sempre que o Gradio atualiza o
    # componente "resultado" (ver botao.click abaixo) -- senão POLL_JS
    # perde a referência via getElementById na próxima atualização via
    # fetch() e o polling continua rodando, mas silenciosamente sem
    # aparecer na tela.
    return f'<div id="status-box">{message}</div>'


def iniciar_conversao(arquivo, titulo, autor, voz_label):
    if arquivo is None:
        return status_box("⚠️ Escolha um arquivo EPUB ou PDF primeiro."), ""
    if not titulo.strip() or not autor.strip():
        return status_box("⚠️ Preencha o título e o autor do livro."), ""
    if os.path.splitext(arquivo)[1].lower() not in (".epub", ".pdf"):
        return status_box("⚠️ Só aceito arquivos .epub ou .pdf."), ""

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"message": "⏳ Iniciando...", "done": False}
    threading.Thread(
        target=run_job,
        args=(job_id, arquivo, titulo, autor, voz_label),
        daemon=True,
    ).start()
    return status_box(JOBS[job_id]["message"]), job_id


with gr.Blocks(title="Conversor de Audiolivros") as demo:
    gr.Markdown("# 🎧 Conversor de Audiolivros")
    gr.Markdown(
        "Suba um livro em EPUB ou PDF, escolha a voz, e converta em audiolivro. "
        "Quando terminar, ele já aparece na sua estante do Audiobookshelf."
    )
    gr.Markdown(f"ℹ️ {PDF_WARNING}")

    with gr.Row():
        with gr.Column():
            arquivo = gr.File(label="Livro (EPUB ou PDF)", file_types=[".epub", ".pdf"], type="filepath")
            titulo = gr.Textbox(label="Título do livro")
            autor = gr.Textbox(label="Autor")
            voz = gr.Dropdown(label="Voz", choices=list(VOICES.keys()), value=list(VOICES.keys())[0])
            botao = gr.Button("Converter", variant="primary")
        with gr.Column():
            resultado = gr.HTML(status_box(""), label="Status")
            job_id_box = gr.Textbox(visible=False)

    # queue=False evita que este clique entre na fila do Gradio atrás de
    # outros eventos -- não tira a chamada da conexão /queue/data em si
    # (não é mais possível desabilitar isso por completo no Gradio atual),
    # mas essa chamada só dispara a thread e retorna na hora, então nunca
    # fica presa tempo suficiente pra importar. Quem acompanha a conversão
    # inteira é o .then() seguinte, via JS real (POLL_JS) chamando
    # /job_status/<id> por fetch() puro -- nunca passa pelo Gradio.
    botao.click(
        fn=iniciar_conversao,
        inputs=[arquivo, titulo, autor, voz],
        outputs=[resultado, job_id_box],
        queue=False,
    ).then(
        fn=None,
        inputs=[job_id_box],
        outputs=None,
        js=POLL_JS,
    )

fastapi_app = FastAPI()


@fastapi_app.get("/job_status/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"message": "", "done": True})
    return JSONResponse({"message": job["message"], "done": job["done"]})


app = gr.mount_gradio_app(fastapi_app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
