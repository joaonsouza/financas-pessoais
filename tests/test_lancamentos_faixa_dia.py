"""Lista de lançamentos limpa (sem faixa de fim de dia), saldo líquido em uma data e estado vazio. Dados fictícios."""
from datetime import date

from app.services.saldos import saldo_liquido_em
from tests.factories import nova_conta, novo_lancamento


def _base(db):
    conta = nova_conta(db, "Itaú", saldo=100000, inicio=date(2026, 10, 1))
    novo_lancamento(db, conta, date(2026, 10, 2), -1250, descricao="Padaria")
    novo_lancamento(db, conta, date(2026, 10, 2), -750, descricao="Feira")
    novo_lancamento(db, conta, date(2026, 10, 5), 500000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 9), -30000, descricao="Conserto", status="pendente")
    db.commit()
    return conta


def test_saldo_liquido_ao_fim_do_dia(session):
    _base(session)
    assert saldo_liquido_em(session, date(2026, 10, 2)) == 98000
    assert saldo_liquido_em(session, date(2026, 10, 9)) == 568000  # pendente entra na previsão


def test_lista_limpa_sem_faixa_de_fim_de_dia(client, db):
    _base(db)
    for url in ("/lancamentos?mes=2026-10", "/lancamentos?mes=2026-10&tipo=despesa", "/lancamentos?mes=2026-10&tipo=receita"):
        texto = client.get(url).text
        assert "Saldo do final do dia" not in texto and "Neste dia você" not in texto


def test_estado_vazio(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2027-03").text
    assert "Nenhum resultado" in texto and "Nenhum lançamento neste mês." in texto
    texto = client.get("/lancamentos?mes=2026-10&q=naoexiste").text
    assert "Nada com esses filtros" in texto and "Limpar filtros" in texto
