from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Categoria, Conta, Fatura, Lancamento
from app.services.faturas import total_fatura, total_pago
from app.services.mes_caixa import com_status, no_mes

EFETIVADO = ("efetivado",)
PROJETADO = ("efetivado", "pendente")
COR_SEM_CATEGORIA = "#9ca3af"


@dataclass(frozen=True)
class TotaisMes:
    receitas_efetivado: int
    receitas_projetado: int
    despesas_efetivado: int
    despesas_projetado: int

    @property
    def balanco_efetivado(self) -> int:
        return self.receitas_efetivado - self.despesas_efetivado

    @property
    def balanco_projetado(self) -> int:
        return self.receitas_projetado - self.despesas_projetado


def _soma_tipo(s: Session, competencia: str, tipo: str, status: tuple[str, ...]) -> int:
    consulta = select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.tipo == tipo,
        no_mes(competencia),
        com_status(status),
        Lancamento.ignorar.is_(False),
    )
    return int(s.scalar(consulta))


def totais_mes(s: Session, competencia: str) -> TotaisMes:
    return TotaisMes(
        receitas_efetivado=_soma_tipo(s, competencia, "receita", EFETIVADO),
        receitas_projetado=_soma_tipo(s, competencia, "receita", PROJETADO),
        despesas_efetivado=-_soma_tipo(s, competencia, "despesa", EFETIVADO),
        despesas_projetado=-_soma_tipo(s, competencia, "despesa", PROJETADO),
    )


@dataclass(frozen=True)
class FatiaCategoria:
    categoria_id: int | None
    nome: str
    cor: str
    total: int


def despesas_por_categoria(s: Session, competencia: str, *, projetado: bool = True) -> list[FatiaCategoria]:
    linhas = s.execute(
        select(Lancamento.categoria_id, func.sum(Lancamento.valor))
        .where(
            Lancamento.tipo == "despesa",
            no_mes(competencia),
            com_status(PROJETADO if projetado else EFETIVADO),
            Lancamento.ignorar.is_(False),
        )
        .group_by(Lancamento.categoria_id)
    ).all()
    categorias = {c.id: c for c in s.scalars(select(Categoria))}
    acumulado: dict[int | None, int] = {}
    for categoria_id, soma in linhas:
        raiz = categoria_id
        if categoria_id is not None and categorias[categoria_id].pai_id is not None:
            raiz = categorias[categoria_id].pai_id
        acumulado[raiz] = acumulado.get(raiz, 0) - int(soma)
    fatias = [
        FatiaCategoria(
            categoria_id=cid,
            nome=categorias[cid].nome if cid is not None else "Sem categoria",
            cor=categorias[cid].cor if cid is not None else COR_SEM_CATEGORIA,
            total=total,
        )
        for cid, total in acumulado.items()
        if total > 0
    ]
    return sorted(fatias, key=lambda f: f.total, reverse=True)


@dataclass(frozen=True)
class ItemAPagar:
    data: date
    descricao: str
    valor: int
    conta_nome: str
    atrasado: bool
    lancamento_id: int | None = None
    fatura_id: int | None = None


def a_pagar(s: Session, hoje: date, dias: int = 7) -> list[ItemAPagar]:
    limite = hoje + timedelta(days=dias)
    itens: list[ItemAPagar] = []
    pendentes = s.execute(
        select(Lancamento, Conta)
        .join(Conta, Lancamento.conta_id == Conta.id)
        .where(
            Lancamento.status == "pendente",
            Lancamento.ignorar.is_(False),
            Lancamento.valor < 0,
            Lancamento.tipo.in_(("despesa", "pagamento_fatura")),
            Conta.tipo != "cartao",
            Lancamento.data <= limite,
        )
    ).all()
    faturas_com_pagamento_pendente = set()
    for lanc, conta in pendentes:
        if lanc.tipo == "pagamento_fatura":
            faturas_com_pagamento_pendente.add(lanc.fatura_id)
        itens.append(ItemAPagar(lanc.data, lanc.descricao, -lanc.valor, conta.nome,
                                lanc.data < hoje, lancamento_id=lanc.id))
    faturas = s.execute(
        select(Fatura, Conta)
        .join(Conta, Fatura.cartao_id == Conta.id)
        .where(Fatura.status != "paga", Fatura.data_vencimento <= limite)
    ).all()
    for fatura, cartao in faturas:
        if fatura.id in faturas_com_pagamento_pendente:
            continue
        restante = total_fatura(s, fatura) - total_pago(s, fatura)
        if restante > 0:
            itens.append(ItemAPagar(fatura.data_vencimento, f"Fatura {cartao.nome}", restante, cartao.nome,
                                    fatura.data_vencimento < hoje, fatura_id=fatura.id))
    return sorted(itens, key=lambda i: i.data)
