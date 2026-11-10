"""Em que mês cada lançamento pesa nos totais (dashboard, planejamento, orçamento e relatórios).

Compra no cartão conta no mês em que a fatura vence, e só fica efetivada quando a fatura é paga:
é o dinheiro que sai, igual à tela de Lançamentos. O resto conta na própria data e no próprio status."""
from sqlalchemy import case, func, select

from app.core.periodo import primeiro_dia, ultimo_dia
from app.models import Fatura, Lancamento


def _da_fatura(coluna):
    return (select(coluna)
            .where(Fatura.id == Lancamento.fatura_id, Fatura.cartao_id == Lancamento.conta_id)
            .correlate(Lancamento).scalar_subquery())


def data_caixa():
    """Data em que o lançamento conta: o vencimento da fatura para compra no cartão."""
    return func.coalesce(_da_fatura(Fatura.data_vencimento), Lancamento.data)


def status_caixa():
    """Status para os totais: compra no cartão é efetivada só com a fatura paga."""
    fatura = _da_fatura(Fatura.status)
    return case(
        (Lancamento.fatura_id.is_(None), Lancamento.status),
        (Lancamento.status == "cancelado", Lancamento.status),
        (fatura.is_(None), Lancamento.status),
        (fatura == "paga", "efetivado"),
        else_="pendente",
    )


def no_mes(competencia: str):
    return data_caixa().between(primeiro_dia(competencia), ultimo_dia(competencia))


def entre_meses(inicio: str, fim: str):
    return data_caixa().between(primeiro_dia(inicio), ultimo_dia(fim))


def com_status(status: tuple[str, ...]):
    return status_caixa().in_(status)


def mes_caixa():
    """'AAAA-MM' do mês em que o lançamento conta, para agrupar séries."""
    return func.strftime("%Y-%m", data_caixa())
