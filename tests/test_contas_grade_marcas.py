"""Tela de Contas em grade e marcas dos bancos. Dados fictícios."""
from app.web.marcas import marca
from tests.factories import nova_conta


def test_marcas_conhecidas_e_inicial():
    assert marca("Nubank")["sigla"] == "nu" and marca("Cartão Nubank")["fundo"] == "#820AD1"
    assert marca("Itaú Corrente")["sigla"] == "itaú"
    assert marca("NuInvest")["fundo"] == "#4A0F7C"
    assert marca("Caixinha Provisões", "#123456") == {"sigla": "C", "fundo": "#123456", "frente": "#FFFFFF"}


def test_contas_em_grade_com_nova_conta_e_adicionar_despesa(client, db):
    conta = nova_conta(db, "Nubank", saldo=12345)
    db.commit()
    texto = client.get("/contas").text
    assert ">Nova conta<" in texto
    assert f'hx-get="/lancamentos/novo?tipo=despesa&conta={conta.id}"' in texto and "Adicionar despesa" in texto
    assert "background: #820AD1" in texto  # avatar do Nubank
    assert "Saldo previsto" in texto and "text-income" in texto
