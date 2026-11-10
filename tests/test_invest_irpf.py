from datetime import date
from decimal import Decimal as D

from app.models import Ativo, OperacaoInvest
from app.services.invest_irpf import classificar_rfb, relatorio_bens_e_direitos
from tests.factories import nova_conta


def test_classificacao_rfb_por_classe():
    assert classificar_rfb(Ativo(classe="acao", codigo="PETR4")).grupo_cod == "03"
    assert classificar_rfb(Ativo(classe="acao", codigo="PETR4")).codigo_cod == "01"
    assert classificar_rfb(Ativo(classe="fii", codigo="HGLG11")).grupo_cod == "07"
    assert classificar_rfb(Ativo(classe="fii", codigo="HGLG11")).codigo_cod == "03"
    assert classificar_rfb(Ativo(classe="etf", codigo="BOVA11", alocacao="acoes")).codigo_cod == "09"
    assert classificar_rfb(Ativo(classe="etf", codigo="B5P211", alocacao="renda_fixa")).codigo_cod == "06"
    ext = classificar_rfb(Ativo(classe="exterior", codigo="VT"))
    assert (ext.grupo_cod, ext.localizacao_cod) == ("03", "249")
    assert classificar_rfb(Ativo(classe="cripto", codigo="BTC")).codigo_cod == "01"
    assert classificar_rfb(Ativo(classe="cripto", codigo="ETH")).codigo_cod == "02"
    assert classificar_rfb(Ativo(classe="cripto", codigo="USDC")).codigo_cod == "03"
    assert classificar_rfb(Ativo(classe="renda_fixa", codigo="CDB Banco X")).codigo_cod == "02"
    assert classificar_rfb(Ativo(classe="renda_fixa", codigo="LCI Itaú 95% CDI")).codigo_cod == "03"


def test_relatorio_bens_e_direitos_calcula_31_12_anterior_e_atual(db):
    corretora = nova_conta(db, nome="Itaú Corretora", tipo="investimento")
    petr = Ativo(classe="acao", alocacao="acoes", codigo="PETR4", nome="Petrobras PN", corretora_conta_id=corretora.id)
    vale = Ativo(classe="acao", alocacao="acoes", codigo="VALE3", nome="Vale ON")
    cdb = Ativo(
        classe="renda_fixa", alocacao="renda_fixa", codigo="CDB C6 105%", indexador="cdi", taxa=D("105"),
        vencimento=date(2027, 6, 1),
    )
    db.add_all([petr, vale, cdb])
    db.flush()

    # PETR4: comprou 100 em 2024 a R$ 30,00 + comprou 50 em 2025 a R$ 36,00
    db.add_all([
        OperacaoInvest(
            ativo_id=petr.id, tipo="compra", data=date(2024, 5, 10), quantidade=D(100), preco_unitario=3000,
            custodia_conta_id=corretora.id,
        ),
        OperacaoInvest(
            ativo_id=petr.id, tipo="compra", data=date(2025, 3, 15), quantidade=D(50), preco_unitario=3600,
            custodia_conta_id=corretora.id,
        ),
        # VALE3: comprou em 2024 e vendeu tudo em 2025 (deve aparecer com saldo em 31/12/2024 e R$ 0 em 31/12/2025)
        OperacaoInvest(
            ativo_id=vale.id, tipo="compra", data=date(2024, 8, 1), quantidade=D(40), preco_unitario=6000,
            custodia_conta_id=corretora.id,
        ),
        OperacaoInvest(
            ativo_id=vale.id, tipo="venda", data=date(2025, 7, 1), quantidade=D(40), preco_unitario=6500,
            custodia_conta_id=corretora.id,
        ),
        # CDB: aplicado em 2025
        OperacaoInvest(
            ativo_id=cdb.id, tipo="aplicacao", data=date(2025, 2, 1), quantidade=D(1), preco_unitario=500000,
            custodia_conta_id=corretora.id,
        ),
    ])
    db.commit()

    rel = relatorio_bens_e_direitos(db, 2025)
    assert rel.ano == 2025
    assert rel.ano_anterior == 2024
    assert rel.ano_exercicio == 2026
    assert rel.total_itens == 3
    # Em 31/12/2024: PETR4 (R$ 3.000) + VALE3 (R$ 2.400) = R$ 5.400
    assert rel.custo_anterior_total == 540000
    # Em 31/12/2025: PETR4 (R$ 4.800) + VALE3 (R$ 0) + CDB (R$ 5.000) = R$ 9.800
    assert rel.custo_atual_total == 980000

    itens_por_cod = {i.ativo.codigo: i for g in rel.grupos for i in g.itens}
    item_petr = itens_por_cod["PETR4"]
    assert item_petr.custo_anterior_brl == 300000
    assert item_petr.custo_atual_brl == 480000
    assert item_petr.quantidade_atual == D(150)
    assert item_petr.preco_medio_atual_brl == 3200
    assert "150 ações de PETR4 (Petrobras PN)" in item_petr.discriminacao
    assert "Itaú Corretora" in item_petr.discriminacao
    assert not item_petr.encerrada_no_ano

    item_vale = itens_por_cod["VALE3"]
    assert item_vale.custo_anterior_brl == 240000
    assert item_vale.custo_atual_brl == 0
    assert item_vale.encerrada_no_ano
    assert "encerrada integralmente ao longo de 2025" in item_vale.discriminacao


def test_rota_irpf_renderiza_pagina_e_filtros(client, db):
    corretora = nova_conta(db, nome="XP Investimentos", tipo="investimento")
    fii = Ativo(classe="fii", alocacao="acoes", codigo="HGLG11", nome="CSHG Logística")
    db.add(fii)
    db.flush()
    db.add(OperacaoInvest(
        ativo_id=fii.id, tipo="compra", data=date(2025, 4, 10), quantidade=D(10), preco_unitario=16000,
        custodia_conta_id=corretora.id,
    ))
    db.commit()

    resp = client.get("/investimentos/irpf")
    assert resp.status_code == 200
    texto = resp.text
    assert "IRPF · Bens e Direitos" in texto
    assert "07 - Fundos" in texto
    assert "03 - Fundos de Investimento Imobiliário (FII)" in texto
    assert "HGLG11" in texto
    assert "XP Investimentos" in texto
    assert "1.600,00" in texto

    # Link na página principal de investimentos
    index_txt = client.get("/investimentos").text
    assert 'href="/investimentos/irpf' in index_txt

    # Gráfico Consolidação de aportes na página de lançamentos
    lanc_txt = client.get("/investimentos/lancamentos").text
    assert "Consolidação de aportes" in lanc_txt
    assert "grafico-consolidacao-aportes" in lanc_txt
    assert '"mes": "2025-04"' in lanc_txt or '"2025-04"' in lanc_txt

