from datetime import date
from decimal import Decimal as D

import pytest

from app.models import Meta, Recorrencia, SerieIndicador
from app.services.assinaturas import criar_assinatura
from app.services.desempenho.inflacao import (
    CONSUMO, FIXO, gasto_no_mes, grafico_inflacao, inflacao_pessoal, itens_da_cesta, preco_no_mes,
)
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 11, 5)


@pytest.fixture
def conta(db):
    return nova_conta(db)


@pytest.fixture
def cat(db):
    return {"fixo": nova_categoria(db, "Aluguel", inflacao="fixo"),
            "consumo": nova_categoria(db, "Energia", inflacao="consumo"),
            "fora": nova_categoria(db, "Mercado")}


def pagar(db, conta, categoria, dia: str, reais, descricao="Conta", **kw):
    return novo_lancamento(db, conta, date.fromisoformat(dia), -int(D(str(reais)) * 100), tipo="despesa",
                           categoria=categoria, descricao=descricao, **kw)


# ---------- itens ----------

def test_descricoes_do_mesmo_estabelecimento_viram_um_item(db, conta, cat):
    pagar(db, conta, cat["fixo"], "2026-09-13", "193.49", "NETPGT*FaturaClaro")
    pagar(db, conta, cat["fixo"], "2026-10-13", "195.22", "Net Pgt*fatura Claro")
    [item] = itens_da_cesta(db, HOJE)
    assert item.nome == "Net Pgt*fatura Claro"           # descrição mais recente
    assert item.tipo == FIXO and [p.valor for p in item.pagamentos] == [19349, 19522]


def test_item_de_um_mes_so_e_categoria_fora_ficam_de_fora(db, conta, cat):
    pagar(db, conta, cat["fixo"], "2026-10-10", 1200, "Aluguel")
    pagar(db, conta, cat["fora"], "2026-09-03", 300, "Supermercado")
    pagar(db, conta, cat["fora"], "2026-10-03", 320, "Supermercado")
    assert itens_da_cesta(db, HOJE) == []


def test_pendente_ignorado_e_futuro_nao_contam(db, conta, cat):
    pagar(db, conta, cat["fixo"], "2026-09-10", 1200, "Aluguel")
    pagar(db, conta, cat["fixo"], "2026-10-10", 1200, "Aluguel", status="pendente")
    pagar(db, conta, cat["fixo"], "2026-10-11", 1200, "Aluguel", ignorar=True)
    pagar(db, conta, cat["fixo"], "2026-11-10", 1200, "Aluguel")   # depois de hoje
    assert itens_da_cesta(db, date(2026, 11, 1)) == []


def test_subcategoria_sem_marcacao_herda_da_mae(db, conta):
    moradia = nova_categoria(db, "Moradia", inflacao="consumo")
    gas = nova_categoria(db, "Botijão", pai=moradia)
    pagar(db, conta, gas, "2026-09-08", 120, "Gás")
    pagar(db, conta, gas, "2026-10-08", 130, "Gás")
    [item] = itens_da_cesta(db, HOJE)
    assert item.tipo == CONSUMO


def test_recorrencia_junta_descricoes_diferentes(db, conta, cat):
    rec = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=120000, dia=10, inicio="2026-09")
    db.add(rec)
    db.flush()
    for dia, desc in (("2026-09-10", "PIX TRANSF JOSE S"), ("2026-10-10", "PIX JOSE DA SILVA")):
        pagar(db, conta, cat["fixo"], dia, 1200, desc).recorrencia_id = rec.id
    [item] = itens_da_cesta(db, HOJE)
    assert item.nome == "Aluguel" and len(item.pagamentos) == 2


def test_recorrencia_de_reserva_fica_fora(db, conta, cat):
    meta = Meta(nome="IPTU", tipo="objetivo")
    db.add(meta)
    db.flush()
    rec = Recorrencia(descricao="Meta: IPTU", tipo="despesa", conta_id=conta.id, valor=14286, dia=5,
                      inicio="2026-09", origem_meta_id=meta.id)
    db.add(rec)
    db.flush()
    for dia in ("2026-09-05", "2026-10-05"):
        pagar(db, conta, cat["fixo"], dia, "142.86", "Meta: IPTU").recorrencia_id = rec.id
    assert itens_da_cesta(db, HOJE) == []


def test_assinatura_junta_cobrancas_pelo_padrao(db, conta):
    assinaturas = nova_categoria(db, "Assinaturas", inflacao="fixo")
    criar_assinatura(db, nome="Spotify", valor=4090, conta_id=conta.id, hoje=HOJE, padrao="EBN*SPOTIFY")
    pagar(db, conta, assinaturas, "2026-09-29", "40.90", "Ebn*Spotify")
    pagar(db, conta, assinaturas, "2026-10-29", "40.90", "EBN*SPOTIFY P1A2B")
    [item] = itens_da_cesta(db, HOJE)
    assert item.nome == "Spotify" and item.chave.startswith("ass:")


# ---------- preço ----------

def test_preco_fixo_repete_e_some_dois_meses_depois(db, conta, cat):
    pagar(db, conta, cat["fixo"], "2026-05-10", 1000, "Aluguel")
    pagar(db, conta, cat["fixo"], "2026-06-10", 1100, "Aluguel")
    [item] = itens_da_cesta(db, HOJE)
    assert preco_no_mes(item, "2026-04") is None
    assert preco_no_mes(item, "2026-05") == 100000
    assert preco_no_mes(item, "2026-08") == 110000      # repete até 2 meses depois do último
    assert preco_no_mes(item, "2026-09") is None        # saiu da cesta


def test_consumo_usa_a_media_dos_12_meses(db, conta, cat):
    pagar(db, conta, cat["consumo"], "2025-11-05", 300, "Neoenergia")
    pagar(db, conta, cat["consumo"], "2026-09-05", 400, "Neoenergia")
    pagar(db, conta, cat["consumo"], "2026-10-05", 500, "Neoenergia")
    [item] = itens_da_cesta(db, HOJE)
    assert preco_no_mes(item, "2026-10") == 40000       # nov/25, set e out
    assert preco_no_mes(item, "2026-11") == 45000       # nov/25 saiu da janela


def test_faxina_semanal_preco_e_ultimo_pagamento_e_gasto_e_o_total(db, conta):
    faxina = nova_categoria(db, "Faxina", inflacao="fixo")
    for dia in ("2026-09-03", "2026-09-10", "2026-09-17", "2026-09-24", "2026-10-01"):
        pagar(db, conta, faxina, dia, 185, "Faxina Dª Lourdes")
    [item] = itens_da_cesta(db, HOJE)
    assert preco_no_mes(item, "2026-09") == 18500
    assert gasto_no_mes(item, "2026-09") == 74000
    assert gasto_no_mes(item, "2026-11") == 18500       # mês sem pagamento usa o preço


# ---------- índice ----------

def _aluguel_e_internet(db, conta, cat, aluguel_out=1100):
    internet = nova_categoria(db, "Internet/Celular", inflacao="fixo")
    for mes, valor in (("2026-08", 1000), ("2026-09", 1000), ("2026-10", aluguel_out)):
        pagar(db, conta, cat["fixo"], f"{mes}-10", valor, "Aluguel")
        pagar(db, conta, internet, f"{mes}-15", 100, "Claro")


def test_reajuste_entra_ponderado_pelo_gasto_do_mes_anterior(db, conta, cat):
    _aluguel_e_internet(db, conta, cat)
    r = inflacao_pessoal(db, "2026-10", HOJE)
    assert [m.competencia for m in r.meses] == ["2026-09", "2026-10"]
    assert r.meses[0].pessoal == 0
    assert float(r.meses[1].pessoal) == pytest.approx(1000 / 1100 * 0.10)
    assert float(r.acumulado_pessoal) == pytest.approx(1000 / 1100 * 0.10)
    assert r.rotulo == "desde set/26" and r.meses_sem_dados == 0


def test_resumo_por_item_ordenado_pela_contribuicao(db, conta, cat):
    _aluguel_e_internet(db, conta, cat)
    aluguel, claro = inflacao_pessoal(db, "2026-10", HOJE).itens
    assert aluguel.nome == "Aluguel" and float(aluguel.variacao) == pytest.approx(0.10)
    assert aluguel.ultimo_reajuste[0] == "2026-10" and float(aluguel.ultimo_reajuste[1]) == pytest.approx(0.10)
    assert float(aluguel.peso) == pytest.approx(1100 / 1200)
    assert claro.variacao == 0 and claro.ultimo_reajuste is None


def test_fim_vira_o_ultimo_mes_fechado(db, conta, cat):
    _aluguel_e_internet(db, conta, cat)
    r = inflacao_pessoal(db, "2026-11", HOJE)      # novembro ainda não fechou
    assert r.meses[-1].competencia == "2026-10"


def test_mes_com_menos_de_dois_itens_e_dados_insuficientes(db, conta, cat):
    internet = nova_categoria(db, "Internet/Celular", inflacao="fixo")
    for mes in ("2026-06", "2026-07"):
        pagar(db, conta, internet, f"{mes}-15", 100, "Claro")
    for mes in ("2026-06", "2026-07", "2026-08", "2026-09", "2026-10"):
        pagar(db, conta, cat["fixo"], f"{mes}-10", 1000, "Aluguel")
    r = inflacao_pessoal(db, "2026-10", HOJE)      # Claro sai da cesta em out (2 meses depois de jul)
    assert r.meses[0].competencia == "2026-07"
    assert r.meses[-1].pessoal is None and r.meses[-1].comparaveis == 1
    assert r.meses_sem_dados == 1 and r.acumulado_pessoal == 0


def test_ipca_acumula_ate_o_ultimo_mes_publicado(db, conta, cat):
    _aluguel_e_internet(db, conta, cat)
    db.add(SerieIndicador(serie="ipca", data=date(2026, 9, 1), valor_pct=D("0.48")))
    db.flush()
    r = inflacao_pessoal(db, "2026-10", HOJE)
    assert r.meses[0].ipca == D("0.0048") and r.meses[1].ipca is None
    assert r.acumulado_ipca == D("0.0048") and r.ipca_ate == "2026-09"
    g = grafico_inflacao(r)
    assert g["rotulos"] == ["set/26", "out/26"]
    assert g["acum_ipca"] == [pytest.approx(0.48), None]
    assert g["pessoal"][1] == pytest.approx(100 * 1000 / 1100 * 0.10)


def test_sem_itens_nao_tem_meses(db, conta, cat):
    r = inflacao_pessoal(db, "2026-10", HOJE)
    assert r.meses == [] and r.acumulado_pessoal is None and r.itens == []
