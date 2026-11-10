"""Leitor do extrato da conta corrente Itaú (PDF). Etapas: `extrair` (pdfplumber) e `interpretar` (puro)."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from app.importers.base import LeitorErro, ResultadoLeitura, TransacaoBruta
from app.importers.documento import Documento, extrair, linhas_do_documento
from app.importers.normalizacao import normalizar_descricao, valor_br

_RE_LINHA = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(.+?)\s+(-?[\d.]+,\d{2})$")
_RE_CONTA = re.compile(r"agência:\s*(\d+)\s+conta:\s*([\d-]+)", re.I)
_RE_SALDO = re.compile(r"R\$\s*(-?)\s*(-?[\d.]+,\d{2})")
_DATA_GRUDADA = re.compile(r"\s*\d{2}/\d{2}$")

# (padrão sobre a descrição normalizada, tipo sugerido), na ordem de prioridade
_TIPOS = (
    (re.compile(r"^(ITAU VISA|FATURA )"), "pagamento_fatura"),
    (re.compile(r"^PIX QRS (NU PAGAMENT|MERCADO PAG)"), "pagamento_fatura"),
    (re.compile(r"^REMUNERACAO/SALARIO"), "receita"),
    (re.compile(r"^REND PAGO"), "receita"),
    (re.compile(r"^(JUROS|IOF)"), "encargo"),
    (re.compile(r"^PIX TRANSF"), "transferencia"),
)


def tipo_sugerido(descricao: str) -> str | None:
    norm = normalizar_descricao(descricao)
    for padrao, tipo in _TIPOS:
        if padrao.search(norm):
            return tipo
    return None


def _saldo_cabecalho(linhas: list[str]) -> int | None:
    for i, linha in enumerate(linhas):
        if linha.lower().startswith("saldo em conta") and i + 1 < len(linhas):
            m = _RE_SALDO.search(linhas[i + 1])
            if m:
                c = valor_br(m[2])
                return -abs(c) if m[1] else c
    return None


def validar_saldos(
    saldos: dict[date, int], lancamentos: list[tuple[date, int]]
) -> list[str]:
    """Saldo anterior + lançamentos do período = saldo do dia. Devolve avisos de divergência."""
    avisos: list[str] = []
    datas = sorted(saldos)
    por_dia: dict[date, int] = defaultdict(int)
    for d, v in lancamentos:
        por_dia[d] += v
    for anterior, atual in zip(datas, datas[1:]):
        soma = sum(v for d, v in por_dia.items() if anterior < d <= atual)
        esperado = saldos[anterior] + soma
        if esperado == saldos[atual]:
            continue
        if atual == datas[-1]:  # o banco às vezes inclui no último saldo lançamentos datados depois dele
            if esperado + sum(v for d, v in por_dia.items() if d > atual) == saldos[atual]:
                continue
        avisos.append(f"Saldo do dia {atual:%d/%m/%Y} não bate com os lançamentos do extrato.")
    return avisos


def interpretar(doc: Documento) -> ResultadoLeitura:
    linhas = linhas_do_documento(doc)
    texto = "\n".join(linhas)
    if "extrato conta" not in texto.lower() and "SALDO DO DIA" not in texto:
        raise LeitorErro("Extrato Itaú não reconhecido.")
    m_conta = _RE_CONTA.search(texto)
    identificador = f"{m_conta[1]}/{m_conta[2]}" if m_conta else None

    saldos: dict[date, int] = {}
    brutos: list[tuple[date, str, int]] = []
    avisos: list[str] = []
    for linha in linhas:
        m = _RE_LINHA.match(linha)
        if not m:
            continue
        try:
            data = date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            avisos.append(f"Linha ignorada, data inválida: {m[1]}/{m[2]}/{m[3]}.")
            continue
        valor = valor_br(m[5])
        if m[4].strip().upper().startswith("SALDO DO DIA"):
            saldos[data] = valor
        else:
            brutos.append((data, m[4].strip(), valor))

    if not brutos and not saldos:
        raise LeitorErro("Nenhum lançamento encontrado no extrato Itaú.")

    # o extrato vem em ordem decrescente: inverte para ficar cronológico (estável dentro do mesmo dia)
    transacoes = [
        TransacaoBruta(
            data=data, descricao=_DATA_GRUDADA.sub("", original).strip(), descricao_original=original,
            valor=valor, tipo_sugerido=tipo_sugerido(original),
        )
        for data, original, valor in reversed(brutos)
    ]
    avisos += validar_saldos(saldos, [(d, v) for d, _, v in brutos])
    return ResultadoLeitura(
        origem="pdf", banco="itau", tipo_conta="corrente", identificador_conta=identificador,
        transacoes=transacoes, saldo_final_informado=_saldo_cabecalho(linhas), avisos=avisos,
    )


def ler(pdf_bytes: bytes) -> ResultadoLeitura:
    return interpretar(extrair(pdf_bytes))
