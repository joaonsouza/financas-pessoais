"""Reserva de emergência inteligente: o ritmo do mês, a projeção de consumo e o plano para chegar no alvo.

Tudo parte da sobra mensal (receitas − despesas efetivadas, sem contar o que foi guardado em metas):
negativa, a reserva está sendo consumida (projeção e alerta); positiva ou com aporte programado, vira o ritmo
das datas dos degraus. Valores em centavos.
"""
from dataclasses import dataclass
from datetime import date
from math import ceil, floor

from sqlalchemy.orm import Session

from app.core.periodo import NOMES_MESES, competencia_de, somar_meses
from app.models import Meta
from app.services.metas import aporte_mensal, aportes_do_mes, reserva
from app.services.resumo_mes import totais_mes

MESES_RITMO = 3
SUGESTOES_DESEJOS = 2
PRAZOS_SUGERIDOS = (12, 24)


@dataclass(frozen=True)
class Ritmo:
    sobra: int
    base: str
    meses: list[str]  # competências que entraram na média (vazio = previsto do mês atual)


@dataclass(frozen=True)
class Projecao:
    deficit: int
    meses_ate_acabar: float
    acaba_em: str | None  # "mar/2027"; None quando a reserva não cobre nem um mês de déficit
    cortar: int
    sugestoes: list[tuple[str, int, int]]  # (nome, categoria_id, valor): os maiores gastos em Desejos
    base: str


@dataclass(frozen=True)
class DegrauPlano:
    rotulo: str
    valor: int
    data_estimada: str | None


@dataclass(frozen=True)
class Plano:
    ritmo: int
    base: str  # de onde veio o ritmo: "aporte programado" ou a base da sobra
    degraus: list[DegrauPlano]  # só os que ainda faltam
    sugestao_12: int
    sugestao_24: int
    primeiro_parar: bool


def _curto(competencia: str) -> str:
    return NOMES_MESES[int(competencia[5:]) - 1][:3].lower()


def mes_ano(competencia: str) -> str:
    """"2027-03" -> "mar/2027"."""
    return f"{_curto(competencia)}/{competencia[:4]}"


def _sobra_do_mes(s: Session, competencia: str) -> int | None:
    """Balanço efetivado do mês sem os aportes a metas (que são dinheiro guardado, não gasto); None sem lançamento."""
    totais = totais_mes(s, competencia)
    if not (totais.receitas_efetivado or totais.despesas_efetivado):
        return None
    return totais.balanco_efetivado + aportes_do_mes(s, competencia, ("efetivado",))


def ritmo_mensal(s: Session, hoje: date) -> Ritmo:
    atual = competencia_de(hoje)
    sobras: dict[str, int] = {}
    for i in range(MESES_RITMO, 0, -1):
        comp = somar_meses(atual, -i)
        sobra = _sobra_do_mes(s, comp)
        if sobra is not None:
            sobras[comp] = sobra
    if not sobras:
        previsto = totais_mes(s, atual).balanco_projetado + aportes_do_mes(s, atual, ("efetivado", "pendente"))
        return Ritmo(previsto, "previsto deste mês", [])
    meses = list(sobras)
    base = NOMES_MESES[int(meses[0][5:]) - 1].lower() if len(meses) == 1 else f"média de {_curto(meses[0])}–{_curto(meses[-1])}"
    return Ritmo(sum(sobras.values()) // len(meses), base, meses)


def _sugestoes_desejos(s: Session, competencia: str) -> list[tuple[str, int, int]]:
    """As categorias-raiz de Desejos (a mesma classificação do 50/30/20) com maior gasto projetado no mês."""
    from app.services.indicadores_curso import _carregar_mes, _natureza
    mes = _carregar_mes(s, competencia)
    por_raiz: dict[int, int] = {}
    for cid, valor in mes.por_categoria.items():
        raiz = mes.raiz(cid)
        if raiz is not None and _natureza(mes.categoria(cid)) == "desejo" and valor > 0:
            por_raiz[raiz.id] = por_raiz.get(raiz.id, 0) + valor
    maiores = sorted(por_raiz.items(), key=lambda x: (-x[1], mes.categorias[x[0]].nome))[:SUGESTOES_DESEJOS]
    return [(mes.categorias[cid].nome, cid, valor) for cid, valor in maiores]


def projecao_reserva(s: Session, meta: Meta, hoje: date) -> Projecao | None:
    """Quando a reserva acaba no ritmo atual; None se o mês fecha no azul (ou no zero)."""
    ritmo = ritmo_mensal(s, hoje)
    if ritmo.sobra >= 0:
        return None
    deficit = -ritmo.sobra
    atual = reserva(s, meta, hoje).atual
    meses = atual / deficit
    acaba_em = mes_ano(somar_meses(competencia_de(hoje), floor(meses))) if atual >= deficit else None
    return Projecao(deficit, round(meses, 1), acaba_em, deficit, _sugestoes_desejos(s, competencia_de(hoje)),
                    ritmo.base)


def plano_reserva(s: Session, meta: Meta, hoje: date) -> Plano:
    """Datas estimadas dos degraus que faltam e quanto guardar por mês para fechar o alvo em 12 ou 24 meses."""
    r = reserva(s, meta, hoje)
    ritmo = ritmo_mensal(s, hoje)
    aporte = aporte_mensal(s, meta, hoje)
    valor_ritmo = (aporte or 0) if aporte is not None else max(ritmo.sobra, 0)
    atual = competencia_de(hoje)
    degraus = [
        DegrauPlano(d.rotulo, d.valor, mes_ano(somar_meses(atual, ceil((d.valor - r.atual) / valor_ritmo)))
                    if valor_ritmo > 0 else None)
        for d in r.degraus if not d.atingido
    ]
    falta = max(r.alvo - r.atual, 0)
    return Plano(valor_ritmo, "aporte programado" if aporte is not None else ritmo.base, degraus,
                 *(ceil(falta / n) for n in PRAZOS_SUGERIDOS), ritmo.sobra < 0 and not aporte)
