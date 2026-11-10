from datetime import date

import pytest

from app.services.faturas import (
    competencia_da_compra,
    datas_fatura,
    fatura_da_compra,
    faturas_do_cartao,
    obter_ou_criar_fatura,
    pagar_fatura,
    status_exibicao,
    total_fatura,
    total_pago,
)
from app.services.saldos import saldo_atual
from tests.factories import nova_conta, novo_cartao, novo_lancamento


@pytest.mark.parametrize(
    "d, fechamento, esperado",
    [
        (date(2026, 10, 3), 3, "2026-10"),
        (date(2026, 10, 4), 3, "2026-11"),
        (date(2026, 12, 20), 10, "2027-01"),
        (date(2026, 2, 28), 31, "2026-02"),
    ],
)
def test_competencia_da_compra(d, fechamento, esperado):
    assert competencia_da_compra(d, fechamento) == esperado


def test_datas_fatura_vencimento_no_mesmo_mes(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=3, vencimento=10)
    assert datas_fatura(cartao, "2026-10") == (date(2026, 10, 3), date(2026, 10, 10))


def test_datas_fatura_vencimento_no_mes_seguinte(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=28, vencimento=5)
    assert datas_fatura(cartao, "2026-12") == (date(2026, 12, 28), date(2027, 1, 5))


def test_obter_ou_criar_fatura_e_idempotente(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    f1 = obter_ou_criar_fatura(session, cartao, "2026-10")
    f2 = obter_ou_criar_fatura(session, cartao, "2026-10")
    assert f1.id == f2.id
    assert f1.status == "aberta"


def test_obter_fatura_em_conta_corrente_falha(session):
    with pytest.raises(ValueError):
        obter_ou_criar_fatura(session, nova_conta(session), "2026-10")


def _fatura_com_compras(session):
    nubank = nova_conta(session, saldo=500000)
    cartao = novo_cartao(session, pagadora=nubank)
    fatura = fatura_da_compra(session, cartao, date(2026, 10, 2))
    novo_lancamento(session, cartao, date(2026, 10, 2), -30000, fatura_id=fatura.id)
    novo_lancamento(session, cartao, date(2026, 10, 1), -20000, fatura_id=fatura.id)
    novo_lancamento(session, cartao, date(2026, 10, 1), -999, fatura_id=fatura.id, status="cancelado")
    return nubank, cartao, fatura


def test_total_fatura(session):
    _, _, fatura = _fatura_com_compras(session)
    assert total_fatura(session, fatura) == 50000


def test_pagamento_total(session):
    nubank, cartao, fatura = _fatura_com_compras(session)

    saida, entrada = pagar_fatura(session, fatura, 50000, date(2026, 10, 10))

    assert fatura.status == "paga"
    assert (saida.conta_id, saida.valor, saida.tipo) == (nubank.id, -50000, "pagamento_fatura")
    assert (entrada.conta_id, entrada.valor) == (cartao.id, 50000)
    assert saida.transferencia_par_id == entrada.id and entrada.transferencia_par_id == saida.id
    assert total_pago(session, fatura) == 50000
    assert saldo_atual(session, cartao) == 0
    assert saldo_atual(session, nubank) == 450000


def test_pagamento_parcial(session):
    _, _, fatura = _fatura_com_compras(session)
    pagar_fatura(session, fatura, 10000, date(2026, 10, 10))
    assert fatura.status == "paga_parcial"


def test_pagamento_pendente_nao_muda_status(session):
    _, _, fatura = _fatura_com_compras(session)
    pagar_fatura(session, fatura, 50000, date(2026, 10, 10), efetivado=False)
    assert fatura.status == "aberta"


def test_status_exibicao(session):
    _, _, fatura = _fatura_com_compras(session)
    assert status_exibicao(fatura, date(2026, 10, 3)) == "aberta"
    assert status_exibicao(fatura, date(2026, 10, 4)) == "fechada"


def test_faturas_do_cartao_ordem_decrescente(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    obter_ou_criar_fatura(session, cartao, "2026-10")
    obter_ou_criar_fatura(session, cartao, "2026-12")
    obter_ou_criar_fatura(session, cartao, "2026-11")
    assert [f.competencia for f in faturas_do_cartao(session, cartao)] == ["2026-12", "2026-11", "2026-10"]


def test_compra_depois_do_fechamento_real_de_fatura_gravada_vai_para_a_seguinte(session):
    """O cadastro diz dia 25, mas a fatura de outubro veio do banco fechando em 24/10: a compra de 25/10 é de novembro."""
    from app.services.faturas import fatura_da_compra, obter_ou_criar_fatura
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=25, vencimento=1)
    outubro = obter_ou_criar_fatura(session, cartao, "2026-10")
    outubro.data_fechamento = date(2026, 10, 24)
    assert fatura_da_compra(session, cartao, date(2026, 10, 25)).competencia == "2026-11"
    assert fatura_da_compra(session, cartao, date(2026, 10, 24)).competencia == "2026-10"


def test_compra_antes_do_fechamento_real_atrasado_fica_na_fatura_gravada(session):
    """Fechamento real 27/10 (dia cadastrado 25): a compra de 26/10 ainda é de outubro."""
    from app.services.faturas import fatura_da_compra, obter_ou_criar_fatura
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=25, vencimento=1)
    outubro = obter_ou_criar_fatura(session, cartao, "2026-10")
    outubro.data_fechamento = date(2026, 10, 27)
    assert fatura_da_compra(session, cartao, date(2026, 10, 26)).competencia == "2026-10"
    assert fatura_da_compra(session, cartao, date(2026, 10, 28)).competencia == "2026-11"
