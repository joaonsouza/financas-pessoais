"""Melhor cartão para comprar hoje. Dados fictícios."""
from datetime import date

from app.services.faturas import melhor_cartao_hoje
from tests.factories import nova_conta, novo_cartao


def _cartoes(s, *dias):
    conta = nova_conta(s, "Conta")
    for i, (fech, venc, limite) in enumerate(dias):
        novo_cartao(s, f"Cartão {i}", pagadora=conta, fechamento=fech, vencimento=venc, limite=limite)
    s.commit()


def test_escolhe_o_que_acabou_de_fechar(session):
    # hoje 07/10: o que fecha dia 5 já fechou (compra vai para a fatura de novembro, vence 15/11);
    # o que fecha dia 23 ainda não (vence 01/11)
    _cartoes(session, (5, 15, 100000), (23, 1, 100000))
    m = melhor_cartao_hoje(session, date(2026, 10, 7))
    assert (m["cartao"].nome, m["vencimento"], m["dias"]) == ("Cartão 0", date(2026, 11, 15), 39)


def test_empate_vence_o_de_maior_limite(session):
    _cartoes(session, (5, 15, 100000), (5, 15, 300000))
    assert melhor_cartao_hoje(session, date(2026, 10, 7))["cartao"].nome == "Cartão 1"


def test_fechamento_no_fim_do_mes_e_fevereiro(session):
    # fecha dia 30/31: em fevereiro o fechamento vira o último dia do mês
    _cartoes(session, (30, 8, 100000), (31, 10, 100000))
    m = melhor_cartao_hoje(session, date(2027, 2, 28))
    assert m["dias"] > 0 and m["vencimento"] > date(2027, 2, 28)
    m = melhor_cartao_hoje(session, date(2027, 3, 1))  # os dois já fecharam em 28/02
    assert m["vencimento"] >= date(2027, 4, 8)


def test_sem_cartoes(session):
    assert melhor_cartao_hoje(session, date(2026, 10, 7)) is None


def test_tela_de_cartoes_com_resumo_e_novo_cartao(client, db):
    _cartoes(db, (5, 15, 100000))
    texto = client.get("/cartoes").text
    assert "O melhor cartão para comprar hoje é" in texto and "Cartão 0" in texto
    assert ">Novo cartão de crédito<" in texto and ">Faturas fechadas<" in texto
    assert "Limite disponível" in texto and "Valor total das faturas abertas" in texto
    assert 'hx-get="/lancamentos/novo?tipo=cartao&conta=' in texto


def test_cartao_fora_da_sugestao_nao_e_escolhido(session):
    _cartoes(session, (5, 15, 100000), (23, 1, 100000))
    from sqlalchemy import select

    from app.models import Conta
    session.scalars(select(Conta).where(Conta.nome == "Cartão 0")).one().sugerir_compra = False
    session.commit()
    assert melhor_cartao_hoje(session, date(2026, 10, 7))["cartao"].nome == "Cartão 1"


def test_editar_cartao_liga_e_desliga_a_sugestao(client, db):
    _cartoes(db, (5, 15, 100000))
    from sqlalchemy import select

    from app.models import Conta
    cartao = db.scalars(select(Conta).where(Conta.nome == "Cartão 0")).one()
    assert 'name="sugerir_compra"' in client.get(f"/cartoes/{cartao.id}").text
    dados = {"nome": "Cartão 0", "cor": "#6366f1", "limite": "1000,00", "fechamento": "5", "vencimento": "15",
             "pagadora_id": str(cartao.conta_pagamento_id)}
    client.post(f"/cartoes/{cartao.id}/editar", data={**dados, "sugerir_compra": "0"})
    db.refresh(cartao)
    assert cartao.sugerir_compra is False
    client.post(f"/cartoes/{cartao.id}/editar", data={**dados, "sugerir_compra": ["0", "1"]})
    db.refresh(cartao)
    assert cartao.sugerir_compra is True
