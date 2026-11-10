"""Evolução mensal da carteira: patrimônio, aportes e rentabilidade (Dietz modificado) contra CDI e Ibovespa.

Convenções:
- ponto de cada mês = último dia do mês; no mês de `ate`, o próprio `ate` (preço atual);
- patrimônio = posição na data × fechamento mensal em cache (último fechamento até a data);
  renda fixa pelo valor atualizado; sem preço em cache, a posição vale o custo;
- fluxos: compra/aplicação = + (valor + taxas); venda/resgate = − (valor − taxas);
  ajuste de posição = + variação do custo (a posição "aparece" na data);
- Dietz modificado no mês: r = (V_fim − V_ini − F) / (V_ini + Σ F_i × w_i), com w_i = dias restantes ÷ dias do mês
  (o período começa no último dia do mês anterior); acumulado por encadeamento;
- proventos (dividendos, JCP, rendimentos): cotas na véspera da data ex × valor por cota do cache; no exterior,
  em reais pelo dólar do dia e líquidos dos 30% retidos. Saem da carteira para a conta, então entram no Dietz como
  fluxo negativo na data ex (somam no retorno) e não mexem no aplicado;
- CDI acumulado: composição dos CDIs diários do período; Ibovespa: fechamento ÷ fechamento do mês anterior ao
  da primeira operação (se não houver, o primeiro fechamento disponível).
"""
from bisect import bisect_right
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ativo, CotacaoCache, ProventoCache
from app.services.invest_carteira import (
    SIMBOLO_CAMBIO, _cotacao_brl, cambio_atual, operacoes_por_ativo, posicao_do_ativo, series_indicadores,
)
from app.services.invest_posicao import recebido
from app.services.invest_renda_fixa import valor_atualizado
from app.services.mercado.cotacoes import CLASSES_PROVENTOS, moeda_cotacao
from app.services.mercado.tesouro import preco_de_mercado

UM = Decimal(1)
CEM = Decimal(100)
SIMBOLO_IBOV = "^BVSP"
LIQUIDO_EXTERIOR = Decimal("0.7")  # dividendo no exterior chega com 30% retidos na fonte
SIMBOLO_GLOBAL = "VT"  # Vanguard Total World: índice global, comparado em reais (cotação × dólar do dia)


@dataclass(frozen=True)
class PontoMes:
    competencia: str  # "AAAA-MM"
    patrimonio: int
    aplicado_acumulado: int
    rent_mes_pct: Decimal
    rent_acum_pct: Decimal
    cdi_acum_pct: Decimal | None
    ibov_acum_pct: Decimal | None
    global_acum_pct: Decimal | None = None


def _centavo(valor: Decimal) -> int:
    return int(valor.quantize(UM, rounding=ROUND_HALF_UP))


def _fim_do_mes(ano: int, mes: int) -> date:
    return date(ano, mes, monthrange(ano, mes)[1])


def _meses(primeiro: date, ate: date) -> list[tuple[int, int]]:
    resultado, (a, m) = [], (primeiro.year, primeiro.month)
    while (a, m) <= (ate.year, ate.month):
        resultado.append((a, m))
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return resultado


class _Fechamentos:
    """Último fechamento em cache até uma data, por símbolo."""

    def __init__(self, s: Session):
        self.por_simbolo: dict[str, tuple[list[date], list[Decimal]]] = {}
        for r in s.scalars(select(CotacaoCache).order_by(CotacaoCache.simbolo, CotacaoCache.data)):
            datas, valores = self.por_simbolo.setdefault(r.simbolo, ([], []))
            datas.append(r.data)
            valores.append(r.fechamento)

    def ate(self, simbolo: str, dia: date) -> Decimal | None:
        datas, valores = self.por_simbolo.get(simbolo, ([], []))
        i = bisect_right(datas, dia)
        return valores[i - 1] if i else None

    def primeiro(self, simbolo: str) -> Decimal | None:
        _, valores = self.por_simbolo.get(simbolo, ([], []))
        return valores[0] if valores else None


def _fluxos(ativo: Ativo, ops: list, cdi, ipca) -> list[tuple[date, Decimal]]:
    fluxos = []
    for i, op in enumerate(ops):
        bruto = Decimal(op.quantidade) * op.preco_unitario * (UM if op.cambio is None else Decimal(op.cambio))
        taxas = op.taxas or 0
        if op.tipo in ("compra", "aplicacao"):
            fluxos.append((op.data, bruto + taxas))
        elif op.tipo in ("venda", "resgate"):
            fluxos.append((op.data, -recebido(op, bruto)))
        elif op.tipo == "ajuste_posicao":
            antes = posicao_do_ativo(ativo, ops[:i], cdi, ipca).custo_brl
            fluxos.append((op.data, bruto - antes))
    return fluxos


def _proventos(s: Session, ativos: dict[int, Ativo], ops_por: dict[int, list], inicio: date, ate: date,
               cdi, ipca, fech: _Fechamentos) -> list[tuple[date, Decimal]]:
    """Proventos recebidos (data ex, −valor em centavos de real): saem da carteira como um resgate."""
    pagadores = {aid: a for aid, a in ativos.items()
                 if aid in ops_por and a.classe in CLASSES_PROVENTOS and a.simbolo_cotacao}
    por_simbolo: dict[str, list[tuple[date, Decimal]]] = {}
    for simbolo, dia, valor in s.execute(select(ProventoCache.simbolo, ProventoCache.data_ex, ProventoCache.valor).where(
            ProventoCache.simbolo.in_({a.simbolo_cotacao for a in pagadores.values()}),
            ProventoCache.data_ex > inicio, ProventoCache.data_ex <= ate)):
        por_simbolo.setdefault(simbolo, []).append((dia, valor))
    recebidos = []
    for aid, a in pagadores.items():
        for dia, valor in por_simbolo.get(a.simbolo_cotacao, []):
            antes = [o for o in ops_por[aid] if o.data < dia]  # quem compra na data ex já não recebe
            if not antes:
                continue
            quantidade = posicao_do_ativo(a, antes, cdi, ipca).quantidade
            if quantidade <= 0:
                continue
            total = quantidade * valor * CEM
            if moeda_cotacao(a) == "USD":
                taxa = fech.ate(SIMBOLO_CAMBIO, dia) or next(
                    (Decimal(o.cambio) for o in reversed(antes) if o.cambio), None)
                if taxa is None:
                    continue
                total *= taxa
            if a.classe == "exterior":
                total *= LIQUIDO_EXTERIOR
            recebidos.append((dia, -total))
    return recebidos


def evolucao_mensal(s: Session, ate: date, alocacao: str | None = None, carteira=None) -> list[PontoMes]:
    """Série mensal desde o mês da primeira operação até o mês de `ate`; com `alocacao`, só os ativos dela;
    com `carteira`, só as operações dela."""
    ops_por = operacoes_por_ativo(s, ate, carteira=carteira)
    ativos = {a.id: a for a in s.scalars(select(Ativo).where(Ativo.id.in_(list(ops_por))))}
    if alocacao is not None:
        ops_por = {aid: ops for aid, ops in ops_por.items() if ativos[aid].alocacao == alocacao}
    if not ops_por:
        return []
    cdi, ipca = series_indicadores(s)
    fech = _Fechamentos(s)
    cambio_hoje = cambio_atual(s)
    primeira = min(ops[0].data for ops in ops_por.values())
    meses = _meses(primeira, ate)
    pontos_data = [ate if (a, m) == (ate.year, ate.month) else _fim_do_mes(a, m) for a, m in meses]
    inicio_base = primeira.replace(day=1) - timedelta(days=1)  # último dia do mês anterior ao da primeira operação

    fluxos: list[tuple[date, Decimal]] = []
    for aid, ops in ops_por.items():
        fluxos += _fluxos(ativos[aid], ops, cdi, ipca)
    proventos = _proventos(s, ativos, ops_por, inicio_base, ate, cdi, ipca, fech)

    def patrimonio(dia: date) -> int:
        total = 0
        for aid, ops in ops_por.items():
            a = ativos[aid]
            ate_dia = [o for o in ops if o.data <= dia]
            if not ate_dia:
                continue
            pos = posicao_do_ativo(a, ate_dia, cdi, ipca, dia)
            mercado = preco_de_mercado(s, a, dia) if a.classe == "renda_fixa" and a.por_titulos else None
            if mercado is not None:
                total += _centavo(pos.quantidade * mercado.pu_venda)
            elif a.classe == "renda_fixa":
                total += valor_atualizado(a, ate_dia, cdi, ipca, dia)
            elif pos.quantidade == 0:
                continue
            else:
                total += _valor_posicao(a, pos, ate_dia, dia)
        return total

    def _valor_posicao(a: Ativo, pos, ops_dia: list, dia: date) -> int:
        if dia == ate:
            preco, *_ = _cotacao_brl(s, a, cambio_hoje, ops_dia)
            if preco is not None:
                return _centavo(pos.quantidade * preco)
        fechamento = fech.ate(a.simbolo_cotacao, dia) if a.simbolo_cotacao else None
        if fechamento is None:
            return pos.custo_brl
        preco = fechamento * CEM
        if moeda_cotacao(a) == "USD":
            taxa = fech.ate(SIMBOLO_CAMBIO, dia) or next(
                (Decimal(o.cambio) for o in reversed(ops_dia) if o.cambio), None)
            if taxa is None:
                return pos.custo_brl
            preco *= taxa
        return _centavo(pos.quantidade * preco)

    base_ibov = fech.ate(SIMBOLO_IBOV, inicio_base) or fech.primeiro(SIMBOLO_IBOV)

    def global_em_reais(dia: date) -> Decimal | None:
        vt, dolar = fech.ate(SIMBOLO_GLOBAL, dia), fech.ate(SIMBOLO_CAMBIO, dia)
        return vt * dolar if vt is not None and dolar is not None else None

    base_global = global_em_reais(inicio_base)
    pontos: list[PontoMes] = []
    anterior_dia, anterior_patr = inicio_base, 0
    acumulado = UM
    cdi_fator = UM
    cdi_dias = sorted(cdi)
    for (a, m), dia in zip(meses, pontos_data):
        v_fim = patrimonio(dia)
        do_mes = [(d, f) for d, f in (*fluxos, *proventos) if anterior_dia < d <= dia]
        f_total = sum((f for _, f in do_mes), Decimal(0))
        duracao = (dia - anterior_dia).days
        ponderado = sum((f * Decimal((dia - d).days) / duracao for d, f in do_mes), Decimal(0)) if duracao else Decimal(0)
        base = Decimal(anterior_patr) + ponderado
        r = (Decimal(v_fim) - Decimal(anterior_patr) - f_total) / base if base > 0 else Decimal(0)
        acumulado *= UM + r
        for d in cdi_dias:
            if anterior_dia < d <= dia:
                cdi_fator *= UM + cdi[d] / CEM
        ibov = fech.ate(SIMBOLO_IBOV, dia)
        mundo = global_em_reais(dia)
        aplicado = sum((f for d, f in fluxos if d <= dia), Decimal(0))
        pontos.append(PontoMes(
            competencia=f"{a:04d}-{m:02d}", patrimonio=v_fim, aplicado_acumulado=_centavo(aplicado),
            rent_mes_pct=r * CEM, rent_acum_pct=(acumulado - UM) * CEM,
            cdi_acum_pct=(cdi_fator - UM) * CEM if cdi else None,
            ibov_acum_pct=(ibov / base_ibov - UM) * CEM if ibov is not None and base_ibov else None,
            global_acum_pct=(mundo / base_global - UM) * CEM if mundo is not None and base_global else None,
        ))
        anterior_dia, anterior_patr = dia, v_fim
    return pontos
