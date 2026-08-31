# audiobook-converter-ui

Interface web simples (Gradio, em português) para converter EPUB/PDF em
audiolivro usando [epub_to_audiobook](https://github.com/p0n1/epub_to_audiobook)
com Edge TTS (vozes `pt-BR-AntonioNeural` / `pt-BR-FranciscaNeural`) e organizar
a saída já com tags ID3 corretas dentro da biblioteca do
[Audiobookshelf](https://www.audiobookshelf.org/).

Feita para uso doméstico por alguém sem perfil técnico: só título, autor,
arquivo e voz — sem opções de engine/idioma/flags expostas.

PDF passa primeiro por `ebook-convert` (Calibre, headless via `xvfb-run`) antes
de virar EPUB. Qualidade depende do PDF: funciona bem com PDF só-texto,
degradado em PDFs escaneados (sem OCR) ou de colunas duplas.

Deploy vive em [`felipewilliam2/k3s-cluster`](https://github.com/felipewilliam2/k3s-cluster)
em `apps/familia/audiobook-converter/`, na mesma PVC de biblioteca do
Audiobookshelf.

A imagem é publicada em `ghcr.io/felipewilliam2/audiobook-converter-ui` a cada
push em `main` (ver `.github/workflows/build.yaml`).
