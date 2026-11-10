"""Máscara de reais: os campos de dinheiro levam data-mascara (o valor do lançamento, que aceita contas, em modo
"conta"); quantidade, câmbio e percentuais ficam sem máscara."""
from datetime import date

from app.core.dinheiro import avaliar_valor, parse_reais
from tests.factories import nova_conta


def test_formato_da_mascara_e_aceito_pelo_servidor():
    assert parse_reais("1.234,56") == 123456 and parse_reais("-5,00") == -500
    assert avaliar_valor("12,50+0,08") == 1258


def test_paginas_carregam_o_script_e_marcam_os_campos_de_dinheiro(client, db):
    nova_conta(db)
    db.commit()
    lanc = client.get("/lancamentos/novo").text
    assert 'data-mascara="conta" name="valor"' in lanc
    assert '/static/mascara.js' in client.get("/investimentos").text
    op = client.get("/investimentos/operacoes/nova").text
    assert 'data-mascara="reais" name="preco"' in op and 'data-mascara="reais" name="taxas"' in op
    for campo in ("quantidade", "cambio", "taxa", "taxa_op"):
        assert f'data-mascara="reais" name="{campo}"' not in op
