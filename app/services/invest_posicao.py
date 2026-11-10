from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

UM = Decimal(1)
OITO_CASAS = Decimal("1E-8")  # quantidade com rendimento embutido: precisão de cripto

# Retorna o valor atualizado (centavos BRL) da posição de renda fixa na data, dadas as operações anteriores.
ValorNaData = Callable[[date, Sequence], int]


@dataclass(frozen=True)
class Posicao:
    quantidade: Decimal
    preco_medio_brl: Decimal  # centavos de BRL por unidade (fracionário, sem arredondar)
    custo_brl: int  # centavos
    lucro_realizado_brl: int  # centavos


def _centavo(valor: Decimal) -> int:
    return int(valor.quantize(UM, rounding=ROUND_HALF_UP))


def _cambio(op) -> Decimal:
    return UM if op.cambio is None else Decimal(op.cambio)


def recebido(op, bruto: Decimal) -> Decimal:
    """Venda/resgate: o valor líquido informado (imposto já retido) ou, sem ele, bruto − taxas."""
    liquido = getattr(op, "valor_liquido", None)
    return Decimal(liquido) if liquido is not None else bruto - (op.taxas or 0)


def posicao(operacoes: Sequence, valor_na_data: ValorNaData | None = None, por_titulos: bool = False,
            rendimento_aa: Decimal | None = None, ate: date | None = None) -> Posicao:
    """Posição a partir das operações já ordenadas por (data, id).

    `valor_na_data` só é usado em resgates de renda fixa (valor atualizado do título na data do resgate).
    Sem ele, o valor atualizado é considerado igual ao custo.
    `por_titulos` (Tesouro Direto): aplicação e resgate contam títulos, como compra e venda.
    `rendimento_aa` (% a.a. embutido no ativo, ex.: USDC na corretora): a quantidade cresce todo dia a juros
    compostos, de operação em operação e, com `ate`, até essa data. O custo não muda: o rendimento é lucro.
    """
    fator_dia = (UM + Decimal(rendimento_aa) / 100) ** (UM / 365) if rendimento_aa else None
    qtd = Decimal(0)
    custo = 0
    lucro = 0
    anteriores: list = []
    desde: date | None = None

    def render(ate_dia: date) -> Decimal:
        dias = (ate_dia - desde).days if desde else 0
        return (qtd * fator_dia ** dias).quantize(OITO_CASAS) if fator_dia and qtd and dias > 0 else qtd

    for op in operacoes:
        qtd, desde = render(op.data), op.data
        tipo = {"aplicacao": "compra", "resgate": "venda"}.get(op.tipo, op.tipo) if por_titulos else op.tipo
        q_op = Decimal(op.quantidade)
        bruto = q_op * op.preco_unitario * _cambio(op)
        taxas = op.taxas or 0
        if tipo in ("compra", "aplicacao"):
            custo = _centavo(custo + bruto + taxas)
            qtd = qtd + q_op if tipo == "compra" else (UM if custo > 0 else Decimal(0))
        elif tipo == "venda":
            if q_op > qtd:
                raise ValueError(f"venda de {q_op} acima da posição de {qtd} em {op.data}")
            lucro += _centavo(recebido(op, bruto) - (Decimal(custo) * q_op / qtd))
            novo_qtd = qtd - q_op
            custo = _centavo(Decimal(custo) * novo_qtd / qtd) if novo_qtd else 0
            qtd = novo_qtd
        elif tipo == "resgate":
            valor = valor_na_data(op.data, anteriores) if valor_na_data else custo
            if bruto > valor and valor_na_data is not None:
                raise ValueError(f"resgate de {_centavo(bruto)} acima do valor atualizado de {valor} em {op.data}")
            fracao = min(UM, bruto / valor) if valor else UM
            baixa = _centavo(Decimal(custo) * fracao)
            lucro += _centavo(recebido(op, bruto)) - baixa
            custo -= baixa
            qtd = UM if custo > 0 else Decimal(0)
        elif tipo == "ajuste_posicao":
            qtd = q_op
            custo = _centavo(bruto)
        elif tipo == "rendimento":  # creditado pela corretora: só quantidade, o custo não muda (é lucro)
            qtd = qtd + q_op
        else:
            raise ValueError(f"tipo de operação desconhecido: {op.tipo}")
        anteriores.append(op)
    if ate is not None:
        qtd = render(ate)
    pm = Decimal(custo) / qtd if qtd else Decimal(0)
    return Posicao(quantidade=qtd, preco_medio_brl=pm, custo_brl=custo, lucro_realizado_brl=lucro)
