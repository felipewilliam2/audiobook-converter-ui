"""Interface simples de conversão EPUB/PDF -> audiolivro (Edge TTS pt-BR).

Feita para ser usada por alguém sem perfil técnico: só título, autor, arquivo
e voz. Sem opções de TTS/idioma/engine expostas.

A conversão roda numa thread em background; o navegador acompanha via
polling com fetch() puro (JS real, ver POLL_JS) contra uma rota FastAPI
própria (/job_status/<id>) -- fora do sistema de fila/SSE do Gradio
inteiramente. Necessário porque o Gradio atual entrega TODO evento (mesmo
com queue=False) pela mesma conexão /queue/data (SSE) de longa duração da
sessão, e o Cloudflare Tunnel (planos free/pro) corta ela em ~100s -- uma
conversão real (Calibre + TTS) passa disso com frequência. Um fetch() comum
não usa essa conexão, então o polling nunca esbarra nesse limite.

Progresso: lê a saída do `ebook-convert` e do `main.py` linha a linha
(Popen, não subprocess.run) em vez de esperar o processo inteiro terminar
pra só então saber o resultado -- isso é o que permite mostrar uma barra
de progresso de verdade em vez de só "processando...".
"""

import collections
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
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
PDF_CONVERT_TIMEOUT = 1200
# Livros grandes de verdade passam de 1h fácil (860 mil caracteres, 104
# capítulos, levou ~65min num teste real) -- a arquitetura em background
# não tem mais motivo pra manter um timeout apertado tipo os 3600s
# originais (isso era resquício do design síncrono antigo, onde um job
# preso travava o navegador; agora só mostra "gerando áudio..." por mais
# tempo, sem travar nada). Generoso, mas ainda finito pra evitar um job
# realmente travado rodando pra sempre.
TTS_TIMEOUT = 7200

VOICES = {
    "Antônio (masculina)": "pt-BR-AntonioNeural",
    "Francisca (feminina)": "pt-BR-FranciscaNeural",
}

PDF_WARNING = (
    "PDF funciona melhor quando é só texto (sem páginas escaneadas). "
    "PDFs digitalizados ou com colunas duplas podem sair com erros ou em "
    "ordem errada — quando disponível, prefira o EPUB."
)

# job_id -> {"message": str, "pct": int, "done": bool}. Em memória,
# processo único -- suficiente para o uso doméstico deste app (não
# sobrevive a um restart do pod, mas o job também não sobreviveria mesmo
# com persistência).
JOBS: dict[str, dict] = {}


def sanitize_path_component(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^\w\s-]", "", value).strip()
    value = re.sub(r"[\s]+", " ", value)
    return value or "Sem título"


def _run_with_progress(cmd: list[str], cwd: str, timeout: int, result: dict, env=None):
    """Roda `cmd`, produzindo cada linha de stdout/stderr (combinados) em
    tempo real via yield, em vez de só devolver tudo no final. Levanta
    subprocess.TimeoutExpired se passar do tempo limite. `result` é um
    dict vazio fornecido por quem chama (não global -- cada chamada tem o
    seu, importante porque múltiplas conversões podem rodar concorrentes
    em threads diferentes) que recebe result["returncode"] depois que o
    loop `for line in _run_with_progress(...)` terminar.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=env,
    )
    start = time.monotonic()
    try:
        for line in proc.stdout:
            if time.monotonic() - start > timeout:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)
            yield line
    finally:
        proc.wait()
        result["returncode"] = proc.returncode


def pdf_to_epub(pdf_path: str, work_dir: str, job_id: str) -> str:
    epub_path = os.path.join(work_dir, "livro.epub")
    tail: collections.deque = collections.deque(maxlen=40)
    result: dict = {}

    for line in _run_with_progress(
        ["xvfb-run", "-a", "ebook-convert", pdf_path, epub_path],
        cwd=work_dir,
        timeout=PDF_CONVERT_TIMEOUT,
        result=result,
        env={**os.environ, "HOME": work_dir},
    ):
        tail.append(line)
        # Calibre imprime linhas tipo "34% Running transforms on e-book..."
        m = re.match(r"\s*(\d{1,3})%", line)
        if m:
            calibre_pct = min(int(m.group(1)), 100)
            JOBS[job_id]["pct"] = round(calibre_pct * 0.15)  # etapa PDF = 0-15% do total
            JOBS[job_id]["message"] = f"⏳ Convertendo PDF para EPUB... {calibre_pct}%"

    if result.get("returncode") != 0 or not os.path.exists(epub_path):
        raise RuntimeError(
            "Não consegui converter esse PDF para EPUB. "
            f"Detalhe técnico: {''.join(tail)[-500:]}"
        )
    return epub_path


def run_tts(epub_path: str, out_dir: str, voice_name: str, work_dir: str, job_id: str, pct_start: int):
    os.makedirs(out_dir, exist_ok=True)
    tail: collections.deque = collections.deque(maxlen=40)
    result: dict = {}
    total_chapters = None
    pct_span = 90 - pct_start  # etapa TTS ocupa até 90% do total

    for line in _run_with_progress(
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
        # main.py cria "logs/" relativo ao cwd. O WORKDIR da imagem base é
        # /app (raiz, dono root) -- como rodamos como UID 1000 (ver
        # deployment.yaml), não tem permissão de escrever lá. work_dir é a
        # nossa própria pasta temporária, gravável.
        cwd=work_dir,
        timeout=TTS_TIMEOUT,
        result=result,
    ):
        tail.append(line)

        m_total = re.search(r"Chapters count: (\d+)", line)
        if m_total:
            total_chapters = int(m_total.group(1))
            JOBS[job_id]["message"] = f"⏳ Gerando áudio: 0/{total_chapters} capítulos"

        m_done = re.search(r"Converted chapter (\d+)", line)
        if m_done and total_chapters:
            done = int(m_done.group(1))
            frac = min(done / total_chapters, 1.0)
            JOBS[job_id]["pct"] = round(pct_start + frac * pct_span)
            JOBS[job_id]["message"] = f"⏳ Gerando áudio: {done}/{total_chapters} capítulos"

    if result.get("returncode") != 0:
        raise RuntimeError(
            "A geração de áudio falhou. "
            f"Detalhe técnico: {''.join(tail)[-800:]}"
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
            pct_start = 0
            if ext == ".pdf":
                JOBS[job_id]["message"] = "⏳ Convertendo PDF para EPUB..."
                epub_path = pdf_to_epub(arquivo, work_dir, job_id)
                pct_start = 15
            else:
                epub_path = arquivo

            JOBS[job_id]["pct"] = pct_start
            out_dir = os.path.join(work_dir, "saida")

            # Mesmo se a geração de áudio estourar o tempo limite ou falhar
            # no meio de um livro grande, ainda tentamos salvar os
            # capítulos que já converteram com sucesso até aquele ponto --
            # descartar um livro inteiro (às vezes depois de mais de uma
            # hora rodando) por causa de um timeout batendo perto do fim
            # seria jogar fora trabalho real. Só propaga o erro se não
            # sobrou nenhum capítulo pra salvar.
            partial_reason = None
            try:
                run_tts(epub_path, out_dir, voice_name, work_dir, job_id, pct_start)
            except subprocess.TimeoutExpired:
                partial_reason = "a conversão demorou demais e foi interrompida"
            except RuntimeError as exc:
                partial_reason = str(exc)

            has_output = os.path.isdir(out_dir) and any(
                f.lower().endswith(".mp3") for f in os.listdir(out_dir)
            )
            if partial_reason and not has_output:
                raise RuntimeError(partial_reason)

            JOBS[job_id]["pct"] = 95
            JOBS[job_id]["message"] = "⏳ Organizando os arquivos na estante..."
            total = tag_and_move_chapters(out_dir, autor.strip(), titulo.strip())

            JOBS[job_id]["pct"] = 100
            if partial_reason:
                JOBS[job_id]["message"] = (
                    f"⚠️ Convertido parcialmente: {total} capítulo(s) de "
                    f"**{titulo.strip()}** já estão na sua estante, mas a "
                    f"conversão não terminou o livro inteiro ({partial_reason})."
                )
            else:
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
    const bar = document.getElementById("status-bar");
    const iv = setInterval(() => {
        fetch("/job_status/" + job_id)
            .then((r) => r.json())
            .then((data) => {
                if (box) box.innerHTML = data.message;
                if (bar && typeof data.pct === "number") bar.value = data.pct;
                if (data.done) clearInterval(iv);
            })
            .catch(() => {});
    }, 3000);
}
"""


def status_box(message: str, pct: int = 0) -> str:
    # Os ids destas duas tags precisam existir sempre que o Gradio
    # atualiza o componente "resultado" (ver botao.click abaixo) -- senão
    # POLL_JS perde a referência via getElementById na próxima atualização
    # via fetch() e o polling continua rodando, mas silenciosamente sem
    # aparecer na tela.
    return (
        f'<progress id="status-bar" value="{pct}" max="100" '
        'style="width:100%;height:1.2rem"></progress>'
        f'<div id="status-box" style="margin-top:0.5rem">{message}</div>'
    )


def iniciar_conversao(arquivo, titulo, autor, voz_label):
    if arquivo is None:
        return status_box("⚠️ Escolha um arquivo EPUB ou PDF primeiro."), ""
    if not titulo.strip() or not autor.strip():
        return status_box("⚠️ Preencha o título e o autor do livro."), ""
    if os.path.splitext(arquivo)[1].lower() not in (".epub", ".pdf"):
        return status_box("⚠️ Só aceito arquivos .epub ou .pdf."), ""

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"message": "⏳ Iniciando...", "pct": 0, "done": False}
    threading.Thread(
        target=run_job,
        args=(job_id, arquivo, titulo, autor, voz_label),
        daemon=True,
    ).start()
    return status_box(JOBS[job_id]["message"], 0), job_id


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
        return JSONResponse({"message": "", "pct": 0, "done": True})
    return JSONResponse({"message": job["message"], "pct": job.get("pct", 0), "done": job["done"]})


app = gr.mount_gradio_app(fastapi_app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
