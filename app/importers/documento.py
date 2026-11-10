"""Etapa 1 dos leitores de PDF: `extrair(pdf_bytes) -> Documento` (dict serializável em JSON).

Formato do Documento:
    {"paginas": [{"numero": 1, "largura": 595.0, "altura": 842.0,
                  "palavras": [{"t": "texto", "x0": 10.0, "x1": 40.0, "top": 100.0, "bottom": 110.0}, ...],
                  "linhas":   [{"texto": "linha inteira", "x0": 10.0, "top": 100.0}, ...]}]}
"""
from __future__ import annotations

import io
from typing import Any

from app.importers.base import LeitorErro

Documento = dict[str, Any]

_TOLERANCIA_LINHA = 2.5  # pontos de diferença vertical para considerar a mesma linha


def agrupar_palavras(palavras: list[dict]) -> list[list[dict]]:
    """Agrupa palavras (com `top`) em linhas: cada linha é a lista de palavras, da esquerda para a direita."""
    linhas: list[dict] = []
    for p in sorted(palavras, key=lambda p: (p["top"], p["x0"])):
        if linhas and abs(p["top"] - linhas[-1]["top"]) <= _TOLERANCIA_LINHA:
            linhas[-1]["_palavras"].append(p)
        else:
            linhas.append({"top": p["top"], "_palavras": [p]})
    return [sorted(linha["_palavras"], key=lambda p: p["x0"]) for linha in linhas]


def agrupar_linhas(palavras: list[dict]) -> list[dict]:
    """Agrupa palavras (com `top`) em linhas de texto, da esquerda para a direita e de cima para baixo."""
    return [
        {"texto": " ".join(p["t"] for p in linha), "x0": round(linha[0]["x0"], 1), "top": round(linha[0]["top"], 1)}
        for linha in agrupar_palavras(palavras)
    ]


def extrair(pdf_bytes: bytes, x_tolerance: float = 3) -> Documento:
    """Lê o PDF com pdfplumber e devolve palavras e linhas com posição."""
    try:
        import pdfplumber

        paginas = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, pagina in enumerate(pdf.pages, start=1):
                palavras = [
                    {"t": w["text"], "x0": round(w["x0"], 1), "x1": round(w["x1"], 1),
                     "top": round(w["top"], 1), "bottom": round(w["bottom"], 1)}
                    for w in pagina.extract_words(keep_blank_chars=False, use_text_flow=False,
                                                   x_tolerance=x_tolerance)
                ]
                paginas.append({
                    "numero": i, "largura": round(float(pagina.width), 1), "altura": round(float(pagina.height), 1),
                    "palavras": palavras, "linhas": agrupar_linhas(palavras),
                })
    except LeitorErro:
        raise
    except Exception as e:  # PDF corrompido, protegido por senha etc.
        raise LeitorErro(f"Não foi possível ler o PDF: {e}") from e
    if not any(p["palavras"] for p in paginas):
        raise LeitorErro("PDF sem texto extraível (pode ser imagem escaneada).")
    return {"paginas": paginas}


def linhas_do_documento(doc: Documento) -> list[str]:
    """Todas as linhas de texto do documento, página a página."""
    return [linha["texto"] for p in doc["paginas"] for linha in p["linhas"]]


def refazer_palavras(texto: str, x0: float, top: float) -> list[dict]:
    """Palavras com posição sintética para uma linha de texto (usado nas fixtures anonimizadas)."""
    palavras, x = [], x0
    for t in texto.split():
        largura = round(len(t) * 5.0, 1)
        palavras.append({"t": t, "x0": round(x, 1), "x1": round(x + largura, 1),
                         "top": top, "bottom": round(top + 10, 1)})
        x += largura + 4
    return palavras
