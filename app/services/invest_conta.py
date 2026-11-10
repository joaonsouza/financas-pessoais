"""Onde fica cada lote (custódia) e o dinheiro de uma operação de investimento nas contas do app.

Custódia: a conta de investimento (e, se quiser, a caixinha dela, como "Bolsa de Valores") onde o lote fica.
Pagamento (opcional): a conta (e caixinha) que paga a compra ou recebe a venda. Com pagamento, a operação vira uma
transferência entre a conta e a custódia; não é despesa: fica fora de gastos e do teto, e o saldo líquido cai porque
as contas de investimento não entram nele.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ativo, Conta, Lancamento, OperacaoInvest
from app.models.caixinha import Caixinha
from app.services.invest_posicao import recebido
from app.services.lancamentos import criar_transferencia, excluir_lancamento

ENTRADAS = ("compra", "aplicacao")
SAIDAS = ("venda", "resgate")


@dataclass(frozen=True)
class Local:
    """Conta + caixinha opcional (custódia ou pagamento)."""
    conta: Conta
    caixinha: Caixinha | None = None


@dataclass(frozen=True)
class Vinculo:
    """O que está ligado a uma operação (para preencher o formulário)."""
    custodia_conta_id: int | None = None
    custodia_caixinha_id: int | None = None
    conta_id: int | None = None
    caixinha_conta_id: int | None = None
    carteira_id: int | None = None


def validar_local(local: Local, *, custodia: bool) -> None:
    if local.conta.tipo == "cartao":
        raise ValueError("cartão de crédito não pode pagar nem guardar investimento; escolha uma conta")
    if custodia and local.conta.tipo != "investimento":
        raise ValueError("o investimento fica numa conta de investimento")
    if local.caixinha is not None and local.caixinha.conta_id != local.conta.id:
        raise ValueError(f"a caixinha {local.caixinha.nome} é de outra conta")


def definir_custodia(op: OperacaoInvest, local: Local | None) -> None:
    if local is not None:
        validar_local(local, custodia=True)
    op.custodia_conta_id = local.conta.id if local else None
    op.custodia_caixinha_id = local.caixinha.id if local and local.caixinha else None


def valor_da_operacao(op: OperacaoInvest) -> int:
    """Centavos BRL que saem da conta (compra: bruto + taxas) ou entram nela (venda: bruto − taxas, ou o líquido)."""
    bruto = Decimal(op.quantidade) * op.preco_unitario * (Decimal(op.cambio) if op.cambio else Decimal(1))
    total = bruto + (op.taxas or 0) if op.tipo in ENTRADAS else recebido(op, bruto)
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def vinculo(s: Session, op: OperacaoInvest) -> Vinculo:
    da_conta = s.get(Lancamento, op.lancamento_id) if op.lancamento_id else None
    return Vinculo(op.custodia_conta_id, op.custodia_caixinha_id,
                   da_conta.conta_id if da_conta else None, da_conta.caixinha_id if da_conta else None, op.carteira_id)


def sugestao(s: Session, ativo: Ativo) -> Vinculo:
    """Custódia e pagamento da operação mais recente do ativo que tenha algum; sem nenhuma, a corretora dele."""
    ops = s.scalars(select(OperacaoInvest).where(OperacaoInvest.ativo_id == ativo.id)
                    .order_by(OperacaoInvest.data.desc(), OperacaoInvest.id.desc()))
    custodia = pagamento = None
    for op in ops:
        v = vinculo(s, op)
        if custodia is None and v.custodia_conta_id:
            custodia = (v.custodia_conta_id, v.custodia_caixinha_id)
        if pagamento is None and v.conta_id:
            pagamento = (v.conta_id, v.caixinha_conta_id)
        if custodia and pagamento:
            break
    custodia = custodia or (ativo.corretora_conta_id, None)
    pagamento = pagamento or (None, None)
    ultima = s.scalar(select(OperacaoInvest.carteira_id).where(OperacaoInvest.ativo_id == ativo.id,
                                                              OperacaoInvest.carteira_id.is_not(None))
                      .order_by(OperacaoInvest.data.desc(), OperacaoInvest.id.desc()).limit(1))
    return Vinculo(*custodia, *pagamento, ultima)


def desvincular(s: Session, op: OperacaoInvest) -> None:
    da_conta = s.get(Lancamento, op.lancamento_id) if op.lancamento_id else None
    op.lancamento_id = None
    if da_conta is not None:
        excluir_lancamento(s, da_conta)
    s.flush()


LIQUIDA_EM_D2 = ("acao", "fii", "etf")  # negociados na B3: a venda liquida em D+2 úteis


def somar_dias_uteis(dia: date, n: int) -> date:
    """`n` dias úteis depois de `dia`, pulando só sábado e domingo (feriado não entra: ajuste a data se cair num)."""
    while n > 0:
        dia += timedelta(days=1)
        if dia.weekday() < 5:
            n -= 1
    return dia


def data_do_dinheiro(ativo: Ativo, op: OperacaoInvest) -> date:
    """Quando o dinheiro mexe na conta: na venda ou resgate, a data escolhida no formulário; sem ela, na venda de
    ativo da B3, D+2 úteis (fica pendente até lá); senão, no dia."""
    if op.tipo in SAIDAS and op.data_liquidacao is not None:
        if op.data_liquidacao < op.data:
            raise ValueError("a data em que o dinheiro cai na conta não pode ser antes da venda")
        return op.data_liquidacao
    if op.tipo == "venda" and ativo.classe in LIQUIDA_EM_D2:
        return somar_dias_uteis(op.data, 2)
    return op.data


def vincular(s: Session, ativo: Ativo, op: OperacaoInvest, pagamento: Local | None, hoje: date) -> None:
    """Cria (ou refaz) a transferência entre o pagamento e a custódia; sem pagamento (ou num ajuste), remove."""
    desvincular(s, op)
    if pagamento is None or op.tipo not in (*ENTRADAS, *SAIDAS):
        return
    validar_local(pagamento, custodia=False)
    if op.custodia_conta_id is None:
        raise ValueError("escolha onde o investimento fica (conta de investimento) para movimentar a conta")
    custodia = s.get(Conta, op.custodia_conta_id)
    if pagamento.conta.id == custodia.id:
        raise ValueError("a conta que paga e a conta onde o investimento fica devem ser diferentes")
    valor = valor_da_operacao(op)
    if valor <= 0:
        raise ValueError("o valor da operação deve ser maior que zero para movimentar a conta")
    entrada = op.tipo in ENTRADAS
    origem, destino = (pagamento.conta, custodia) if entrada else (custodia, pagamento.conta)
    rotulo = {"compra": "Compra", "aplicacao": "Aplicação", "venda": "Venda", "resgate": "Resgate"}[op.tipo]
    data = data_do_dinheiro(ativo, op)
    saida, chegada = criar_transferencia(s, origem=origem, destino=destino, data=data, valor_abs=valor,
                                         descricao=f"{rotulo} {ativo.codigo}"[:200], efetivado=data <= hoje)
    na_conta, na_custodia = (saida, chegada) if entrada else (chegada, saida)
    na_conta.caixinha_id = pagamento.caixinha.id if pagamento.caixinha else None
    na_custodia.caixinha_id = op.custodia_caixinha_id
    if data != op.data:  # liquidação depois: na corretora o lote sai no dia da operação (a caixinha bate com as
        na_custodia.data = op.data  # posições); só a chegada na conta espera a data em que cai, pendente
        na_custodia.status = "efetivado" if op.data <= hoje else "pendente"
    op.lancamento_id = na_conta.id
    s.flush()


def quantidade_por_local(ops: list[OperacaoInvest]) -> dict[tuple[int, int | None] | None, Decimal]:
    """Quantidade de cada custódia (None = sem local), pela soma das compras menos as vendas de cada uma.

    Na renda fixa (quantidade sempre 1) vale o valor aplicado menos o resgatado, para repartir o saldo.
    Ajuste de posição zera as anteriores e põe a quantidade dele na custódia dele.
    """
    por: dict = defaultdict(Decimal)
    for op in ops:
        chave = (op.custodia_conta_id, op.custodia_caixinha_id) if op.custodia_conta_id else None
        q = Decimal(op.quantidade) * (op.preco_unitario if op.tipo in ("aplicacao", "resgate") else 1)
        if op.tipo == "ajuste_posicao":
            por = defaultdict(Decimal)
            por[chave] = q
        else:
            por[chave] += q if op.tipo in (*ENTRADAS, "rendimento") else -q
    return {k: v for k, v in por.items() if v > 0}
