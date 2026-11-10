from datetime import date

import pytest
from sqlalchemy import select

from app.models import Categoria, Lancamento, RegraCategoria
from tests.factories import nova_categoria, nova_conta, novo_lancamento


@pytest.fixture
def cenario(db):
    conta = nova_conta(db)
    alim = nova_categoria(db, "Alimentação")
    mercado = nova_categoria(db, "Mercado", pai=alim)
    feira = nova_categoria(db, "Feira", pai=alim)
    moradia = nova_categoria(db, "Moradia")
    salario = nova_categoria(db, "Salário", tipo="receita")
    novo_lancamento(db, conta, date(2026, 10, 1), -1000, categoria=mercado)
    regra = RegraCategoria(padrao="ATACADAO", categoria_id=mercado.id, criada_de="Atacadão SP")
    db.add(regra)
    db.commit()
    return {"alim": alim, "mercado": mercado, "feira": feira, "moradia": moradia, "salario": salario, "regra": regra}


def test_arvore_e_painel(client, db, cenario):
    c = cenario
    texto = client.get("/configuracoes?aba=categorias").text
    assert 'data-arvore-categorias' in texto and "Mercado" in texto and "Salário" not in texto
    assert "1 · R$ 10,00" in texto                                   # uso de 12 meses (mãe e filha)
    texto = client.get(f"/configuracoes?aba=categorias&cat={c['mercado'].id}").text
    assert f'action="/configuracoes/categorias/{c["mercado"].id}"' in texto
    assert f'href="/lancamentos?categoria={c["mercado"].id}"' in texto
    assert "ATACADAO" in texto and "Atacadão SP" in texto             # regra e de onde veio
    assert "Salário" in client.get("/configuracoes?aba=categorias&tipo=receita").text


def test_salvar_volta_ao_painel(client, db, cenario):
    m = cenario["mercado"]
    r = client.post(f"/configuracoes/categorias/{m.id}", data={
        "nome": "Supermercado", "grupo": "variavel", "natureza": "necessidade", "ativa": "on",
        "pai_id": str(cenario["alim"].id)})
    assert r.status_code == 200 and f"cat={m.id}" in str(r.url)
    db.refresh(m)
    assert m.nome == "Supermercado"


def test_nova_categoria_abre_o_painel_dela(client, db, cenario):
    r = client.post("/configuracoes/categorias", data={"nome": "Pets", "tipo": "despesa", "grupo": "variavel",
                                                       "natureza": "desejo", "pai_id": ""})
    nova = db.scalar(select(Categoria).where(Categoria.nome == "Pets"))
    assert nova is not None and f"cat={nova.id}" in str(r.url)


def test_mover_por_arraste(client, db, cenario):
    c = cenario
    r = client.post(f"/configuracoes/categorias/{c['feira'].id}/mover", data={"pai_id": str(c["moradia"].id)})
    assert r.status_code == 200 and r.json() == {"ok": True}
    db.refresh(c["feira"])
    assert c["feira"].pai_id == c["moradia"].id
    r = client.post(f"/configuracoes/categorias/{c['alim'].id}/mover", data={"pai_id": str(c["moradia"].id)})
    assert r.status_code == 400 and "subcategorias" in r.json()["erro"]


def test_juntar_e_excluir(client, db, cenario):
    c = cenario
    mercado_id, feira_id = c["mercado"].id, c["feira"].id
    r = client.post(f"/configuracoes/categorias/{mercado_id}/remover", data={"destino_id": str(feira_id)})
    assert r.status_code == 200 and "juntada em" in r.text and "1 lançamento" in r.text
    db.expire_all()
    assert db.get(Categoria, mercado_id) is None and db.scalar(select(Lancamento.categoria_id)) == feira_id
    r = client.post(f"/configuracoes/categorias/{feira_id}/remover", data={"destino_id": ""})
    assert r.status_code == 200 and "excluída" in r.text
    db.expire_all()
    assert db.scalar(select(Lancamento.categoria_id)) is None
    r = client.post(f"/configuracoes/categorias/{c['alim'].id}/remover", data={"destino_id": ""})
    assert r.status_code == 200 and "excluída" in r.text            # já sem filhas


def test_depois_de_juntar_a_pagina_e_um_get_e_a_mensagem_aparece_uma_vez(client, db, cenario):
    c = cenario
    r = client.post(f"/configuracoes/categorias/{c['mercado'].id}/remover", data={"destino_id": str(c["feira"].id)},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"cat={c['feira'].id}")   # F5 não reenvia
    texto = client.get(r.headers["location"]).text
    assert "“Mercado” juntada em “Feira”" in texto
    assert "juntada em" not in client.get(r.headers["location"]).text                        # só uma vez


def test_remover_recusa_e_mostra_erro(client, db, cenario):
    c = cenario
    r = client.post(f"/configuracoes/categorias/{c['alim'].id}/remover", data={"destino_id": ""})
    assert r.status_code == 400 and "subcategorias" in r.text
    assert db.get(Categoria, c["alim"].id) is not None


def test_trocar_e_apagar_regra(client, db, cenario):
    c = cenario
    regra_id = c["regra"].id
    r = client.post(f"/configuracoes/regras/{regra_id}", data={"categoria_id": str(c["feira"].id)})
    assert r.status_code == 200 and f"cat={c['mercado'].id}" in str(r.url)
    db.expire_all()
    assert db.get(RegraCategoria, regra_id).categoria_id == c["feira"].id
    r = client.post(f"/configuracoes/regras/{regra_id}", data={"categoria_id": str(c["salario"].id)})
    assert r.status_code == 400
    client.post(f"/configuracoes/regras/{regra_id}/apagar")
    db.expire_all()
    assert db.get(RegraCategoria, regra_id) is None
