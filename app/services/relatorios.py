"""Séries e agregações das telas de relatórios (spec §7)."""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import somar_meses, ultimo_dia
from app.models import Categoria, Conta, Lancamento
from app.services.mes_caixa import com_status, entre_meses, mes_caixa, no_mes

MESES_SERIE = 12
PROJETADO = ("efetivado", "pendente")


def janela(fim: str, n: int = MESES_SERIE) -> list[str]:
    return [somar_meses(fim, i - (n - 1)) for i in range(n)]


@dataclass(frozen=True)
class PontoReceitaDespesa:
    competencia: str
    receitas: int
    despesas: int


def receitas_despesas(s: Session, fim: str, n: int = MESES_SERIE) -> list[PontoReceitaDespesa]:
    meses = janela(fim, n)
    mes = mes_caixa()
    linhas = s.execute(
        select(mes, Lancamento.tipo, func.sum(Lancamento.valor))
        .where(Lancamento.tipo.in_(("receita", "despesa")), com_status(PROJETADO),
               Lancamento.ignorar.is_(False), entre_meses(meses[0], meses[-1]))
        .group_by(mes, Lancamento.tipo)
    ).all()
    soma = {(m, t): int(v) for m, t, v in linhas}
    return [PontoReceitaDespesa(m, soma.get((m, "receita"), 0), -soma.get((m, "despesa"), 0)) for m in meses]


@dataclass(frozen=True)
class PontoFixosVariaveis:
    competencia: str
    fixos: int
    variaveis: int


def fixos_variaveis(s: Session, fim: str, n: int = MESES_SERIE) -> list[PontoFixosVariaveis]:
    """Despesas fixas e variáveis; dívidas ficam de fora e sem categoria contam como variáveis."""
    meses = janela(fim, n)
    mes = mes_caixa()
    linhas = s.execute(
        select(mes, Categoria.grupo, func.sum(Lancamento.valor))
        .outerjoin(Categoria, Lancamento.categoria_id == Categoria.id)
        .where(Lancamento.tipo == "despesa", com_status(PROJETADO), Lancamento.ignorar.is_(False),
               entre_meses(meses[0], meses[-1]))
        .group_by(mes, Categoria.grupo)
    ).all()
    fixos: dict[str, int] = {}
    variaveis: dict[str, int] = {}
    for m, grupo, valor in linhas:
        if grupo == "divida":
            continue
        alvo = fixos if grupo == "fixo" else variaveis
        alvo[m] = alvo.get(m, 0) - int(valor)
    return [PontoFixosVariaveis(m, fixos.get(m, 0), variaveis.get(m, 0)) for m in meses]


@dataclass(frozen=True)
class PontoSaldo:
    competencia: str
    saldo: int


def saldo_liquido_mensal(s: Session, fim: str, n: int = MESES_SERIE) -> list[PontoSaldo]:
    """Saldo líquido (contas que entram no líquido, sem cartão) ao fim de cada mês, só efetivado."""
    contas = list(s.scalars(select(Conta).where(Conta.ativa, Conta.entra_saldo_liquido, Conta.tipo != "cartao")))
    pontos = []
    for m in janela(fim, n):
        limite = ultimo_dia(m)
        total = 0
        for conta in contas:
            if conta.data_saldo_inicial > limite:
                continue
            total += conta.saldo_inicial + int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
                Lancamento.conta_id == conta.id, Lancamento.status == "efetivado", Lancamento.ignorar.is_(False),
                Lancamento.data >= conta.data_saldo_inicial, Lancamento.data <= limite)))
        pontos.append(PontoSaldo(m, total))
    return pontos


@dataclass(frozen=True)
class NoCategoria:
    categoria_id: int | None
    nome: str
    cor: str
    total: int
    filhas: list[tuple[str, int]]


def despesas_hierarquia(s: Session, competencia: str) -> list[NoCategoria]:
    """Despesas do mês (projetado) por categoria-raiz, com o detalhe das subcategorias."""
    linhas = s.execute(
        select(Lancamento.categoria_id, func.sum(Lancamento.valor))
        .where(Lancamento.tipo == "despesa", com_status(PROJETADO), Lancamento.ignorar.is_(False),
               no_mes(competencia))
        .group_by(Lancamento.categoria_id)
    ).all()
    categorias = {c.id: c for c in s.scalars(select(Categoria))}
    totais: dict[int | None, int] = {}
    filhas: dict[int | None, dict[str, int]] = {}
    for cid, soma in linhas:
        valor = -int(soma)
        cat = categorias.get(cid)
        raiz = cat.pai_id if cat is not None and cat.pai_id is not None else cid
        totais[raiz] = totais.get(raiz, 0) + valor
        if cat is not None and cat.pai_id is not None:
            filhas.setdefault(raiz, {})[cat.nome] = filhas.get(raiz, {}).get(cat.nome, 0) + valor
    nos = []
    for raiz, total in totais.items():
        if total <= 0:
            continue
        cat = categorias.get(raiz)
        nos.append(NoCategoria(raiz, cat.nome if cat else "Sem categoria", cat.cor if cat else "#9ca3af", total,
                               sorted(filhas.get(raiz, {}).items(), key=lambda x: -x[1])))
    return sorted(nos, key=lambda n: -n.total)
