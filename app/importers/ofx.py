"""Leitor OFX (SGML 1.x ou XML), próprio e tolerante: não depende de `ofxparse`."""
from __future__ import annotations

import html
import re
from datetime import date, timedelta

from app.core.dinheiro import parse_reais
from app.core.periodo import competencia_de
from app.importers.base import LeitorErro, ResultadoLeitura, TransacaoBruta, decodificar
from app.importers.normalizacao import extrair_parcela, normalizar_descricao

_TAG = re.compile(r"<(?P<tag>[A-Za-z0-9.]+)>(?P<valor>[^<\r\n]*)")
_BANCOS_POR_ORG = (("NU PAGAMENTOS", "nubank"), ("NUBANK", "nubank"), ("ITAU", "itau"), ("MERCADO", "mercadopago"))
_PAGAMENTO = ("PAGAMENTO RECEBIDO", "PAGAMENTO DE FATURA", "PAGAMENTO FATURA")
# saldo da fatura anterior que o banco repete na fatura atual: não é compra (a dívida já está na fatura de origem)
_SALDO_ANTERIOR = ("VALOR PENDENTE DO MES ANTERIOR", "SALDO RESTANTE DA FATURA ANTERIOR", "SALDO EM ATRASO",
                   "SALDO ANTERIOR")


def _campos(trecho: str) -> dict[str, str]:
    """Primeiro valor de cada tag folha (com ou sem tag de fechamento)."""
    campos: dict[str, str] = {}
    for m in _TAG.finditer(trecho):
        valor = m["valor"].strip()
        if valor and m["tag"].upper() not in campos:
            campos[m["tag"].upper()] = html.unescape(valor)
    return campos


def _data(texto: str) -> date:
    """`AAAAMMDD[HHMMSS[.mmm]][fuso]`, como `20261024120000[-3:BRT]`: vale a data local do arquivo."""
    m = re.match(r"\s*(\d{4})(\d{2})(\d{2})", texto)
    if not m:
        raise ValueError(f"data inválida: {texto!r}")
    return date(int(m[1]), int(m[2]), int(m[3]))


def _valor(texto: str) -> int:
    return parse_reais(texto.replace(",", ".") if "," in texto and "." not in texto else texto)


def _banco(org: str | None, fid: str | None) -> str | None:
    org = (org or "").upper()
    for trecho, banco in _BANCOS_POR_ORG:
        if trecho in org:
            return banco
    return {"260": "nubank", "341": "itau"}.get((fid or "").strip())


def ler(conteudo: bytes) -> ResultadoLeitura:
    texto = decodificar(conteudo)  # o charset do cabeçalho mente: tenta UTF-8 antes de cp1252
    if "<OFX" not in texto.upper():
        raise LeitorErro("Arquivo não parece um OFX válido.")
    cartao = "<CCSTMTRS" in texto.upper()
    corrente = "<STMTRS" in texto.upper()
    if not cartao and not corrente:
        raise LeitorErro("OFX sem extrato de conta nem de cartão.")

    partes = re.split(r"<STMTTRN>", texto, flags=re.IGNORECASE)
    cabecalho = partes[0]
    resto_final = re.split(r"</BANKTRANLIST>", partes[-1], flags=re.IGNORECASE)
    partes[-1] = resto_final[0]
    rodape = resto_final[1] if len(resto_final) > 1 else ""

    head = _campos(cabecalho)
    transacoes: list[TransacaoBruta] = []
    avisos: list[str] = []
    for i, bloco in enumerate(partes[1:], start=1):
        c = _campos(bloco.split("</STMTTRN>")[0])
        try:
            data = _data(c["DTPOSTED"])
            valor = _valor(c["TRNAMT"])
        except (KeyError, ValueError):
            avisos.append(f"Transação {i} ignorada: data ou valor ilegível.")
            continue
        original = c.get("MEMO") or c.get("NAME") or ""
        limpa, num, total = extrair_parcela(original)
        tipo = None
        norm = normalizar_descricao(original)
        if cartao and valor > 0 and any(norm.startswith(p) for p in _PAGAMENTO):
            tipo = "pagamento_fatura"
        elif cartao and valor < 0 and any(norm.startswith(p) for p in _SALDO_ANTERIOR):
            tipo = "saldo_anterior"
        transacoes.append(TransacaoBruta(
            data=data, descricao=limpa or original, descricao_original=original, valor=valor,
            id_externo=c.get("FITID"), parcela_num=num, parcela_total=total, tipo_sugerido=tipo,
        ))

    saldo = None
    if corrente and not cartao and "BALAMT" in _campos(rodape):
        try:
            saldo = _valor(_campos(rodape)["BALAMT"])
        except ValueError:
            avisos.append("Saldo final ilegível.")

    banco = _banco(head.get("ORG"), head.get("FID"))
    total = fechamento = competencia = None
    if cartao and banco == "nubank":
        total, fechamento = _fatura_fechada_nubank(head, _campos(rodape))
        competencia = _competencia_do_periodo_nubank(head)

    return ResultadoLeitura(
        origem="ofx",
        banco=banco,
        tipo_conta="cartao" if cartao else "corrente",
        identificador_conta=head.get("ACCTID"),
        transacoes=transacoes,
        competencia_fatura=competencia,
        fechamento=fechamento,
        total_informado=total,
        saldo_final_informado=saldo,
        avisos=avisos,
    )


def _competencia_do_periodo_nubank(head: dict[str, str]) -> str | None:
    """O OFX do cartão Nubank traz uma fatura só, aberta ou fechada: o período vai do fechamento anterior até
    o dia seguinte ao fechamento (DTEND). O mês de DTEND menos um dia é a fatura de todas as linhas, inclusive
    a compra do primeiro dia do período, que o dia de fechamento cadastrado pode jogar na fatura anterior."""
    try:
        return competencia_de(_data(head["DTEND"]) - timedelta(days=1))
    except (KeyError, ValueError):
        return None


def _fatura_fechada_nubank(head: dict[str, str], rodape: dict[str, str]) -> tuple[int | None, date | None]:
    """OFX de uma fatura do Nubank já fechada: o período vai até o dia seguinte ao fechamento e o
    LEDGERBAL é o total da fatura. Com a fatura ainda aberta (fim do período depois da geração do
    arquivo) o saldo é parcial e não serve de conferência."""
    try:
        fim, gerado = _data(head["DTEND"]), _data(head["DTSERVER"])
        saldo = _valor(rodape["BALAMT"])
    except (KeyError, ValueError):
        return None, None
    if fim > gerado:
        return None, None
    return -saldo, fim - timedelta(days=1)
