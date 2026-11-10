from datetime import date
from decimal import Decimal as D

import pytest

from app.models import OperacaoInvest
from app.services.invest_posicao import posicao


def op(tipo, qtd, preco, dia=1, cambio=None, taxas=0):
    return OperacaoInvest(tipo=tipo, data=date(2026, 1, dia), quantidade=D(qtd), preco_unitario=preco,
                          cambio=None if cambio is None else D(cambio), taxas=taxas)


def test_sem_operacoes():
    p = posicao([])
    assert (p.quantidade, p.preco_medio_brl, p.custo_brl, p.lucro_realizado_brl) == (0, 0, 0, 0)


def test_compras_acumulam_custo_com_taxas_e_preco_medio():
    p = posicao([op("compra", "10", 3000, taxas=100), op("compra", "10", 4000, 2)])
    assert p.quantidade == 20
    assert p.custo_brl == 30000 + 100 + 40000
    assert p.preco_medio_brl == D(70100) / 20


def test_venda_parcial_mantem_pm_e_realiza_lucro():
    p = posicao([op("compra", "10", 3000), op("compra", "10", 5000, 2), op("venda", "5", 6000, 3, taxas=50)])
    # PM = 4000; lucro = (5*6000 - 50) - 5*4000 = 9950
    assert p.quantidade == 15
    assert p.preco_medio_brl == 4000
    assert p.custo_brl == 60000
    assert p.lucro_realizado_brl == 9950


def test_venda_total_zera_custo():
    p = posicao([op("compra", "3", 3333), op("venda", "3", 4000, 2)])
    assert p.quantidade == 0 and p.custo_brl == 0 and p.preco_medio_brl == 0
    assert p.lucro_realizado_brl == 12000 - 9999


def test_venda_acima_da_posicao():
    with pytest.raises(ValueError):
        posicao([op("compra", "5", 1000), op("venda", "5.00000001", 1000, 2)])
    with pytest.raises(ValueError):
        posicao([op("venda", "1", 1000)])


def test_exterior_com_cambio():
    # 2 ações a US$ 150,00 (15000 centavos) com câmbio 5,50 -> 2*15000*5.5 = 165000 centavos BRL
    p = posicao([op("compra", "2", 15000, cambio="5.50", taxas=500)])
    assert p.custo_brl == 165500
    assert p.preco_medio_brl == D(165500) / 2
    # vende 1 a US$ 200 com câmbio 6,00 -> 120000; custo baixado 82750
    p = posicao([op("compra", "2", 15000, cambio="5.50", taxas=500), op("venda", "1", 20000, 2, cambio="6.00")])
    assert p.lucro_realizado_brl == 120000 - 82750
    assert p.custo_brl == 82750


def test_cripto_fracionada():
    p = posicao([op("compra", "0.00123456", 30000000), op("venda", "0.0006", 40000000, 2)])
    assert p.quantidade == D("0.00063456")
    assert p.custo_brl == int((D(37037) * D("0.00063456") / D("0.00123456")).quantize(D(1)))
    assert p.lucro_realizado_brl == 24000 - int((D(37037) * D("0.0006") / D("0.00123456")).quantize(D(1)))


def test_arredondamento_half_up_no_centavo():
    p = posicao([op("compra", "1", 1005), op("compra", "0.5", 1)])  # 1005 + 0.5 -> 1006 (half up)
    assert p.custo_brl == 1006


def test_ajuste_reinicia_posicao_e_mantem_lucro():
    p = posicao([op("compra", "10", 1000), op("venda", "10", 1500, 2), op("compra", "5", 900, 3),
                 op("ajuste_posicao", "100", 2000, 4)])
    assert p.quantidade == 100 and p.preco_medio_brl == 2000 and p.custo_brl == 200000
    assert p.lucro_realizado_brl == 5000
    p = posicao([op("ajuste_posicao", "100", 2000, 4), op("compra", "100", 3000, 5)])
    assert p.preco_medio_brl == 2500


def test_ajuste_exterior_usa_cambio():
    p = posicao([op("ajuste_posicao", "10", 10000, cambio="5")])
    assert p.custo_brl == 500000 and p.preco_medio_brl == 50000


def test_renda_fixa_aplicacao_e_resgate_proporcional_ao_valor_atualizado():
    ops = [op("aplicacao", "1", 100000), op("aplicacao", "1", 50000, 2)]
    assert posicao(ops).custo_brl == 150000
    # título vale 180000; resgata 90000 (metade) -> baixa metade do custo (75000); lucro 15000
    p = posicao(ops + [op("resgate", "1", 90000, 10)], valor_na_data=lambda data, anteriores: 180000)
    assert p.custo_brl == 75000
    assert p.lucro_realizado_brl == 15000
    assert p.quantidade == 1


def test_renda_fixa_resgate_total_zera():
    ops = [op("aplicacao", "1", 100000), op("resgate", "1", 110000, 10, taxas=1000)]
    p = posicao(ops, valor_na_data=lambda d, a: 110000)
    assert p.custo_brl == 0 and p.quantidade == 0 and p.lucro_realizado_brl == 9000


def test_renda_fixa_resgate_acima_do_valor_atualizado():
    with pytest.raises(ValueError):
        posicao([op("aplicacao", "1", 100000), op("resgate", "1", 120000, 10)], valor_na_data=lambda d, a: 110000)


def test_valor_na_data_recebe_operacoes_anteriores():
    vistos = []
    posicao([op("aplicacao", "1", 100), op("resgate", "1", 50, 5)],
            valor_na_data=lambda d, a: (vistos.append((d, len(a))), 100)[1])
    assert vistos == [(date(2026, 1, 5), 1)]


# --- rendimento embutido (ex.: USDC rende 4% a.a. na corretora): a quantidade cresce todo dia ---

def op_em(tipo, qtd, preco, data):
    return OperacaoInvest(tipo=tipo, data=data, quantidade=D(qtd), preco_unitario=preco, cambio=None, taxas=0)


def test_rendimento_cresce_a_quantidade_ate_a_data_e_nao_mexe_no_custo():
    ops = [op_em("compra", "100", 500, date(2026, 1, 1))]
    um_ano = posicao(ops, rendimento_aa=D("4"), ate=date(2027, 1, 1))
    assert um_ano.quantidade == D("104.00000000") and um_ano.custo_brl == 50000
    dez_dias = posicao(ops, rendimento_aa=D("4"), ate=date(2026, 1, 11))
    assert dez_dias.quantidade == (D(100) * D("1.04") ** (D(10) / 365)).quantize(D("1E-8"))
    assert posicao(ops, rendimento_aa=D("4")).quantidade == D(100)  # sem `ate`: até a última operação


def test_rendimento_conta_na_venda_e_recomeca_do_ajuste():
    compra = op_em("compra", "100", 500, date(2026, 1, 1))
    venda = op_em("venda", "104", 500, date(2027, 1, 1))  # vende o rendimento junto: não passa da posição
    assert posicao([compra, venda], rendimento_aa=D("4")).quantidade == 0
    ajuste = op_em("ajuste_posicao", "50", 500, date(2027, 1, 1))
    pos = posicao([compra, ajuste], rendimento_aa=D("4"), ate=date(2028, 1, 1))
    assert pos.quantidade == D("52.00000000")


def test_sem_rendimento_a_data_nao_muda_nada():
    ops = [op_em("compra", "100", 500, date(2026, 1, 1))]
    assert posicao(ops, ate=date(2027, 1, 1)).quantidade == D(100)


def test_operacao_rendimento_soma_quantidade_sem_custo():
    """Rendimento creditado pela corretora (ex.: USDC): entra quantidade, o custo não muda, vira lucro."""
    p = posicao([op("compra", "100", 500), op("rendimento", "0.5", 0, 2), op("venda", "50.25", 600, 3)])
    # custo 50.000 sobre 100,5 unidades: PM 497,51...; a venda leva metade
    assert p.quantidade == D("50.25") and p.custo_brl == 25000
    assert p.lucro_realizado_brl == 50.25 * 600 - 25000
