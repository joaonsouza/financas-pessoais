"""Visão das parcelas futuras do cartão: quanto falta, mês a mês, contra um teto mensal editável."""
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, somar_meses
from app.models import Conta, Fatura, Lancamento
from app.models.config import get_config, set_config
from app.services.mes_caixa import com_status, no_mes

TETO_PADRAO = 200000  # R$ 2.000,00 em centavos
CHAVE_TETO = "teto_parcelas"


@dataclass
class MesParcelas:
    competencia: str
    total: int
    qtd: int
    acima: bool
    passado: bool = False


@dataclass
class CompraParcelada:
    grupo: str
    descricao: str
    cartao_id: int
    cartao: str
    proxima_num: int | None
    total_parcelas: int | None
    valor_parcela: int
    restantes: int
    falta: int
    ultimo_mes: str


@dataclass
class Sobra:
    id: int
    descricao: str
    cartao: str
    data: date
    valor: int


@dataclass
class VisaoParcelas:
    teto: int
    total_a_pagar: int = 0
    qtd_compras: int = 0
    meses: list[MesParcelas] = field(default_factory=list)
    pico: MesParcelas | None = None
    meses_acima: int = 0
    compras: list[CompraParcelada] = field(default_factory=list)
    sobras: list[Sobra] = field(default_factory=list)


def teto_parcelas(s: Session) -> int:
    return int(get_config(s, CHAVE_TETO, TETO_PADRAO))


def definir_teto_parcelas(s: Session, centavos: int) -> None:
    if centavos < 0:
        raise ValueError("O teto não pode ser negativo.")
    set_config(s, CHAVE_TETO, int(centavos))


def _lancadas(s: Session, cartao_id: int | None):
    """Toda parcela de cartão ativo (pendente e efetivada): (lançamento, competência da fatura, vencimento, cartão)."""
    consulta = (
        select(Lancamento, Fatura.competencia, Fatura.data_vencimento, Conta.nome)
        .join(Conta, Conta.id == Lancamento.conta_id)
        .outerjoin(Fatura, Fatura.id == Lancamento.fatura_id)
        .where(Conta.tipo == "cartao", Conta.ativa, Lancamento.grupo_parcela_id.is_not(None),
               Lancamento.status != "cancelado", Lancamento.valor < 0, Lancamento.ignorar.is_(False))
        .order_by(Lancamento.data, Lancamento.parcela_num, Lancamento.id)
    )
    if cartao_id is not None:
        consulta = consulta.where(Lancamento.conta_id == cartao_id)
    # competência da fatura (mês em que ela fecha); sem fatura, o mês da data do lançamento
    return [(lanc, comp or competencia_de(lanc.data), venc, cartao) for lanc, comp, venc, cartao in s.execute(consulta)]


def visao_parcelas(s: Session, hoje: date, cartao_id: int | None = None, mes_ref: str | None = None) -> VisaoParcelas:
    """Dois recortes.

    Totais de "o que falta pagar" (total_a_pagar, compras, sobras): só parcela pendente futura, isto é, com
    vencimento da fatura (ou data, sem fatura) >= hoje; pendente com data anterior é sobra.
    A curva mensal (meses) soma todas as parcelas, pendentes e já lançadas, pela competência da fatura.
    """
    teto = teto_parcelas(s)
    todas = _lancadas(s, cartao_id)
    futuras: list[tuple[Lancamento, str, str]] = []
    sobras: list[Sobra] = []
    for lanc, comp, vencimento, cartao in todas:
        if lanc.status != "pendente":
            continue
        referencia = vencimento or lanc.data  # vencimento da fatura; sem fatura, a data do lançamento
        if referencia < hoje:
            sobras.append(Sobra(lanc.id, lanc.descricao, cartao, lanc.data, lanc.valor))
        else:
            futuras.append((lanc, comp, cartao))
    visao = VisaoParcelas(teto=teto, sobras=sobras)
    if not todas:
        return visao

    atual = competencia_de(hoje)
    por_mes: dict[str, list[int]] = {}
    for lanc, comp, _, _ in todas:
        por_mes.setdefault(comp, []).append(-lanc.valor)
    por_compra: dict[str, list[tuple[Lancamento, str, str]]] = {}
    for item in futuras:
        por_compra.setdefault(item[0].grupo_parcela_id, []).append(item)

    ref = mes_ref or atual
    mes = somar_meses(min(atual, ref), -2)
    fim = max([atual, ref] + [i[1] for i in futuras])
    while mes <= fim:
        valores = por_mes.get(mes, [])
        total = sum(valores)
        visao.meses.append(MesParcelas(mes, total, len(valores), teto > 0 and total > teto, mes < atual))
        mes = somar_meses(mes, 1)

    visao.total_a_pagar = sum(-i[0].valor for i in futuras)
    visao.qtd_compras = len(por_compra)
    daqui = [m for m in visao.meses if not m.passado]
    visao.meses_acima = sum(m.acima for m in daqui)
    visao.pico = max(daqui, key=lambda m: m.total)  # empate: o mês mais cedo
    for grupo, itens in por_compra.items():
        proxima, _, cartao = itens[0]
        visao.compras.append(CompraParcelada(
            grupo=grupo, descricao=proxima.descricao, cartao_id=proxima.conta_id, cartao=cartao,
            proxima_num=proxima.parcela_num, total_parcelas=proxima.parcela_total,
            valor_parcela=-proxima.valor, restantes=len(itens), falta=sum(-i[0].valor for i in itens),
            ultimo_mes=max(i[1] for i in itens)))
    visao.compras.sort(key=lambda c: (-c.falta, c.descricao))
    return visao


def parcelas_do_mes(s: Session, hoje: date, competencia: str) -> tuple[int, int]:
    """(total de parcelas da fatura da competência, pendentes e já lançadas; teto): o resumo do Planejamento."""
    total = sum(-lanc.valor for lanc, comp, _, _ in _lancadas(s, None) if comp == competencia)
    return total, teto_parcelas(s)


def parcelas_no_gasto_do_mes(s: Session, competencia: str) -> int:
    """Parte do gasto do mês que é parcela, pela mesma regra do orçamento e do teto (compra no cartão conta no
    mês em que a fatura vence; efetivada ou pendente): é o pedaço da barra do teto total que vem de parcelas."""
    soma = s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.grupo_parcela_id.is_not(None), Lancamento.tipo == "despesa", Lancamento.valor < 0,
        Lancamento.ignorar.is_(False), no_mes(competencia), com_status(("efetivado", "pendente"))))
    return -int(soma)
