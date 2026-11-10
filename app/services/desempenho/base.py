"""Base de agregações e períodos da tela de Desempenho.

Regras unificadas:
- Competência pelo mês de caixa (`data_caixa`).
- Lançamentos com `meta_id` (aportes/resgates de metas) são computados como `guardado` e NÃO entram em receitas nem despesas.
- Janela de histórico sem meses vazios anteriores ao início real dos lançamentos.
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, primeiro_dia, somar_meses, ultimo_dia
from app.models import Categoria, Lancamento
from app.services.mes_caixa import com_status, entre_meses, mes_caixa, no_mes

PROJETADO = ("efetivado", "pendente")
EFETIVADO = ("efetivado",)


def janela_historico(s: Session, fim: str, n: int = 12) -> list[str]:
    """Lista de competências ['AAAA-MM', ...] terminando em `fim`.
    Descarta meses anteriores ao primeiro mês com lançamento real no banco."""
    primeira_data = s.scalar(
        select(func.min(Lancamento.data)).where(
            Lancamento.ignorar.is_(False),
            Lancamento.status != "cancelado",
        )
    )
    if primeira_data is None:
        return [fim]
    comp_min = competencia_de(primeira_data)
    inicio_ideal = somar_meses(fim, -(n - 1))
    inicio = max(inicio_ideal, comp_min)
    if inicio > fim:
        return [fim]
    meses: list[str] = []
    atual = inicio
    while atual <= fim:
        meses.append(atual)
        atual = somar_meses(atual, 1)
    return meses


def meses_media_referencia(s: Session, mes: str, n: int = 3) -> list[str]:
    """Até `n` meses completos imediatamente anteriores a `mes` que possuam dados."""
    candidatos: list[str] = []
    cursor = somar_meses(mes, -1)
    # Busca até 24 meses para trás
    limite = somar_meses(mes, -24)
    while cursor >= limite and len(candidatos) < n:
        tem_dados = s.scalar(
            select(func.count(Lancamento.id)).where(
                Lancamento.ignorar.is_(False),
                Lancamento.status != "cancelado",
                Lancamento.tipo.in_(("receita", "despesa")),
                no_mes(cursor),
            )
        )
        if tem_dados:
            candidatos.append(cursor)
        cursor = somar_meses(cursor, -1)
    candidatos.reverse()
    return candidatos


@dataclass(frozen=True)
class TotaisMensais:
    competencia: str
    receitas_efetivado: int
    receitas_projetado: int
    despesas_efetivado: int
    despesas_projetado: int
    guardado_efetivado: int
    guardado_projetado: int
    fixos_projetado: int
    variaveis_projetado: int
    dividas_projetado: int

    @property
    def sobra_efetivado(self) -> int:
        return self.receitas_efetivado - self.despesas_efetivado

    @property
    def sobra_projetado(self) -> int:
        return self.receitas_projetado - self.despesas_projetado

    @property
    def taxa_poupanca_efetivado(self) -> float | None:
        if self.receitas_efetivado <= 0:
            return None
        return round((self.sobra_efetivado + self.guardado_efetivado) * 100 / self.receitas_efetivado, 1)

    @property
    def taxa_poupanca_projetado(self) -> float | None:
        if self.receitas_projetado <= 0:
            return None
        return round((self.sobra_projetado + self.guardado_projetado) * 100 / self.receitas_projetado, 1)


def carregar_totais_periodo(s: Session, inicio: str, fim: str) -> dict[str, TotaisMensais]:
    """Carrega os totais agregados mês a mês no período especificado em uma única consulta agrupada."""
    mes_col = mes_caixa()
    # 1. Lançamentos normais (sem meta_id)
    linhas = s.execute(
        select(
            mes_col,
            Lancamento.tipo,
            Categoria.grupo,
            Lancamento.status,
            func.sum(Lancamento.valor),
        )
        .outerjoin(Categoria, Lancamento.categoria_id == Categoria.id)
        .where(
            Lancamento.ignorar.is_(False),
            Lancamento.status.in_(PROJETADO),
            Lancamento.meta_id.is_(None),
            Lancamento.tipo.in_(("receita", "despesa")),
            entre_meses(inicio, fim),
        )
        .group_by(mes_col, Lancamento.tipo, Categoria.grupo, Lancamento.status)
    ).all()

    # 2. Guardado (lançamentos com meta_id)
    linhas_metas = s.execute(
        select(
            mes_col,
            Lancamento.status,
            func.sum(Lancamento.valor),
        )
        .where(
            Lancamento.ignorar.is_(False),
            Lancamento.status.in_(PROJETADO),
            Lancamento.meta_id.is_not(None),
            Lancamento.tipo.in_(("receita", "despesa")),
            entre_meses(inicio, fim),
        )
        .group_by(mes_col, Lancamento.status)
    ).all()

    # Mapeamentos intermediários
    dados_mes: dict[str, dict] = {}

    def _get(m: str):
        if m not in dados_mes:
            dados_mes[m] = {
                "rec_ef": 0, "rec_pr": 0,
                "desp_ef": 0, "desp_pr": 0,
                "guard_ef": 0, "guard_pr": 0,
                "fixos": 0, "variaveis": 0, "dividas": 0,
            }
        return dados_mes[m]

    for m, tipo, grupo, status, valor in linhas:
        val = int(valor)
        d = _get(m)
        if tipo == "receita":
            d["rec_pr"] += val
            if status == "efetivado":
                d["rec_ef"] += val
        elif tipo == "despesa":
            desp_abs = -val
            d["desp_pr"] += desp_abs
            if status == "efetivado":
                d["desp_ef"] += desp_abs
            # Divisão por grupo para despesas projetadas
            grp = grupo if grupo in ("fixo", "divida") else "variavel"
            if grp == "fixo":
                d["fixos"] += desp_abs
            elif grp == "divida":
                d["dividas"] += desp_abs
            else:
                d["variaveis"] += desp_abs

    for m, status, valor in linhas_metas:
        # valor negativo para despesa (dinheiro guardado), positivo para resgate
        guardado = -int(valor)
        d = _get(m)
        d["guard_pr"] += guardado
        if status == "efetivado":
            d["guard_ef"] += guardado

    # Monta resultado final
    resultado = {}
    atual = inicio
    while atual <= fim:
        d = _get(atual)
        resultado[atual] = TotaisMensais(
            competencia=atual,
            receitas_efetivado=d["rec_ef"],
            receitas_projetado=d["rec_pr"],
            despesas_efetivado=d["desp_ef"],
            despesas_projetado=d["desp_pr"],
            guardado_efetivado=d["guard_ef"],
            guardado_projetado=d["guard_pr"],
            fixos_projetado=d["fixos"],
            variaveis_projetado=d["variaveis"],
            dividas_projetado=d["dividas"],
        )
        atual = somar_meses(atual, 1)

    return resultado


def totais_do_mes(s: Session, competencia: str) -> TotaisMensais:
    return carregar_totais_periodo(s, competencia, competencia)[competencia]
