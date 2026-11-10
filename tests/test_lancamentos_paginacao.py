from datetime import date

from tests.factories import nova_conta, novo_lancamento


def _trinta(db):
    conta = nova_conta(db)
    for i in range(1, 31):
        novo_lancamento(db, conta, date(2026, 10, i), -100 * i, descricao=f"Compra {i:02d}")
    db.commit()


def _tabela(texto: str) -> str:
    return texto.split('id="tabela-lancamentos"', 1)[1]


def test_padrao_mostra_ate_50_linhas(client, db):
    _trinta(db)
    tabela = _tabela(client.get("/lancamentos?mes=2026-10").text)

    assert "1–30 de 30" in tabela
    assert "Compra 01" in tabela and "Compra 30" in tabela


def test_pagina_com_25_por_pagina(client, db):
    _trinta(db)
    primeira = _tabela(client.get("/lancamentos?mes=2026-10&por=25").text)
    segunda = _tabela(client.get("/lancamentos?mes=2026-10&por=25&pagina=2").text)

    # data crescente: a primeira página tem os dias 1 a 25
    assert "1–25 de 30" in primeira
    assert "Compra 01" in primeira and "Compra 25" in primeira and "Compra 26" not in primeira
    assert "pagina=2" in primeira
    assert "26–30 de 30" in segunda
    assert "Compra 26" in segunda and "Compra 25" not in segunda


def test_pagina_fora_do_intervalo_vai_para_a_ultima(client, db):
    _trinta(db)
    tabela = _tabela(client.get("/lancamentos?mes=2026-10&por=25&pagina=9").text)

    assert "26–30 de 30" in tabela


def test_por_pagina_se_mantem_nos_links(client, db):
    _trinta(db)
    texto = client.get("/lancamentos?mes=2026-10&por=25").text

    # ordenar por valor e trocar de mês continuam com 25 por página
    assert "ordem=valor" in texto and "por=25" in texto
    assert "mes=2026-11&amp;por=25" in texto
