"""Leitor da planilha da fatura do cartão Itaú (o "salvar em Excel" do site).

Um layout só para a fatura fechada, a atual e a próxima: um título ("Fatura Fechada - Outubro/2026"), o cartão
com o valor ("Valor" ou "Valor (parcial)") e o vencimento, e a tabela "Data | Lançamento | Parcelamento | Valor |
(dólar) | Titularidade | Nome | Tipo do cartão | Número do cartão". As colunas são achadas pelo rótulo.
Na planilha a compra é positiva e o estorno/pagamento negativo; no app é o contrário (saída do cartão).
"""
from __future__ import annotations

import io
import re
import warnings
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.periodo import competencia_de
from app.importers.base import LeitorErro, ResultadoLeitura, TransacaoBruta
from app.importers.normalizacao import extrair_parcela, valor_br

# o Itaú fecha a fatura ~7 dias antes do vencimento (a emissão do PDF): é por ele que o app nomeia a fatura
DIAS_FECHAMENTO_ANTES_DO_VENCIMENTO = 7
_FINAL = re.compile(r"final\s*(\d{4})", re.I)
_NUMERO = re.compile(r"(\d{4})\s*$")


def _texto(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _centavos(v) -> int:
    if isinstance(v, (int, float)):
        return int((Decimal(str(v)) * 100).quantize(Decimal(1)))
    return valor_br(_texto(v))


def _data(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _linhas(conteudo: bytes) -> list[tuple]:
    try:
        import openpyxl
        with warnings.catch_warnings():  # a planilha do Itaú não tem estilo padrão; o openpyxl avisa à toa
            warnings.simplefilter("ignore", UserWarning)
            wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except Exception as exc:  # zip corrompido, não é xlsx
        raise LeitorErro("não consegui abrir a planilha") from exc
    try:
        return [tuple(linha) for linha in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()


def _indice(linha: tuple, rotulo: str) -> int | None:
    for i, v in enumerate(linha):
        if _texto(v).lower().startswith(rotulo.lower()):
            return i
    return None


def eh_fatura_itau(conteudo: bytes) -> bool:
    try:
        linhas = _linhas(conteudo)
    except LeitorErro:
        return False
    return _achar_cabecalhos(linhas) is not None


def _achar_cabecalhos(linhas: list[tuple]):
    """(índice da linha "Cartão | Valor | Vencimento", índice da linha "Data | Lançamento ...") ou None."""
    cartao = tabela = None
    for i, linha in enumerate(linhas):
        textos = [_texto(v).lower() for v in linha]
        if cartao is None and "cartão" in textos and "vencimento" in textos:
            cartao = i
        if "data" in textos and "lançamento" in textos and any(t.startswith("valor") for t in textos):
            tabela = i
            break
    return (cartao, tabela) if cartao is not None and tabela is not None else None


def ler(conteudo: bytes) -> ResultadoLeitura:
    linhas = _linhas(conteudo)
    achados = _achar_cabecalhos(linhas)
    if achados is None:
        raise LeitorErro("a planilha não parece uma fatura do cartão Itaú (sem as colunas Data/Lançamento/Valor)")
    i_cartao, i_tabela = achados
    titulo = next((_texto(v) for linha in linhas[:i_cartao] for v in linha if _texto(v).lower().startswith("fatura")),
                  "")
    rotulos, dados = linhas[i_cartao], linhas[i_cartao + 1]
    c_valor, c_venc, c_cartao = _indice(rotulos, "valor"), _indice(rotulos, "vencimento"), _indice(rotulos, "cartão")
    parcial = "parcial" in _texto(rotulos[c_valor]).lower() or "fechada" not in titulo.lower()
    vencimento = _data(dados[c_venc])
    if vencimento is None:
        raise LeitorErro("vencimento da fatura ilegível")
    total = _centavos(dados[c_valor]) if dados[c_valor] is not None else None
    final = _FINAL.search(_texto(dados[c_cartao]))

    cab = linhas[i_tabela]
    c = {nome: _indice(cab, rotulo) for nome, rotulo in (
        ("data", "data"), ("desc", "lançamento"), ("parc", "parcelamento"), ("valor", "valor"),
        ("titular", "titularidade"), ("numero", "número"))}
    transacoes: list[TransacaoBruta] = []
    for linha in linhas[i_tabela + 1:]:
        d = _data(linha[c["data"]]) if c["data"] is not None and c["data"] < len(linha) else None
        if d is None:
            if any("subtotal" in _texto(v).lower() for v in linha):
                break
            continue
        original = _texto(linha[c["desc"]])
        parcela = _texto(linha[c["parc"]]) if c["parc"] is not None else ""
        descricao, num, tot = extrair_parcela(f"{original} {parcela}".strip())
        # a coluna sem rótulo entre o valor e a titularidade traz o valor em dólar das compras internacionais
        extra = " ".join(_texto(v) for v in linha[c["valor"] + 1:c["titular"]] if _texto(v)) if c["titular"] else ""
        numero = _NUMERO.search(_texto(linha[c["numero"]])) if c["numero"] is not None else None
        valor = -_centavos(linha[c["valor"]])
        minusculo = original.lower()
        tipo = ("pagamento_fatura" if minusculo.startswith("pagamento efetuado")
                else "encargo" if minusculo.startswith("iof") else None)
        transacoes.append(TransacaoBruta(
            data=d, descricao=descricao or original, descricao_original=" · ".join(p for p in (original, parcela, extra) if p),
            valor=valor, parcela_num=num, parcela_total=tot, portador=numero[1] if numero else None,
            tipo_sugerido=tipo))

    avisos: list[str] = []
    if not parcial and total is not None:
        soma = -sum(t.valor for t in transacoes if t.tipo_sugerido != "pagamento_fatura")
        if soma != total:
            avisos.append(f"A soma dos lançamentos ({soma / 100:.2f}) não bate com o total da fatura ({total / 100:.2f}).")
    return ResultadoLeitura(
        origem="xlsx", banco="itau", tipo_conta="cartao", identificador_conta=final[1] if final else None,
        transacoes=transacoes,
        competencia_fatura=competencia_de(vencimento - timedelta(days=DIAS_FECHAMENTO_ANTES_DO_VENCIMENTO)),
        vencimento=vencimento, fechamento=None,
        # fatura ainda aberta: o valor é parcial e não serve de conferência (como o OFX aberto do Nubank)
        total_informado=None if parcial else total, avisos=avisos)
