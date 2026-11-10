"""Leitor da fatura do cartão Mercado Pago (PDF). Etapas: `extrair` (pdfplumber) e `interpretar` (puro)."""
from __future__ import annotations

import re
from datetime import date

from app.core.periodo import competencia_de
from app.importers.base import LeitorErro, ResultadoLeitura, TransacaoBruta
from app.importers.documento import Documento, extrair, linhas_do_documento
from app.importers.normalizacao import extrair_parcela, valor_br

_DATA = r"(\d{2})/(\d{2})/(\d{4})"
_VALOR = r"(-?)\s*R\$\s*(-?)\s*(\d{1,3}(?:\.\d{3})*,\d{2})"
_RE_RESUMO = re.compile(rf"Total a pagar.*?\n\s*R\$\s*([\d.]+,\d{{2}})\s+{_DATA}\s+R\$\s*([\d.]+,\d{{2}})", re.S)
_RE_LINHA = re.compile(rf"^(\d{{2}})/(\d{{2}})\s+(.+?)\s+{_VALOR}$")
_RE_CARTAO = re.compile(r"Cartão\s+\w+\s*\[[*\s]*(\d{4})\]", re.I)
_RE_TOTAL_BLOCO = re.compile(rf"^Total\s+{_VALOR}$")


def _data(m: tuple[str, str, str]) -> date:
    return date(int(m[2]), int(m[1]), int(m[0]))


def _centavos(sinal1: str, sinal2: str, texto: str) -> int:
    c = valor_br(texto)
    return -c if (sinal1 or sinal2) else c


def _procurar_data(texto: str, rotulo: str) -> date | None:
    m = re.search(rf"{rotulo}\s+{_DATA}", texto, re.I)
    return _data(m.groups()) if m else None


def _ano(dia: int, mes: int, ref: date) -> date:
    """A fatura traz só DD/MM: a data é a mais recente que não passa da referência (fechamento/vencimento)."""
    for ano in (ref.year, ref.year - 1):
        try:
            d = date(ano, mes, dia)
        except ValueError:
            continue
        if d <= ref:
            return d
    raise ValueError("data fora do período")


def interpretar(doc: Documento) -> ResultadoLeitura:
    linhas = linhas_do_documento(doc)
    texto = "\n".join(linhas)
    resumo = _RE_RESUMO.search(texto)
    if not resumo:
        raise LeitorErro("Fatura Mercado Pago não reconhecida: resumo (Total a pagar / Vence em) não encontrado.")
    total = valor_br(resumo[1])
    vencimento = _data(resumo.groups()[1:4])
    fechamento = _procurar_data(texto, "Fechamento da fatura")
    avisos: list[str] = []
    if fechamento is None:
        avisos.append("Data de fechamento não encontrada na fatura.")
    referencia = fechamento or vencimento

    transacoes: list[TransacaoBruta] = []
    cartoes: list[str] = []
    totais_blocos: list[int] = []
    secao = None  # "movimentacoes" | "cartao"
    ultimos4: str | None = None
    for linha in linhas:
        if linha.startswith("Movimentações na fatura"):
            secao = "movimentacoes"
            continue
        m_cartao = _RE_CARTAO.search(linha)
        if m_cartao:
            secao, ultimos4 = "cartao", m_cartao[1]
            cartoes.append(ultimos4)
            continue
        m_total = _RE_TOTAL_BLOCO.match(linha)
        if m_total and secao == "cartao":
            totais_blocos.append(valor_br(m_total[3]))
            secao = None
            continue
        m = _RE_LINHA.match(linha)
        if not m or secao is None:
            continue
        try:
            data = _ano(int(m[1]), int(m[2]), referencia)
        except ValueError:
            avisos.append(f"Linha ignorada, data inválida: {m[1]}/{m[2]}.")
            continue
        original = m[3].strip()
        valor = _centavos(m[4], m[5], m[6])
        if secao == "movimentacoes":
            if original.lower().startswith("pagamento"):
                transacoes.append(TransacaoBruta(
                    data=data, descricao=original, descricao_original=original, valor=abs(valor),
                    tipo_sugerido="pagamento_fatura",
                ))
            continue
        limpa, num, parc_total = extrair_parcela(original)
        # compra é saída (negativa); estorno/crédito vem negativo na fatura e vira positivo
        transacoes.append(TransacaoBruta(
            data=data, descricao=limpa or original, descricao_original=original, valor=-valor,
            parcela_num=num, parcela_total=parc_total, portador=ultimos4,
        ))

    compras = -sum(t.valor for t in transacoes if t.tipo_sugerido != "pagamento_fatura")
    if not cartoes:
        avisos.append("Bloco do cartão não encontrado na fatura.")
    if compras != total:
        avisos.append("A soma das compras não bate com o total da fatura.")
    if totais_blocos and sum(totais_blocos) != total:
        avisos.append("O total dos blocos de cartão não bate com o total da fatura.")

    return ResultadoLeitura(
        origem="pdf", banco="mercadopago", tipo_conta="cartao",
        identificador_conta=cartoes[0] if cartoes else None,
        transacoes=transacoes,
        competencia_fatura=competencia_de(fechamento) if fechamento else None,
        vencimento=vencimento, fechamento=fechamento, total_informado=total, avisos=avisos,
    )


def ler(pdf_bytes: bytes) -> ResultadoLeitura:
    return interpretar(extrair(pdf_bytes))
