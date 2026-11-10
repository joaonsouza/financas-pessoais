from app.models.config import get_config
from app.services.faturas import obter_ou_criar_fatura
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _cartao_com_parcelas(db):
    cartao = novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))
    for num, comp in ((2, "2026-10"), (3, "2026-11")):
        f = obter_ou_criar_fatura(db, cartao, comp)
        l = novo_lancamento(db, cartao, f.data_vencimento, -10000, status="pendente", descricao="Tv Sala",
                            fatura_id=f.id)
        l.grupo_parcela_id, l.parcela_num, l.parcela_total = "g1", num, 4
    db.commit()
    return cartao


def test_pagina_vazia_abre(client, db):
    nova_conta(db)
    db.commit()
    r = client.get("/cartoes/parcelas")
    assert r.status_code == 200
    assert "Nenhuma parcela a vencer" in r.text and 'value="2000,00"' in r.text


def test_pagina_com_dados(client, db):
    cartao = _cartao_com_parcelas(db)
    r = client.get("/cartoes/parcelas")
    assert r.status_code == 200
    assert "Tv Sala" in r.text and "200,00" in r.text and "Cartão Teste" in r.text
    assert "grafico-parcelas" in r.text
    assert "&#34;descricao&#34;" in r.text
    so = client.get(f"/cartoes/parcelas?cartao={cartao.id}")
    assert so.status_code == 200 and "Tv Sala" in so.text


def test_filtro_por_cartao_exclui_o_outro(client, db):
    cartao_a = novo_cartao(db, "Cartão A", pagadora=nova_conta(db, "Conta A"))
    cartao_b = novo_cartao(db, "Cartão B", pagadora=nova_conta(db, "Conta B"))
    for cartao, desc, grupo in ((cartao_a, "Notebook Alfa", "ga"), (cartao_b, "Geladeira Beta", "gb")):
        f = obter_ou_criar_fatura(db, cartao, "2026-10")
        l = novo_lancamento(db, cartao, f.data_vencimento, -10000, status="pendente", descricao=desc,
                            fatura_id=f.id)
        l.grupo_parcela_id, l.parcela_num, l.parcela_total = grupo, 2, 4
    db.commit()
    r = client.get(f"/cartoes/parcelas?cartao={cartao_a.id}")
    assert r.status_code == 200
    assert "Notebook Alfa" in r.text and "Geladeira Beta" not in r.text


def test_filtro_de_cartao_inexistente_mostra_todos(client, db):
    _cartao_com_parcelas(db)
    r = client.get("/cartoes/parcelas?cartao=9999")
    assert r.status_code == 200 and "Tv Sala" in r.text


def test_filtro_todos_com_valor_vazio(client, db):
    _cartao_com_parcelas(db)
    r = client.get("/cartoes/parcelas?cartao=")
    assert r.status_code == 200 and "Tv Sala" in r.text


def test_filtro_com_cartao_arquivado_mostra_todos(client, db):
    cartao = _cartao_com_parcelas(db)
    outro = novo_cartao(db, "Cartão Velho", pagadora=nova_conta(db, "Conta Velha"))
    outro.ativa = False
    db.commit()
    r = client.get(f"/cartoes/parcelas?cartao={outro.id}")
    assert r.status_code == 200 and "Tv Sala" in r.text and cartao.nome in r.text


def test_aviso_de_sobras_na_rota(client, db):
    cartao = novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))
    f = obter_ou_criar_fatura(db, cartao, "2026-08")
    l = novo_lancamento(db, cartao, f.data_vencimento, -7000, status="pendente", descricao="Velha",
                        fatura_id=f.id)
    l.grupo_parcela_id, l.parcela_num, l.parcela_total = "v", 2, 3
    db.commit()
    r = client.get("/cartoes/parcelas")
    assert r.status_code == 200
    assert "Parcelas antigas ainda pendentes" in r.text and "Velha" in r.text


def test_pagina_vazia_mostra_numeros_zerados(client, db):
    nova_conta(db)
    db.commit()
    r = client.get("/cartoes/parcelas")
    assert "Nenhuma parcela a vencer" in r.text
    assert "Falta pagar em parcelas" in r.text and "Meses acima do teto" in r.text
    assert "grafico-parcelas" not in r.text


def test_rota_nao_e_capturada_por_cartao_id(client, db):
    nova_conta(db)
    db.commit()
    assert client.get("/cartoes/parcelas").status_code == 200


def test_salvar_teto(client, db):
    nova_conta(db)
    db.commit()
    r = client.post("/cartoes/parcelas/teto", data={"teto": "3.500,00"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cartoes/parcelas"
    db.expire_all()
    assert get_config(db, "teto_parcelas") == 350000


def test_teto_invalido_ou_negativo_nao_grava(client, db):
    nova_conta(db)
    db.commit()
    for texto in ("abc", "-5", ""):
        r = client.post("/cartoes/parcelas/teto", data={"teto": texto}, follow_redirects=False)
        assert r.status_code == 422, texto
    db.expire_all()
    assert get_config(db, "teto_parcelas") is None


def test_lista_de_cartoes_tem_link_para_parcelas(client, db):
    nova_conta(db)
    db.commit()
    assert 'href="/cartoes/parcelas"' in client.get("/cartoes").text
