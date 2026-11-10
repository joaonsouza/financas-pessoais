from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import ultimo_dia
from app.models import Conta, Fatura, Lancamento, Recorrencia
from app.services.faturas import total_fatura
from app.services.ordem import criterio_ordem


@dataclass(frozen=True)
class SaldoConta:
    conta_id: int
    nome: str
    tipo: str
    cor: str
    entra_saldo_liquido: bool
    atual: int
    previsto_dia: int
    previsto_mes: int
    # atual/previstos em reais (conta em outra moeda: convertida pela última cotação); atual_moeda na moeda da conta
    moeda: str = "BRL"
    atual_moeda: int = 0
    sem_cotacao: bool = False


def _soma(s: Session, conta: Conta, *filtros) -> int:
    consulta = select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.conta_id == conta.id,
        Lancamento.data >= conta.data_saldo_inicial,
        Lancamento.ignorar.is_(False),
        *filtros,
    )
    return int(s.scalar(consulta))


def saldo_atual(s: Session, conta: Conta) -> int:
    return conta.saldo_inicial + _soma(s, conta, Lancamento.status == "efetivado")


def saldo_previsto(s: Session, conta: Conta, ate: date) -> int:
    pendentes = _soma(s, conta, Lancamento.status == "pendente", Lancamento.data <= ate)
    return saldo_atual(s, conta) + pendentes + aportes_a_receber(s, ate).get(conta.id, 0)


def aportes_a_receber(s: Session, ate: date) -> dict[int, int]:
    """Aportes de meta "como despesa" ainda pendentes, por conta da meta: enquanto pendentes são despesa na
    conta de origem e a entrada na conta da meta ainda não existe (nasce ao efetivar). Sem isto o previsto da
    conta da meta ignoraria o que vai chegar."""
    linhas = s.execute(
        select(Recorrencia.conta_destino_id, func.coalesce(func.sum(Lancamento.valor), 0))
        .join(Recorrencia, Lancamento.recorrencia_id == Recorrencia.id)
        .where(Recorrencia.origem_meta_id.is_not(None), Recorrencia.tipo == "despesa",
               Recorrencia.conta_destino_id.is_not(None), Lancamento.tipo == "despesa",
               Lancamento.status == "pendente", Lancamento.ignorar.is_(False), Lancamento.data <= ate)
        .group_by(Recorrencia.conta_destino_id)
    ).all()
    return {conta_id: -int(soma) for conta_id, soma in linhas}


def faturas_a_pagar(s: Session, ate: date) -> dict[int, int]:
    """O que falta pagar das faturas que vencem até `ate` (inclusive as vencidas), por conta pagadora.
    As compras do cartão não ficam nas contas: sem isto o previsto ignoraria as faturas.
    Desconta o já pago e o pagamento agendado (este já entra no previsto como pendente)."""
    pagadoras = dict(s.execute(select(Conta.id, Conta.conta_pagamento_id).where(Conta.tipo == "cartao")).all())
    devido: dict[int, int] = defaultdict(int)
    for fatura in s.scalars(select(Fatura).where(Fatura.status != "paga", Fatura.data_vencimento <= ate)):
        conta_id = pagadoras.get(fatura.cartao_id)
        if not conta_id:
            continue
        pagos = s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
            Lancamento.fatura_id == fatura.id, Lancamento.conta_id == fatura.cartao_id,
            Lancamento.tipo == "pagamento_fatura", Lancamento.status != "cancelado", Lancamento.ignorar.is_(False)))
        falta = total_fatura(s, fatura) - int(pagos)
        if falta > 0:
            devido[conta_id] += falta
    return devido


def cambio_brl(s: Session, moeda: str) -> Decimal | None:
    """Reais por unidade da moeda pela última cotação guardada (sem rede); BRL é 1."""
    if moeda == "BRL":
        return Decimal(1)
    # import aqui: invest_carteira → invest_conta → lancamentos → saldos
    from app.services.invest_carteira import cambio_atual
    return cambio_atual(s) if moeda == "USD" else None


def saldos_contas(s: Session, hoje: date, competencia: str, *,
                  tipos: tuple[str, ...] | None = None) -> list[SaldoConta]:
    # na ordem escolhida em Contas; sem ordem, na de criação
    consulta = select(Conta).where(Conta.ativa).order_by(*criterio_ordem(Conta.id))
    if tipos:
        consulta = consulta.where(Conta.tipo.in_(tipos))
    fim_mes = ultimo_dia(competencia)
    faturas_dia, faturas_mes = faturas_a_pagar(s, hoje), faturas_a_pagar(s, fim_mes)
    aportes_dia, aportes_mes = aportes_a_receber(s, hoje), aportes_a_receber(s, fim_mes)
    resultado = []
    for conta in s.scalars(consulta):
        atual = saldo_atual(s, conta)
        cambio = cambio_brl(s, conta.moeda)

        def em_reais(valor: int) -> int:
            return 0 if cambio is None else int((Decimal(valor) * cambio).quantize(Decimal(1), ROUND_HALF_UP))

        resultado.append(SaldoConta(
            conta_id=conta.id,
            nome=conta.nome,
            tipo=conta.tipo,
            cor=conta.cor,
            entra_saldo_liquido=conta.entra_saldo_liquido,
            atual=em_reais(atual),
            previsto_dia=em_reais(atual + _soma(s, conta, Lancamento.status == "pendente", Lancamento.data <= hoje)
                                  - faturas_dia.get(conta.id, 0) + aportes_dia.get(conta.id, 0)),
            previsto_mes=em_reais(atual + _soma(s, conta, Lancamento.status == "pendente", Lancamento.data <= fim_mes)
                                  - faturas_mes.get(conta.id, 0) + aportes_mes.get(conta.id, 0)),
            moeda=conta.moeda,
            atual_moeda=atual,
            sem_cotacao=cambio is None,
        ))
    return resultado


def saldo_liquido(saldos: list[SaldoConta], campo: str = "atual") -> int:
    return sum(getattr(x, campo) for x in saldos if x.entra_saldo_liquido)


def saldo_liquido_em(s: Session, dia: date) -> int:
    """Saldo líquido previsto ao fim de `dia`: contas do saldo líquido com tudo até o dia (efetivado ou
    pendente) e as faturas que vencem até o dia ainda por pagar."""
    contas = list(s.scalars(select(Conta).where(Conta.ativa, Conta.entra_saldo_liquido, Conta.tipo != "cartao")))
    faturas, aportes = faturas_a_pagar(s, dia), aportes_a_receber(s, dia)
    total = 0
    for conta in contas:
        total += conta.saldo_inicial + _soma(s, conta, Lancamento.status != "cancelado", Lancamento.data <= dia)
        total += aportes.get(conta.id, 0) - faturas.get(conta.id, 0)
    return total
