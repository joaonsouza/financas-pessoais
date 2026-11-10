"""Ajustar saldo pode criar um lançamento de ajuste ou corrigir o saldo inicial da conta."""
from datetime import date

from sqlalchemy import func, select

from app.models import Conta, Lancamento
from app.services.lancamentos import ajustar_saldo_inicial
from app.services.saldos import saldo_atual
from tests.factories import nova_conta, novo_lancamento


def test_corrige_o_saldo_inicial_sem_lancamento(session):
    conta = nova_conta(session, saldo=10000)
    novo_lancamento(session, conta, date(2026, 10, 2), -3000)

    diferenca = ajustar_saldo_inicial(session, conta, 9000)

    assert diferenca == 2000
    assert conta.saldo_inicial == 12000
    assert saldo_atual(session, conta) == 9000
    assert session.scalar(select(func.count(Lancamento.id)).where(Lancamento.tipo == "ajuste")) == 0


def test_saldo_igual_nao_muda_nada(session):
    conta = nova_conta(session, saldo=10000)

    assert ajustar_saldo_inicial(session, conta, 10000) == 0
    assert conta.saldo_inicial == 10000


def test_rota_ajustar_com_saldo_inicial(client, db):
    conta = nova_conta(db, saldo=10000)
    novo_lancamento(db, conta, date(2026, 10, 2), -3000)
    db.commit()

    resposta = client.post(f"/contas/{conta.id}/ajustar?mes=2026-10",
                           data={"novo_saldo": "50,00", "data": "2026-10-05", "modo": "saldo_inicial"},
                           follow_redirects=False)

    assert resposta.status_code == 303
    db.expire_all()
    assert db.get(Conta, conta.id).saldo_inicial == 8000
    assert db.scalar(select(func.count(Lancamento.id)).where(Lancamento.tipo == "ajuste")) == 0


def test_rota_ajustar_padrao_continua_criando_lancamento(client, db):
    conta = nova_conta(db, saldo=10000)
    db.commit()

    client.post(f"/contas/{conta.id}/ajustar?mes=2026-10", data={"novo_saldo": "50,00", "data": "2026-10-05"})

    db.expire_all()
    assert db.get(Conta, conta.id).saldo_inicial == 10000
    assert db.scalar(select(func.count(Lancamento.id)).where(Lancamento.tipo == "ajuste")) == 1


def test_formularios_oferecem_as_duas_formas(client, db):
    conta = nova_conta(db)
    db.commit()

    assert 'value="saldo_inicial"' in client.get("/contas?mes=2026-10").text
    assert 'value="saldo_inicial"' in client.get(f"/contas/{conta.id}").text
