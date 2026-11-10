from datetime import date

import openpyxl
from openpyxl.worksheet.table import Table
from sqlalchemy import func, select

from app.models import Assinatura, Categoria, Conta, Lancamento, Recorrencia, get_config
from app.services.categorias_padrao import semear_categorias
from app.services.config_inicial import AssinaturaSugerida, ContaSugerida, ler_config_inicial
from app.web.routes import setup as rota_setup
from tests.factories import nova_conta


def _planilha(caminho):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CONFIG_INICIAL"
    contas = [
        ["Ord", "Desc", "SL", "Tipo", "Inicial"],
        [1, "Itaú", True, "Corrente", 1500.25],
        [2, "NuInvest", False, "Investimento", 20000.5],
        [None, "Total", None, None, 21500.75],
    ]
    assinaturas = [
        ["Desc", "Ativa", "Valor"],
        ["Spotify", True, 40.9],
        ["Netflix", False, 59.9],
        ["Total", None, 40.9],
    ]
    for i, linha in enumerate(contas):
        for j, valor in enumerate(linha):
            ws.cell(4 + i, 2 + j, valor)
    for i, linha in enumerate(assinaturas):
        for j, valor in enumerate(linha):
            ws.cell(4 + i, 8 + j, valor)
    ws.add_table(Table(displayName="CONFIG", ref="B4:F7"))
    ws.add_table(Table(displayName="Assinaturas341336468", ref="H4:J7"))
    wb.save(caminho)
    return caminho


def test_ler_config_inicial(tmp_path):
    contas, assinaturas = ler_config_inicial(_planilha(tmp_path / "p.xlsx"))
    assert contas == [
        ContaSugerida("Itaú", "corrente", True, 150025),
        ContaSugerida("NuInvest", "investimento", False, 2000050),
    ]
    assert assinaturas == [AssinaturaSugerida("Spotify", True, 4090), AssinaturaSugerida("Netflix", False, 5990)]


def test_semear_categorias(session):
    assert semear_categorias(session) == 28
    assert semear_categorias(session) == 0
    por_nome = {c.nome: c for c in session.scalars(select(Categoria))}
    assert (por_nome["Aluguel"].grupo, por_nome["Aluguel"].essencial, por_nome["Aluguel"].natureza) == (
        "fixo", True, "necessidade",
    )
    assert (por_nome["Mercado"].essencial, por_nome["Mercado"].natureza) == (True, "necessidade")
    assert (por_nome["Lazer"].essencial, por_nome["Lazer"].natureza) == (False, "desejo")
    assert por_nome["Parcelas de empréstimo/financiamento"].grupo == "divida"
    assert por_nome["Salário"].tipo == "receita"


def _dados_passo1(**extra):
    dados = {
        "data_inicio": "2026-10-01", "n_contas": "2",
        "conta_incluir_0": "on", "conta_nome_0": "Itaú", "conta_tipo_0": "corrente",
        "conta_sl_0": "on", "conta_saldo_0": "1.500,25",
        "conta_incluir_1": "on", "conta_nome_1": "NuInvest", "conta_tipo_1": "investimento",
        "conta_saldo_1": "20.000,50",
        "cartao_nome_0": "Cartão Itaú", "cartao_limite_0": "10.000,00", "cartao_fechamento_0": "3",
        "cartao_vencimento_0": "10", "cartao_pagadora_0": "Itaú",
    }
    dados.update(extra)
    return dados


def test_get_setup_mostra_sugestoes_da_planilha(client, monkeypatch, tmp_path):
    monkeypatch.setattr(rota_setup, "PLANILHA", _planilha(tmp_path / "p.xlsx"))
    resposta = client.get("/setup")
    assert resposta.status_code == 200
    assert 'value="Itaú"' in resposta.text
    assert 'value="1500,25"' in resposta.text


def test_get_setup_sem_planilha(client, monkeypatch, tmp_path):
    monkeypatch.setattr(rota_setup, "PLANILHA", tmp_path / "nao-existe.xlsx")
    assert client.get("/setup").status_code == 200


def test_post_setup_cria_contas_cartao_e_categorias(client, db):
    resposta = client.post("/setup", data=_dados_passo1(), follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/setup/assinaturas"

    contas = {c.nome: c for c in db.scalars(select(Conta))}
    assert contas["Itaú"].saldo_inicial == 150025
    assert contas["Itaú"].entra_saldo_liquido is True
    assert contas["NuInvest"].entra_saldo_liquido is False
    cartao = contas["Cartão Itaú"]
    assert (cartao.tipo, cartao.limite, cartao.dia_fechamento, cartao.dia_vencimento) == ("cartao", 1000000, 3, 10)
    assert cartao.conta_pagamento_id == contas["Itaú"].id
    assert cartao.data_saldo_inicial == date(2026, 10, 1)
    assert db.scalar(select(func.count(Categoria.id))) == 28
    assert get_config(db, "data_inicio") == "2026-10-01"


def test_post_setup_saldo_invalido_mantem_dados(client, db):
    resposta = client.post("/setup", data=_dados_passo1(conta_saldo_1="abc"))
    assert resposta.status_code == 400
    assert "valor inválido" in resposta.text
    assert 'value="NuInvest"' in resposta.text
    assert db.scalar(select(func.count(Conta.id))) == 0


def test_post_setup_pagadora_inexistente(client):
    resposta = client.post("/setup", data=_dados_passo1(cartao_pagadora_0="Banco X"))
    assert resposta.status_code == 400
    assert "conta pagadora" in resposta.text


def test_setup_assinaturas(client, db, monkeypatch, tmp_path):
    monkeypatch.setattr(rota_setup, "PLANILHA", _planilha(tmp_path / "p.xlsx"))
    client.post("/setup", data=_dados_passo1())
    assert "Spotify" in client.get("/setup/assinaturas").text

    cartao_id = db.scalar(select(Conta.id).where(Conta.nome == "Cartão Itaú"))
    resposta = client.post("/setup/assinaturas", data={
        "n_assinaturas": "1", "assinatura_incluir_0": "on", "assinatura_nome_0": "Spotify",
        "assinatura_valor_0": "40,90", "assinatura_dia_0": "15", "assinatura_conta_0": str(cartao_id),
    }, follow_redirects=False)

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/"
    ass = db.scalars(select(Assinatura)).one()
    assert (ass.nome, ass.valor, ass.dia, ass.ativa, ass.conta_id) == ("Spotify", 4090, 15, True, cartao_id)
    assert db.scalars(select(Recorrencia)).all() == []  # assinatura é só controle: não gera recorrência
    assert db.scalar(select(func.count(Lancamento.id)).where(Lancamento.recorrencia_id.is_not(None))) == 0


def test_setup_com_contas_redireciona(client, db):
    nova_conta(db)
    db.commit()
    resposta = client.get("/setup", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/"
