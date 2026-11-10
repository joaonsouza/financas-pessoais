from datetime import date

from sqlalchemy import select

from app.models import Assinatura, Lancamento
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _cartao(db):
    return novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))


def _ass(db, cartao, nome="Netflix", valor=4590):
    a = Assinatura(nome=nome, padrao=nome.upper(), valor=valor, conta_id=cartao.id, inicio=date(2026, 1, 1))
    db.add(a)
    db.commit()
    return a


def test_pagina_vazia(client, db):
    _cartao(db)
    db.commit()
    r = client.get("/lancamentos/assinaturas")
    assert r.status_code == 200 and "Nenhuma assinatura cadastrada" in r.text


def test_pagina_mostra_totais_e_listas(client, db):
    cartao = _cartao(db)
    _ass(db, cartao, "Netflix", 4590)
    cancelada = _ass(db, cartao, "Hbo Max", 3490)
    cancelada.ativa, cancelada.cancelada_em = False, date(2026, 9, 1)
    db.commit()
    r = client.get("/lancamentos/assinaturas")
    assert r.status_code == 200
    assert "Netflix" in r.text and "R$ 45,90" in r.text and "R$ 550,80" in r.text
    assert "Canceladas" in r.text and "Hbo Max" in r.text


def test_criar_editar_cancelar_reativar(client, db):
    cartao = _cartao(db)
    db.commit()
    r = client.post("/lancamentos/assinaturas", data={
        "nome": "Spotify", "valor": "40,90", "conta_id": str(cartao.id), "intervalo_meses": "1", "dia": "15"},
        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/lancamentos/assinaturas"
    a = db.scalars(select(Assinatura)).one()
    assert (a.valor, a.dia, a.ativa) == (4090, 15, True)
    r = client.post(f"/lancamentos/assinaturas/{a.id}", data={
        "nome": "Spotify", "valor": "45,00", "conta_id": str(cartao.id), "intervalo_meses": "1"},
        follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Assinatura, a.id).valor == 4500
    client.post(f"/lancamentos/assinaturas/{a.id}/cancelar", data={"em": "2026-10-01"})
    db.expire_all()
    assert (db.get(Assinatura, a.id).ativa, db.get(Assinatura, a.id).cancelada_em) == (False, date(2026, 10, 1))
    client.post(f"/lancamentos/assinaturas/{a.id}/reativar")
    db.expire_all()
    assert db.get(Assinatura, a.id).ativa is True


def test_criar_com_erro_devolve_400_e_nao_grava(client, db):
    cartao = _cartao(db)
    db.commit()
    r = client.post("/lancamentos/assinaturas", data={"nome": "", "valor": "10,00", "conta_id": str(cartao.id)})
    assert r.status_code == 400 and "informe o nome" in r.text
    assert db.scalars(select(Assinatura)).all() == []


def test_assinatura_nunca_cria_lancamento(client, db):
    cartao = _cartao(db)
    db.commit()
    antes = db.scalar(select(Lancamento.id).limit(1))
    client.post("/lancamentos/assinaturas", data={"nome": "Spotify", "valor": "40,90", "conta_id": str(cartao.id)})
    assert db.scalar(select(Lancamento.id).limit(1)) == antes


def _sequencia(db, cartao, desc):
    for dia in (date(2026, 8, 10), date(2026, 9, 10), date(2026, 10, 2)):
        novo_lancamento(db, cartao, dia, -2990, descricao=desc)
    db.commit()


def test_sugestao_cadastrar_e_ignorar(client, db):
    cartao = _cartao(db)
    _sequencia(db, cartao, "Disney Plus")
    _sequencia(db, cartao, "Hbo Max")
    html = client.get("/lancamentos/assinaturas").text
    assert "Sugestões para incluir" in html and "Disney Plus" in html and "Hbo Max" in html
    r = client.post("/lancamentos/assinaturas/sugestoes/cadastrar",
                    data={"descricao": "Disney Plus", "conta_id": str(cartao.id)}, follow_redirects=False)
    assert r.status_code == 303
    assert [a.nome for a in db.scalars(select(Assinatura))] == ["Disney Plus"]
    client.post("/lancamentos/assinaturas/sugestoes/ignorar", data={"descricao": "Hbo Max", "conta_id": str(cartao.id)})
    html = client.get("/lancamentos/assinaturas").text
    assert "Hbo Max" not in html.split("Sugestões para incluir")[-1]


def test_sugestao_inexistente_nao_cria_nada(client, db):
    cartao = _cartao(db)
    db.commit()
    r = client.post("/lancamentos/assinaturas/sugestoes/cadastrar",
                    data={"descricao": "Nada", "conta_id": str(cartao.id)}, follow_redirects=False)
    assert r.status_code == 303 and db.scalars(select(Assinatura)).all() == []


def test_lancamentos_tem_link_para_assinaturas(client, db):
    nova = _cartao(db)
    db.commit()
    assert 'href="/lancamentos/assinaturas"' in client.get("/lancamentos").text
