"""Conta em dólar: saldo na moeda da conta, somas em reais pela última cotação. Dados fictícios."""
from datetime import date, datetime
from decimal import Decimal as D

import pytest

from app.models import Conta, CotacaoCache
from app.services.lancamentos import criar_transferencia
from app.services.saldos import saldo_liquido, saldos_contas
from tests.factories import nova_conta

HOJE = date(2026, 10, 30)


def _cotacao(s, preco, dia=HOJE):
    s.add(CotacaoCache(simbolo="USDBRL=X", data=dia, fechamento=D(preco), moeda="BRL", fonte="yahoo",
                       atualizado_em=datetime(2026, 10, 30, 10)))
    s.flush()


def _nomad(s, saldo=861):
    conta = nova_conta(s, "Nomad", saldo=saldo, sl=False)
    conta.moeda = "USD"
    s.flush()
    return conta


def test_conta_nova_e_em_reais(session):
    assert nova_conta(session).moeda == "BRL"


def test_conta_em_dolar_soma_em_reais_e_mostra_em_dolar(session):
    nova_conta(session, "Nubank", saldo=100000)
    _nomad(session)
    _cotacao(session, "5.30", dia=date(2026, 10, 29))
    _cotacao(session, "5.3793")  # a mais recente vale
    session.commit()
    saldos = {x.nome: x for x in saldos_contas(session, HOJE, "2026-10")}
    nomad = saldos["Nomad"]
    assert (nomad.moeda, nomad.atual_moeda, nomad.atual, nomad.previsto_mes) == ("USD", 861, 4632, 4632)
    assert nomad.sem_cotacao is False
    assert (saldos["Nubank"].moeda, saldos["Nubank"].atual_moeda) == ("BRL", 100000)
    assert saldo_liquido(list(saldos.values())) == 100000  # Nomad fica fora do saldo líquido


def test_sem_cotacao_soma_zero_e_avisa(session):
    _nomad(session)
    session.commit()
    nomad = saldos_contas(session, HOJE, "2026-10")[0]
    assert (nomad.atual_moeda, nomad.atual, nomad.sem_cotacao) == (861, 0, True)


def test_transferencia_entre_moedas_e_recusada(session):
    nubank, nomad = nova_conta(session, "Nubank"), _nomad(session)
    with pytest.raises(ValueError, match="moedas diferentes"):
        criar_transferencia(session, origem=nubank, destino=nomad, data=HOJE, valor_abs=1000)


def test_formatar_dolares():
    from app.core.dinheiro import formatar_dolares
    assert formatar_dolares(861) == "US$ 8,61" and formatar_dolares(-123456) == "-US$ 1.234,56"


def test_tela_inicial_e_contas_mostram_dolar_e_reais(client, db):
    nova_conta(db, "Nubank", saldo=100000)
    _nomad(db)
    _cotacao(db, "5.3793")
    db.commit()
    for url in ("/?mes=2026-10", "/contas?mes=2026-10"):
        texto = client.get(url).text
        assert "US$ 8,61" in texto and "≈ R$ 46,32" in texto


def test_criar_conta_em_dolar_fica_fora_do_saldo_liquido(client, db):
    nova_conta(db, "Nubank")  # sem conta nenhuma o app manda para o setup
    db.commit()
    client.post("/contas?mes=2026-10", data={"nome": "Nomad", "tipo": "corrente", "moeda": "USD", "saldo": "8,61",
                                             "sl": "on"})
    conta = db.query(Conta).filter_by(nome="Nomad").one()
    assert (conta.moeda, conta.saldo_inicial, conta.entra_saldo_liquido) == ("USD", 861, False)


def test_conta_de_investimento_nao_pode_ser_em_dolar(client, db):
    nova_conta(db, "Nubank")
    db.commit()
    r = client.post("/contas?mes=2026-10", data={"nome": "X", "tipo": "investimento", "moeda": "USD"})
    assert r.status_code == 400 and "só conta corrente" in r.text


def test_editar_nao_liga_saldo_liquido_nem_troca_moeda(client, db):
    conta = _nomad(db)
    db.commit()
    client.post(f"/contas/{conta.id}/editar?mes=2026-10", data={"nome": "Nomad", "cor": "#6366f1", "sl": "on",
                                                                 "moeda": "BRL"})
    db.refresh(conta)
    assert (conta.moeda, conta.entra_saldo_liquido) == ("USD", False)


def test_conta_em_dolar_nao_paga_fatura(client, db):
    from tests.factories import novo_cartao
    nubank = nova_conta(db, "Nubank")
    _nomad(db)
    cartao = novo_cartao(db, pagadora=nubank)
    db.commit()
    assert ">Nomad<" not in client.get(f"/cartoes/{cartao.id}").text
