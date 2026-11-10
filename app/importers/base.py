"""Contrato comum dos leitores de arquivos de importação (OFX, CSV, PDF)."""
from __future__ import annotations

import importlib
import io
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date


class LeitorErro(ValueError):
    """Arquivo ilegível ou inconsistente para o leitor escolhido."""


@dataclass
class TransacaoBruta:
    data: date
    descricao: str                 # limpa (sem parcela/data grudada), para exibição
    descricao_original: str        # como veio
    valor: int                     # centavos; negativo = saída (no cartão: compra negativa, estorno positivo)
    id_externo: str | None = None  # FITID/Identificador quando houver (NÃO assumir único)
    parcela_num: int | None = None
    parcela_total: int | None = None
    dica_categoria: str | None = None   # ex.: categoria do Itaú ("ALIMENTAÇÃO")
    portador: str | None = None          # ex.: final do cartão adicional "1234"
    tipo_sugerido: str | None = None     # "pagamento_fatura" | "transferencia" | "receita" | "despesa" | "encargo"


@dataclass
class ResultadoLeitura:
    origem: str                      # "ofx" | "csv" | "pdf" | "pdf_ia"
    banco: str | None                # "nubank" | "itau" | "mercadopago" | ...
    tipo_conta: str                  # "cartao" | "corrente"
    identificador_conta: str | None  # ACCTID / final do cartão / agência-conta mascarada
    transacoes: list[TransacaoBruta]
    competencia_fatura: str | None = None
    vencimento: date | None = None
    fechamento: date | None = None
    total_informado: int | None = None    # para validação
    saldo_final_informado: int | None = None
    avisos: list[str] = field(default_factory=list)  # ex.: "soma não bate com o total"


@dataclass
class Leitor:
    """Leitor escolhido pela detecção. `ler(conteudo, ...)` devolve o resultado; o módulo é importado sob demanda."""

    nome: str
    ler: Callable[..., ResultadoLeitura]


def _carregar(modulo: str, funcao: str) -> Callable[..., ResultadoLeitura]:
    def ler(*args, **kwargs) -> ResultadoLeitura:
        return getattr(importlib.import_module(modulo), funcao)(*args, **kwargs)

    return ler


def decodificar(conteudo: bytes) -> str:
    """UTF-8 primeiro (ignora charset declarado no cabeçalho), cp1252 se falhar."""
    try:
        return conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return conteudo.decode("cp1252", errors="replace")


def _sem_acento_minusculo(texto: str) -> str:
    base = unicodedata.normalize("NFD", texto)
    return "".join(c for c in base if not unicodedata.combining(c)).lower()


def detectar_pdf(texto_primeira_pagina: str) -> Leitor | None:
    """Reconhece o PDF pelas palavras-chave do banco na 1ª página."""
    t = _sem_acento_minusculo(texto_primeira_pagina)
    s = t.replace(" ", "")  # extract_text às vezes cola as palavras
    if "mercadopago" in s:
        return Leitor("pdf_mercadopago", _carregar("app.importers.pdf_mercadopago", "ler"))
    if "itau" in s and "extratoconta" in s:
        return Leitor("pdf_itau_extrato", _carregar("app.importers.pdf_itau_extrato", "ler"))
    # na fatura real a 1ª página não traz a palavra "Itaú" (o logo é imagem): usa o resumo característico
    if "resumodafatura" in s and ("itau" in s or ("totaldestafatura" in s and "lancamentosatuais" in s)):
        return Leitor("pdf_itau_fatura", _carregar("app.importers.pdf_itau_fatura", "ler"))
    return None


def _detectar_pdf_bytes(conteudo: bytes) -> Leitor | None:
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
            if not pdf.pages:
                return None
            texto = pdf.pages[0].extract_text() or ""
    except Exception:  # PDF corrompido/protegido: desconhecido
        return None
    return detectar_pdf(texto)


def detectar_leitor(nome_arquivo: str, conteudo: bytes) -> Leitor | None:
    """Escolhe o leitor por extensão e conteúdo; `None` se desconhecido (a UI oferece IA)."""
    if conteudo[:5] == b"%PDF-":
        return _detectar_pdf_bytes(conteudo)
    if conteudo[:2] == b"PK":  # xlsx é um zip: só a planilha da fatura do Itaú tem leitor
        from app.importers.xlsx_itau_fatura import eh_fatura_itau
        if eh_fatura_itau(conteudo):
            return Leitor("xlsx_itau_fatura", _carregar("app.importers.xlsx_itau_fatura", "ler"))
        return None
    texto = decodificar(conteudo[:4096])
    if "OFXHEADER" in texto or "<OFX>" in texto.upper():
        return Leitor("ofx", _carregar("app.importers.ofx", "ler"))
    if nome_arquivo.lower().endswith(".csv"):
        linhas = texto.strip().splitlines()
        colunas = [c.strip().lower() for c in linhas[0].split(",")] if linhas else []
        if colunas[:4] == ["data", "valor", "identificador", "descrição"]:
            return Leitor("csv_nubank_conta", _carregar("app.importers.csv_", "ler_nubank_conta"))
        if colunas[:3] == ["date", "title", "amount"]:
            return Leitor("csv_nubank_cartao", _carregar("app.importers.csv_", "ler_nubank_cartao"))
        if linhas and linhas[0].lower().startswith("data de compra;nome no cartão;final do cartão"):
            return Leitor("csv_c6_cartao", _carregar("app.importers.csv_", "ler_c6_cartao"))
        return Leitor("csv_generico", _carregar("app.importers.csv_", "ler_generico"))
    return None
