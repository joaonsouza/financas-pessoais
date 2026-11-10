"""Venda/resgate com imposto retido: o valor líquido (o que cai na conta), quando informado, substitui bruto − taxas."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.models import OperacaoInvest
from app.services.invest_conta import valor_da_operacao
from app.services.invest_posicao import posicao
from tests.test_invest_conta import contas, postar, transferencias
from tests.test_mercado_provedores import sem_rede_real  # noqa: F401
from tests.test_rotas_investimentos import mercado  # noqa: F401


def _op(tipo, qtd, preco, dia=1, taxas=0, liquido=None):
    return OperacaoInvest(ativo_id=1, tipo=tipo, data=date(2026, 1, dia), quantidade=D(qtd), preco_unitario=preco,
                          taxas=taxas, valor_liquido=liquido)


def test_lucro_realizado_e_valor_que_cai_na_conta_usam_o_liquido():
    compra = _op("compra", 10, 1000)
    venda = _op("venda", 10, 1500, dia=2, taxas=100, liquido=14_000)    # bruto 15.000 − taxas 100; IR retido 900
    assert posicao([compra, venda]).lucro_realizado_brl == 14_000 - 10_000
    assert valor_da_operacao(venda) == 14_000
    assert valor_da_operacao(_op("venda", 10, 1500, taxas=100)) == 14_900   # sem líquido: bruto − taxas


def test_resgate_de_renda_fixa_com_liquido():
    aplicacao = _op("aplicacao", 1, 100_000)
    resgate = _op("resgate", 1, 110_000, dia=2, liquido=108_500)       # IR de R$ 15,00 retido
    assert posicao([aplicacao, resgate]).lucro_realizado_brl == 8_500


def test_venda_pela_tela_credita_o_liquido_e_recusa_liquido_acima_do_bruto(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    postar(client)
    local = dict(conta_id=str(corrente.id), custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    r = client.post("/investimentos/operacoes", data={
        "ativo_id": "1", "tipo": "venda", "data": "2026-08-10", "quantidade": "10", "preco": "40,00", "taxas": "2,00",
        "valor_liquido": "500,00", **local})
    assert r.status_code == 422 and "líquido" in r.text                      # bruto 400 − taxas 2 = 398
    postar(client, ativo_id="1", tipo="venda", quantidade="10", preco="40,00", taxas="2,00", valor_liquido="390,00",
           **local)
    venda = db.scalar(select(OperacaoInvest).where(OperacaoInvest.tipo == "venda"))
    assert venda.valor_liquido == 39_000
    pernas = transferencias(db)
    assert pernas[corrente.id].valor == 39_000 and pernas[corretora.id].valor == -39_000
    form = client.get(f"/investimentos/operacoes/{venda.id}/editar").text
    assert 'name="valor_liquido"' in form and 'value="390,00"' in form


def test_compra_ignora_o_liquido(client, db, mercado):  # noqa: F811
    contas(db)
    postar(client, valor_liquido="10,00")
    assert db.scalar(select(OperacaoInvest)).valor_liquido is None
