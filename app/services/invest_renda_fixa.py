"""Valor atualizado (bruto, sem IR/IOF) de títulos de renda fixa: pré, % do CDI e IPCA+.

Regras:
- dias úteis = datas presentes na série do CDI entre a aplicação (exclusiva) e a data de cálculo (inclusiva);
- pré: aplicado × (1 + taxa)^(du/252);
- cdi: aplicado × Π(1 + cdi_d × pct/100);
- ipca: aplicado × Π(1 + ipca_m) × (1 + taxa)^(du/252), com o IPCA de cada mês de referência M
  entrando para os meses M desde o mês da aplicação até o mês anterior ao da data de cálculo;
  mês sem IPCA divulgado usa o último disponível e o resultado é marcado como "estimado";
- cada aplicação rende por conta própria, com a taxa dela (ou a do ativo, se a aplicação não tiver); um resgate baixa todas as aplicações em aberto na mesma
  proporção (valor resgatado ÷ valor atualizado na data);
- depois do vencimento o valor deixa de render.
"""
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.models import Ativo

UM = Decimal(1)
CEM = Decimal(100)
DIAS_UTEIS_ANO = 252
PRECISAO = 40


@dataclass(frozen=True)
class ValorRendaFixa:
    valor: int  # centavos BRL, bruto
    estimado: bool  # algum mês de IPCA usado ainda não foi divulgado


def _como_serie(serie: Mapping[date, Decimal] | Iterable[tuple[date, Decimal]]) -> dict[date, Decimal]:
    return dict(serie)


def _potencia(base: Decimal, du: int) -> Decimal:
    """base^(du/252) em Decimal."""
    if du == 0:
        return UM
    with localcontext() as ctx:
        ctx.prec = PRECISAO
        return (base.ln() * du / DIAS_UTEIS_ANO).exp()


def _primeiro_do_mes(d: date) -> date:
    return d.replace(day=1)


def _proximo_mes(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


class _Series:
    def __init__(self, cdi: Mapping[date, Decimal], ipca: Mapping[date, Decimal]):
        self.cdi = cdi
        self.dias = sorted(cdi)
        self.ipca = {_primeiro_do_mes(d): v for d, v in ipca.items()}
        self.meses_ipca = sorted(self.ipca)

    def dias_uteis(self, inicio_excl: date, fim_incl: date) -> list[date]:
        return self.dias[bisect_right(self.dias, inicio_excl):bisect_right(self.dias, fim_incl)]

    def ipca_do_mes(self, mes: date) -> tuple[Decimal, bool]:
        if mes in self.ipca:
            return self.ipca[mes], False
        i = bisect_left(self.meses_ipca, mes)
        return (self.ipca[self.meses_ipca[i - 1]] if i else Decimal(0)), True


def _fator(ativo: Ativo, series: _Series, aplicacao: date, ate: date,
           taxa_lote: Decimal | None = None) -> tuple[Decimal, bool]:
    if ate <= aplicacao:
        return UM, False
    uteis = series.dias_uteis(aplicacao, ate)
    taxa = Decimal(taxa_lote if taxa_lote is not None else (ativo.taxa or 0)) / CEM
    if ativo.indexador == "pre":
        return _potencia(UM + taxa, len(uteis)), False
    if ativo.indexador == "cdi":
        fator = UM
        for dia in uteis:
            fator *= UM + series.cdi[dia] / CEM * taxa
        return fator, False
    if ativo.indexador == "ipca":
        fator = _potencia(UM + taxa, len(uteis))
        estimado = False
        mes, limite = _primeiro_do_mes(aplicacao), _primeiro_do_mes(ate)
        while mes < limite:
            valor, estimou = series.ipca_do_mes(mes)
            fator *= UM + valor / CEM
            estimado = estimado or estimou
            mes = _proximo_mes(mes)
        return fator, estimado
    raise ValueError(f"indexador desconhecido: {ativo.indexador!r}")


def valor_atualizado_detalhe(ativo: Ativo, operacoes: Sequence, serie_cdi, serie_ipca, ate: date) -> ValorRendaFixa:
    """Valor bruto atualizado até `ate`, a partir das operações (ordenadas por data, id)."""
    series = _Series(_como_serie(serie_cdi), _como_serie(serie_ipca))
    if ativo.vencimento is not None:
        ate = min(ate, ativo.vencimento)
    lotes: list[list] = []  # [data da aplicação, principal (centavos), fração em aberto, taxa da aplicação]
    estimado = False

    def valor_lote(lote, ate_d: date) -> Decimal:
        nonlocal estimado
        fator, est = _fator(ativo, series, lote[0], ate_d, lote[3])
        estimado = estimado or est
        return lote[1] * fator * lote[2]

    for op in operacoes:
        if op.data > ate:
            break
        if op.tipo == "aplicacao":
            lotes.append([op.data, Decimal(op.quantidade) * op.preco_unitario, UM, getattr(op, "taxa", None)])
        elif op.tipo == "resgate":
            total = sum((valor_lote(lote, op.data) for lote in lotes), Decimal(0))
            if total > 0:
                restante = max(Decimal(0), UM - Decimal(op.quantidade) * op.preco_unitario / total)
                for lote in lotes:
                    lote[2] *= restante
    total = sum((valor_lote(lote, ate) for lote in lotes), Decimal(0))
    return ValorRendaFixa(int(total.quantize(UM, rounding=ROUND_HALF_UP)), estimado)


def valor_atualizado(ativo: Ativo, operacoes: Sequence, serie_cdi, serie_ipca, ate: date) -> int:
    """Valor bruto atualizado em centavos de BRL (ver `valor_atualizado_detalhe` para o aviso de estimativa)."""
    return valor_atualizado_detalhe(ativo, operacoes, serie_cdi, serie_ipca, ate).valor


def valor_na_data_para_posicao(ativo: Ativo, serie_cdi, serie_ipca):
    """Adapta o cálculo ao parâmetro `valor_na_data` de `invest_posicao.posicao` (resgates de renda fixa)."""
    cdi, ipca = _como_serie(serie_cdi), _como_serie(serie_ipca)

    def valor(data: date, anteriores: Sequence) -> int:
        return valor_atualizado(ativo, anteriores, cdi, ipca, data)

    return valor
