"""Editar conta e cartão. Dados fictícios."""
from datetime import date

from sqlalchemy import select

from app.models import Conta, Fatura
from app.services.faturas import obter_ou_criar_fatura
from tests.factories import nova_conta, novo_cartao


def test_editar_conta(client, db):
    conta = nova_conta(db, "Itaú")
    nova_conta(db, "Nubank")
    db.commit()
    assert f'action="/contas/{conta.id}/editar' in client.get("/contas").text
    r = client.post(f"/contas/{conta.id}/editar", data={"nome": "Itaú Corrente", "cor": "#ff7a00"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.refresh(conta)
    assert (conta.nome, conta.cor, conta.entra_saldo_liquido) == ("Itaú Corrente", "#ff7a00", False)
    r = client.post(f"/contas/{conta.id}/editar", data={"nome": "Nubank", "sl": "on"})
    assert r.status_code == 400 and "já existe" in r.text


def test_editar_cartao_recalcula_faturas_que_ainda_vao_fechar(client, db):
    itau = nova_conta(db, "Itaú")
    nubank = nova_conta(db, "Nubank")
    cartao = novo_cartao(db, "Cartão", pagadora=itau, fechamento=3, vencimento=10)
    fechada = obter_ou_criar_fatura(db, cartao, "2026-10")   # fechou 03/10 (hoje dos testes: 05/10)
    futura = obter_ou_criar_fatura(db, cartao, "2026-11")    # fecha 03/11
    db.commit()
    r = client.post(f"/cartoes/{cartao.id}/editar?fatura=2026-11", data={
        "nome": "Cartão Itaú", "cor": "#ff7a00", "limite": "5.000,00", "fechamento": "25", "vencimento": "2",
        "pagadora_id": str(nubank.id)}, follow_redirects=False)
    assert r.status_code == 303
    db.refresh(cartao); db.refresh(fechada); db.refresh(futura)
    assert (cartao.nome, cartao.limite, cartao.dia_fechamento, cartao.dia_vencimento, cartao.conta_pagamento_id) == (
        "Cartão Itaú", 500000, 25, 2, nubank.id)
    assert (futura.data_fechamento, futura.data_vencimento) == (date(2026, 11, 25), date(2026, 12, 2))
    assert fechada.data_fechamento == date(2026, 10, 3)  # a que já fechou não muda


def test_editar_cartao_valida(client, db):
    itau = nova_conta(db, "Itaú")
    cartao = novo_cartao(db, "Cartão", pagadora=itau)
    db.commit()
    r = client.post(f"/cartoes/{cartao.id}/editar", data={
        "nome": "Cartão", "limite": "1,00", "fechamento": "40", "vencimento": "2", "pagadora_id": str(itau.id)})
    assert r.status_code == 400 and "Fechamento" in r.text
