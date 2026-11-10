from datetime import date

from app.services.saldos import saldo_atual, saldo_liquido, saldo_previsto, saldos_contas
from tests.factories import nova_conta, novo_lancamento

HOJE = date(2026, 10, 5)


def _cenario(s):
    conta = nova_conta(s, "Nubank", saldo=100000)
    novo_lancamento(s, conta, date(2026, 9, 30), -99999)  # antes do saldo inicial: ignorado
    novo_lancamento(s, conta, date(2026, 10, 2), -20000)
    novo_lancamento(s, conta, date(2026, 10, 3), -10000, status="pendente")
    novo_lancamento(s, conta, date(2026, 10, 20), -5000, status="pendente")
    novo_lancamento(s, conta, date(2026, 11, 5), -3000, status="pendente")
    novo_lancamento(s, conta, date(2026, 10, 4), -7777, status="cancelado")
    return conta


def test_saldo_atual_ignora_pendentes_cancelados_e_anteriores(session):
    conta = _cenario(session)
    assert saldo_atual(session, conta) == 80000


def test_saldo_previsto_dia_e_mes(session):
    conta = _cenario(session)
    assert saldo_previsto(session, conta, HOJE) == 70000
    assert saldo_previsto(session, conta, date(2026, 10, 31)) == 65000


def test_saldos_contas_e_saldo_liquido(session):
    _cenario(session)
    nova_conta(session, "NuInvest", tipo="investimento", saldo=500000, sl=False)

    saldos = saldos_contas(session, HOJE, "2026-10")
    por_nome = {x.nome: x for x in saldos}

    assert (por_nome["Nubank"].atual, por_nome["Nubank"].previsto_dia, por_nome["Nubank"].previsto_mes) == (
        80000, 70000, 65000,
    )
    assert por_nome["NuInvest"].atual == 500000
    assert saldo_liquido(saldos) == 80000
    assert saldo_liquido(saldos, "previsto_mes") == 65000


def test_saldos_contas_ignora_inativas_e_filtra_tipo(session):
    _cenario(session)
    inativa = nova_conta(session, "Antiga", saldo=1)
    inativa.ativa = False
    nova_conta(session, "XP", tipo="investimento", saldo=10)

    nomes = [x.nome for x in saldos_contas(session, HOJE, "2026-10")]
    assert nomes == ["Nubank", "XP"]
    assert [x.nome for x in saldos_contas(session, HOJE, "2026-10", tipos=("investimento",))] == ["XP"]
