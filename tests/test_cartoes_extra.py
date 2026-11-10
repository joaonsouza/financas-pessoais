from datetime import date

from app.services.faturas import (
    compras_da_fatura,
    fatura_existente,
    limite_disponivel,
    pagamentos_da_fatura,
    pagar_fatura,
)
from app.services.lancamentos import criar_compra_parcelada, criar_lancamento
from tests.factories import nova_conta, novo_cartao


def test_limite_disponivel_considera_parcelas_futuras_e_pagamentos(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), limite=1_000_000)
    compra = criar_lancamento(session, conta=cartao, data=date(2026, 10, 2), descricao="Mercado",
                              valor_abs=30000, tipo="despesa", efetivado=True)
    criar_compra_parcelada(session, conta=cartao, data=date(2026, 10, 2), descricao="TV",
                           valor_total=30000, parcelas=3, efetivado_primeira=True)
    fatura = fatura_existente(session, cartao, "2026-10")
    pagar_fatura(session, fatura, 30000, date(2026, 10, 10))
    pagar_fatura(session, fatura, 5000, date(2026, 10, 10), efetivado=False)

    assert limite_disponivel(session, cartao) == 970000
    assert [l.descricao for l in compras_da_fatura(session, fatura)] == ["Mercado", "TV"]
    assert [l.valor for l in pagamentos_da_fatura(session, fatura)] == [30000, 5000]
    assert compra.fatura_id == fatura.id


def test_fatura_existente_retorna_none(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    assert fatura_existente(session, cartao, "2030-01") is None
