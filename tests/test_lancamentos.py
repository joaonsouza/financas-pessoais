from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Fatura, Lancamento, Recorrencia, Tag
from app.services.faturas import pagar_fatura
from app.services.lancamentos import (
    ajustar_saldo,
    alternar_efetivado,
    atualizar_lancamento,
    criar_compra_parcelada,
    criar_lancamento,
    criar_transferencia,
    excluir_lancamento,
)
from app.services.saldos import saldo_atual
from tests.factories import nova_conta, novo_cartao


def test_despesa_em_conta_corrente(session):
    conta = nova_conta(session)
    lanc = criar_lancamento(session, conta=conta, data=date(2026, 10, 2), descricao=" Padaria ",
                            valor_abs=1250, tipo="despesa", tags=["Viagem", "viagem", "SP"])
    assert (lanc.valor, lanc.status, lanc.descricao, lanc.fatura_id) == (-1250, "pendente", "Padaria", None)
    assert [t.nome for t in lanc.tags] == ["viagem", "sp"]


def test_tags_sao_reaproveitadas(session):
    conta = nova_conta(session)
    for _ in range(2):
        criar_lancamento(session, conta=conta, data=date(2026, 10, 2), descricao="X",
                         valor_abs=1, tipo="despesa", tags=["viagem"])
    assert session.scalar(select(func.count(Tag.id))) == 1


def test_receita_positiva_e_efetivada(session):
    conta = nova_conta(session)
    lanc = criar_lancamento(session, conta=conta, data=date(2026, 10, 30), descricao="Salário",
                            valor_abs=842549, tipo="receita", efetivado=True)
    assert (lanc.valor, lanc.status) == (842549, "efetivado")


def test_despesa_no_cartao_recebe_fatura(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=3)
    lanc = criar_lancamento(session, conta=cartao, data=date(2026, 10, 4), descricao="Mercado",
                            valor_abs=5000, tipo="despesa")
    assert session.get(Fatura, lanc.fatura_id).competencia == "2026-11"


@pytest.mark.parametrize("tipo, valor", [("receita", 100), ("despesa", 0), ("transferencia", 100)])
def test_criar_lancamento_invalido(session, tipo, valor):
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    with pytest.raises(ValueError):
        criar_lancamento(session, conta=cartao, data=date(2026, 10, 4), descricao="X", valor_abs=valor, tipo=tipo)


def test_transferencia_move_saldo(session):
    nubank = nova_conta(session, "Nubank", saldo=100000)
    nuinvest = nova_conta(session, "NuInvest", tipo="investimento", sl=False)
    saida, entrada = criar_transferencia(session, origem=nubank, destino=nuinvest, data=date(2026, 10, 2),
                                         valor_abs=30000, efetivado=True)
    assert (saida.valor, entrada.valor, saida.tipo) == (-30000, 30000, "transferencia")
    assert saida.transferencia_par_id == entrada.id
    assert saldo_atual(session, nubank) == 70000
    assert saldo_atual(session, nuinvest) == 30000


def test_transferencia_para_cartao_falha(session):
    nubank = nova_conta(session)
    cartao = novo_cartao(session, pagadora=nubank)
    with pytest.raises(ValueError):
        criar_transferencia(session, origem=nubank, destino=cartao, data=date(2026, 10, 2), valor_abs=1)


def test_compra_parcelada_no_cartao(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=3)
    parcelas = criar_compra_parcelada(session, conta=cartao, data=date(2026, 10, 2), descricao="Geladeira",
                                      valor_total=10000, parcelas=3, efetivado_primeira=True)
    assert [p.valor for p in parcelas] == [-3334, -3333, -3333]
    assert [p.parcela_num for p in parcelas] == [1, 2, 3]
    assert {p.parcela_total for p in parcelas} == {3}
    assert len({p.grupo_parcela_id for p in parcelas}) == 1
    assert [p.status for p in parcelas] == ["efetivado", "pendente", "pendente"]
    assert [p.data for p in parcelas] == [date(2026, 10, 2), date(2026, 11, 2), date(2026, 12, 2)]
    assert [session.get(Fatura, p.fatura_id).competencia for p in parcelas] == ["2026-10", "2026-11", "2026-12"]


def test_compra_parcelada_exige_duas_parcelas(session):
    with pytest.raises(ValueError):
        criar_compra_parcelada(session, conta=nova_conta(session), data=date(2026, 10, 2),
                               descricao="X", valor_total=100, parcelas=1)


def test_ajustar_saldo(session):
    conta = nova_conta(session, "NuInvest", tipo="investimento", saldo=100000)
    ajuste = ajustar_saldo(session, conta, 100750, date(2026, 10, 5))
    assert (ajuste.tipo, ajuste.valor, ajuste.status) == ("ajuste", 750, "efetivado")
    assert saldo_atual(session, conta) == 100750
    assert ajustar_saldo(session, conta, 100750, date(2026, 10, 5)) is None


def test_alternar_efetivado_em_pagamento_atualiza_fatura(session):
    nubank = nova_conta(session, saldo=100000)
    cartao = novo_cartao(session, pagadora=nubank)
    compra = criar_lancamento(session, conta=cartao, data=date(2026, 10, 2), descricao="X",
                              valor_abs=5000, tipo="despesa", efetivado=True)
    fatura = session.get(Fatura, compra.fatura_id)
    saida, entrada = pagar_fatura(session, fatura, 5000, date(2026, 10, 10), efetivado=False)

    alternar_efetivado(session, saida)

    assert (saida.status, entrada.status, fatura.status) == ("efetivado", "efetivado", "paga")


def test_atualizar_compra_no_cartao_muda_fatura(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=3)
    lanc = criar_lancamento(session, conta=cartao, data=date(2026, 10, 2), descricao="X",
                            valor_abs=5000, tipo="despesa")
    atualizar_lancamento(session, lanc, data=date(2026, 10, 10), descricao="Mercado", valor_abs=6000,
                         categoria_id=None, tags=["casa"])
    assert (lanc.valor, lanc.descricao) == (-6000, "Mercado")
    assert session.get(Fatura, lanc.fatura_id).competencia == "2026-11"
    assert [t.nome for t in lanc.tags] == ["casa"]


def test_atualizar_transferencia_atualiza_par(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    saida, entrada = criar_transferencia(session, origem=a, destino=b, data=date(2026, 10, 2), valor_abs=100)
    atualizar_lancamento(session, saida, data=date(2026, 10, 3), descricao="Reserva", valor_abs=200,
                         categoria_id=None, tags=[])
    assert (entrada.valor, entrada.data) == (200, date(2026, 10, 3))


def test_excluir_transferencia_apaga_par(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    saida, _ = criar_transferencia(session, origem=a, destino=b, data=date(2026, 10, 2), valor_abs=100)
    excluir_lancamento(session, saida)
    assert session.scalar(select(func.count(Lancamento.id))) == 0


def test_excluir_pendente_de_recorrencia_vira_cancelado(session):
    conta = nova_conta(session)
    rec = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=120000, dia=10, inicio="2026-10")
    session.add(rec)
    session.flush()
    lanc = criar_lancamento(session, conta=conta, data=date(2026, 10, 10), descricao="Aluguel",
                            valor_abs=120000, tipo="despesa")
    lanc.recorrencia_id, lanc.competencia_recorrencia = rec.id, "2026-10"

    excluir_lancamento(session, lanc)

    assert lanc.status == "cancelado"
    assert session.scalar(select(func.count(Lancamento.id))) == 1
