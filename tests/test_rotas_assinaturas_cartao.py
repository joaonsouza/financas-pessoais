from datetime import date

from sqlalchemy import select

from app.models import Assinatura, Recorrencia
from app.services.assinaturas import criar_assinatura
from app.services.faturas import obter_ou_criar_fatura
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _cartao(db):
    return novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))


def _assinatura(db, cartao, nome="Netflix", *, cobrada_em=None):
    a = criar_assinatura(db, nome=nome, valor=4590, conta_id=cartao.id, hoje=date(2026, 1, 1))
    if cobrada_em:
        fatura = obter_ou_criar_fatura(db, cartao, cobrada_em)
        novo_lancamento(db, cartao, fatura.data_vencimento, -4590, descricao=nome, fatura_id=fatura.id)
    db.commit()
    return a


def _sequencia(db, cartao, desc):
    obter_ou_criar_fatura(db, cartao, "2026-10")
    for dia in (date(2026, 8, 10), date(2026, 9, 10), date(2026, 10, 2)):
        novo_lancamento(db, cartao, dia, -2990, descricao=desc)
    db.commit()


def test_painel_mostra_assinatura_confirmada(client, db):
    cartao = _cartao(db)
    _assinatura(db, cartao, cobrada_em="2026-10")
    r = client.get(f"/cartoes/{cartao.id}?fatura=2026-10")
    assert r.status_code == 200
    assert "Assinaturas desta fatura" in r.text and "Netflix" in r.text and "Confirmada" in r.text


def test_painel_mostra_nao_apareceu_e_aguardando(client, db):
    cartao = _cartao(db)
    _assinatura(db, cartao)
    obter_ou_criar_fatura(db, cartao, "2026-10")
    obter_ou_criar_fatura(db, cartao, "2026-11")
    db.commit()
    assert "Não apareceu" in client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    assert "Aguardando" in client.get(f"/cartoes/{cartao.id}?fatura=2026-11").text


def test_sem_assinaturas_mostra_mensagem_e_link(client, db):
    cartao = _cartao(db)
    obter_ou_criar_fatura(db, cartao, "2026-10")
    db.commit()
    r = client.get(f"/cartoes/{cartao.id}?fatura=2026-10")
    assert "Nenhuma assinatura cadastrada neste cartão" in r.text and 'href="/lancamentos/assinaturas"' in r.text


def test_sugestao_aparece_e_cadastrar_cria_a_assinatura(client, db):
    cartao = _cartao(db)
    _sequencia(db, cartao, "Disney Plus")
    html = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    assert "Parece assinatura, mas não está cadastrada" in html and "Disney Plus" in html
    r = client.post(f"/cartoes/{cartao.id}/assinaturas/cadastrar?fatura=2026-10", data={"descricao": "Disney Plus"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/cartoes/{cartao.id}?fatura=2026-10"
    [a] = db.scalars(select(Assinatura)).all()
    assert (a.nome, a.conta_id, a.ativa) == ("Disney Plus", cartao.id, True)
    assert db.scalars(select(Recorrencia)).all() == []  # assinatura é só controle


def test_ignorar_sugestao_some_da_pagina(client, db):
    cartao = _cartao(db)
    _sequencia(db, cartao, "Disney Plus")
    r = client.post(f"/cartoes/{cartao.id}/assinaturas/ignorar?fatura=2026-10", data={"descricao": "Disney Plus"},
                    follow_redirects=False)
    assert r.status_code == 303
    html = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    assert "Parece assinatura, mas não está cadastrada" not in html
    assert db.scalars(select(Assinatura)).all() == []


def test_cadastrar_sugestao_inexistente_nao_cria_nada(client, db):
    cartao = _cartao(db)
    obter_ou_criar_fatura(db, cartao, "2026-10")
    db.commit()
    r = client.post(f"/cartoes/{cartao.id}/assinaturas/cadastrar?fatura=2026-10", data={"descricao": "Nada"},
                    follow_redirects=False)
    assert r.status_code == 303 and db.scalars(select(Assinatura)).all() == []
