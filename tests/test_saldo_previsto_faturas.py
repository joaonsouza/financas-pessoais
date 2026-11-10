"""Saldo previsto desconta as faturas a pagar até a data. Dados fictícios."""
from datetime import date

from app.services.faturas import obter_ou_criar_fatura, pagar_fatura
from app.services.saldos import saldo_liquido, saldos_contas
from tests.factories import nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 10, 7)


def _base(s):
    conta = nova_conta(s, "Itaú", saldo=100000)
    fora = nova_conta(s, "Reserva", tipo="investimento", saldo=50000, sl=False)
    cartao = novo_cartao(s, "Cartão Itaú", pagadora=conta, fechamento=3, vencimento=10)
    outubro = obter_ou_criar_fatura(s, cartao, "2026-10")   # vence 10/10
    novembro = obter_ou_criar_fatura(s, cartao, "2026-11")  # vence 10/11
    novo_lancamento(s, cartao, date(2026, 9, 20), -30000, fatura_id=outubro.id)
    novo_lancamento(s, cartao, date(2026, 10, 20), -20000, fatura_id=novembro.id)
    novo_lancamento(s, conta, date(2026, 10, 25), -5000, status="pendente", descricao="Conta de luz")
    s.commit()
    return conta, fora, cartao, outubro


def _por_nome(s):
    return {x.nome: x for x in saldos_contas(s, HOJE, "2026-10")}


def test_previsto_do_mes_desconta_fatura_que_vence_no_mes(session):
    _base(session)
    itau = _por_nome(session)["Itaú"]
    # 1.000 - 50 (luz) - 300 (fatura de 10/10); a fatura de 10/11 fica para o mês seguinte
    assert (itau.atual, itau.previsto_dia, itau.previsto_mes) == (100000, 100000, 65000)


def test_fatura_vencida_entra_no_previsto_de_hoje(session):
    _base(session)
    itau = {x.nome: x for x in saldos_contas(session, date(2026, 10, 12), "2026-10")}["Itaú"]
    assert itau.previsto_dia == 70000


def test_pagamento_parcial_desconta_so_o_que_falta(session):
    conta, _, _, outubro = _base(session)
    pagar_fatura(session, outubro, 10000, date(2026, 10, 5), conta_origem=conta, efetivado=True)
    session.commit()
    itau = _por_nome(session)["Itaú"]
    # pago 100 sai do atual; faltam 200 da fatura
    assert (itau.atual, itau.previsto_mes) == (90000, 65000)


def test_pagamento_agendado_nao_conta_duas_vezes(session):
    conta, _, _, outubro = _base(session)
    pagar_fatura(session, outubro, 30000, date(2026, 10, 10), conta_origem=conta, efetivado=False)
    session.commit()
    itau = _por_nome(session)["Itaú"]
    assert (itau.atual, itau.previsto_mes) == (100000, 65000)


def test_fatura_paga_nao_desconta_de_novo(session):
    conta, _, _, outubro = _base(session)
    pagar_fatura(session, outubro, 30000, date(2026, 10, 5), conta_origem=conta, efetivado=True)
    session.commit()
    itau = _por_nome(session)["Itaú"]
    assert (itau.atual, itau.previsto_mes) == (70000, 65000)


def test_saldo_liquido_previsto(session):
    _base(session)
    saldos = saldos_contas(session, HOJE, "2026-10")
    assert saldo_liquido(saldos, "previsto_mes") == 65000  # a reserva fica fora do saldo líquido
