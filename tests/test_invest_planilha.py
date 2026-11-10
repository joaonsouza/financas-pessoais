from datetime import date
from decimal import Decimal as D

import openpyxl
import pytest
from sqlalchemy import select

from app.models import Ativo, OperacaoInvest
from app.services import invest_planilha
from app.services.invest_planilha import ItemImportacao, PlanilhaIndisponivel, importar_posicoes, ler_planilha
from tests.factories import nova_conta


def _bloco(ws, linha, titulo, linhas, *, com_ticker=True):
    """Escreve um bloco no layout da aba: título em B / '% Ideal' em I; sub-cabeçalho; ativos; linha em branco."""
    ws.cell(linha, 2, titulo)
    ws.cell(linha, 4, "Cotação")
    ws.cell(linha, 6, "Alocação Atual")
    ws.cell(linha, 9, "% Ideal")
    ws.cell(linha + 1, 3 if com_ticker else 2, "Ticker" if com_ticker else "Coluna1")
    ws.cell(linha + 1, 6, "Qtd")
    ws.cell(linha + 1, 7, 12345.67)            # total do bloco (cabeçalho numérico, não é ativo)
    ws.cell(linha + 1, 9, 1)
    for k, (nome, qtd, ideal) in enumerate(linhas):
        r = linha + 2 + k
        ws.cell(r, 2, "#VALUE!" if com_ticker else nome)
        if com_ticker:
            ws.cell(r, 3, nome)
        ws.cell(r, 4, 99.9)                     # preço da planilha: ignorado
        ws.cell(r, 6, qtd)
        ws.cell(r, 9, ideal)


@pytest.fixture
def planilha(tmp_path, monkeypatch):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "INVESTIMENTOS"
    _bloco(ws, 4, "Carteira Ações", [("AAAA3", 10, 0.6), ("bbbb4", 20, 0.4)])
    _bloco(ws, 18, "Carteira Fundos Imobiliários", [("CCCC11", 7, 1.0)])
    _bloco(ws, 32, "Internacional", [("USD", 150.5, 0.0), ("DDD", 2.5, 1.0)])
    _bloco(ws, 42, "Cripto", [("EEE", 0.12345678, 0.7), ("FFF", 0, 0.3)])
    _bloco(ws, 54, "Renda Fixa", [("Tesouro Selic 2031", 1500.5, 0.5), ("Tesouro IPCA+ 2029", 800, 0.5)], com_ticker=False)
    wb.create_sheet("JANEIRO")
    caminho = tmp_path / "controle.xlsx"
    wb.save(caminho)
    monkeypatch.setattr(invest_planilha, "CAMINHO_PLANILHA", caminho)
    return caminho


def test_le_blocos_classes_quantidades_e_metas(planilha):
    pos = ler_planilha()
    por_classe = {}
    for p in pos:
        por_classe.setdefault(p.classe, []).append(p)
    assert [p.codigo for p in por_classe["acao"]] == ["AAAA3", "BBBB4"]
    assert [(p.quantidade, p.meta_pct) for p in por_classe["acao"]] == [(D("10"), D("60")), (D("20"), D("40"))]
    assert [p.codigo for p in por_classe["fii"]] == ["CCCC11"]
    assert [p.codigo for p in por_classe["exterior"]] == ["DDD"]          # "USD" (caixa) é ignorado
    assert por_classe["exterior"][0].quantidade == D("2.5")
    assert [(p.codigo, p.quantidade) for p in por_classe["cripto"]] == [("EEE", D("0.12345678")), ("FFF", D("0"))]
    assert [(p.codigo, p.quantidade, p.meta_pct) for p in por_classe["renda_fixa"]] == [
        ("Tesouro Selic 2031", D("1500.5"), D("50")), ("Tesouro IPCA+ 2029", D("800"), D("50"))]
    assert len(pos) == 8


def test_arquivo_ausente_aba_ausente_e_vazia(tmp_path, monkeypatch):
    with pytest.raises(PlanilhaIndisponivel, match="não encontrada"):
        ler_planilha(tmp_path / "nao-existe.xlsx")
    wb = openpyxl.Workbook()
    wb.active.title = "OUTRA"
    wb.save(tmp_path / "sem_aba.xlsx")
    with pytest.raises(PlanilhaIndisponivel, match="aba INVESTIMENTOS"):
        ler_planilha(tmp_path / "sem_aba.xlsx")
    wb = openpyxl.Workbook()
    wb.active.title = "INVESTIMENTOS"
    wb.save(tmp_path / "vazia.xlsx")
    with pytest.raises(PlanilhaIndisponivel, match="Nenhuma posição"):
        ler_planilha(tmp_path / "vazia.xlsx")
    (tmp_path / "ruim.xlsx").write_text("não é um xlsx")
    with pytest.raises(PlanilhaIndisponivel, match="Não foi possível abrir"):
        ler_planilha(tmp_path / "ruim.xlsx")


def test_importar_cria_ativo_e_ajuste_de_posicao(session):
    itens = [
        ItemImportacao("acao", "AAAA3", D("10"), D("60"), 3000),
        ItemImportacao("exterior", "DDD", D("2.5"), D("100"), 15000, cambio=D("5.1")),
        ItemImportacao("cripto", "FFF", D("0"), D("30"), 0),
        ItemImportacao("renda_fixa", "Tesouro Selic 2031", D("1500.5"), D("50"), 150050, indexador="cdi",
                       taxa=D("100"), vencimento=date(2031, 3, 1)),
    ]
    ativos = importar_posicoes(session, itens, date(2026, 10, 5))
    session.commit()
    assert [a.codigo for a in ativos] == ["AAAA3", "DDD", "FFF", "Tesouro Selic 2031"]
    acao = session.scalars(select(Ativo).where(Ativo.codigo == "AAAA3")).one()
    assert (acao.meta_pct, acao.simbolo_cotacao) == (D("60"), "AAAA3.SA")
    ops = {session.get(Ativo, o.ativo_id).codigo: o for o in session.scalars(select(OperacaoInvest))}
    assert set(ops) == {"AAAA3", "DDD", "Tesouro Selic 2031"}        # quantidade 0: só meta, sem operação
    assert (ops["AAAA3"].tipo, ops["AAAA3"].quantidade, ops["AAAA3"].preco_unitario) == ("ajuste_posicao", D("10"), 3000)
    assert ops["DDD"].cambio == D("5.1") and session.scalars(select(Ativo).where(Ativo.codigo == "DDD")).one().moeda == "USD"
    assert (ops["Tesouro Selic 2031"].quantidade, ops["Tesouro Selic 2031"].preco_unitario) == (D(1), 150050)


def test_importar_exige_preco_medio_e_nao_deixa_resto(session):
    with pytest.raises(ValueError, match="AAAA3: informe o preço médio"):
        importar_posicoes(session, [ItemImportacao("acao", "AAAA3", D("10"), D("60"), 0)], date(2026, 10, 5))
    session.rollback()
    with pytest.raises(ValueError, match="câmbio"):
        importar_posicoes(session, [ItemImportacao("exterior", "DDD", D("1"), D("100"), 100)], date(2026, 10, 5))


# ---------- tela ----------

def test_tela_lista_posicoes_sugeridas(client, db, planilha):
    nova_conta(db)
    db.commit()
    texto = client.get("/investimentos/importar-planilha").text
    for esperado in ("AAAA3", "BBBB4", "CCCC11", "DDD", "EEE", "Tesouro Selic 2031"):
        assert esperado in texto
    assert 'value="10"' in texto and 'value="60"' in texto and 'value="0,12345678"' in texto
    assert ">USD<" not in texto


def test_tela_sem_planilha_avisa(client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(invest_planilha, "CAMINHO_PLANILHA", tmp_path / "x.xlsx")
    nova_conta(db)
    db.commit()
    r = client.get("/investimentos/importar-planilha")
    assert r.status_code == 200 and "Planilha não encontrada" in r.text


def _marcar(*indices, **extra):
    dados = {"data": "2026-10-05", "cambio": "5,10"}
    for i in indices:
        dados[f"incluir_{i}"] = "on"
    dados.update(extra)
    return dados


def test_confirmar_cria_ativos_e_ajustes(client, db, planilha):
    nova_conta(db)
    db.commit()
    # ordem lida: 0 AAAA3, 1 BBBB4, 2 CCCC11, 3 DDD, 4 EEE, 5 FFF, 6 Selic, 7 IPCA
    dados = _marcar(0, 3, 6, preco_0="30,00", quantidade_0="10", meta_0="60", preco_3="150,00", quantidade_3="2,5",
                    meta_3="100", preco_6="1.500,50", quantidade_6="1500,5", meta_6="50", indexador_6="cdi",
                    taxa_6="100", vencimento_6="2031-03-01")
    r = client.post("/investimentos/importar-planilha", data=dados, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/investimentos"
    ativos = {a.codigo: a for a in db.scalars(select(Ativo))}
    assert set(ativos) == {"AAAA3", "DDD", "Tesouro Selic 2031"}
    assert ativos["AAAA3"].meta_pct == D("60") and ativos["Tesouro Selic 2031"].indexador == "cdi"
    ops = {db.get(Ativo, o.ativo_id).codigo: o for o in db.scalars(select(OperacaoInvest))}
    assert ops["AAAA3"].preco_unitario == 3000 and ops["AAAA3"].data == date(2026, 10, 5)
    assert ops["DDD"].cambio == D("5.1") and ops["Tesouro Selic 2031"].preco_unitario == 150050
    # a página principal já mostra a carteira importada
    assert "AAAA3" in client.get("/investimentos").text


def test_confirmar_com_erro_nao_grava_nada_e_mantem_valores(client, db, planilha):
    nova_conta(db)
    db.commit()
    r = client.post("/investimentos/importar-planilha", data=_marcar(0, 1, preco_0="30,00", preco_1="",
                                                                     quantidade_0="10", quantidade_1="20"))
    assert r.status_code == 422 and "BBBB4: informe o preço médio" in r.text
    assert 'value="30,00"' in r.text
    assert db.scalars(select(Ativo)).all() == []


def test_confirmar_sem_marcar_nada(client, db, planilha):
    nova_conta(db)
    db.commit()
    r = client.post("/investimentos/importar-planilha", data={"data": "2026-10-05"})
    assert r.status_code == 422 and "Marque pelo menos uma posição" in r.text


def test_ativo_ja_cadastrado_vem_desmarcado(client, db, planilha):
    nova_conta(db)
    db.add(Ativo(classe="acao", codigo="AAAA3", simbolo_cotacao="AAAA3.SA"))
    db.commit()
    texto = client.get("/investimentos/importar-planilha").text
    assert "já cadastrado" in texto
    # re-importar um existente é recusado em vez de duplicar
    r = client.post("/investimentos/importar-planilha", data=_marcar(0, preco_0="30,00", quantidade_0="10", meta_0="60"))
    assert r.status_code == 422 and "já está cadastrado" in r.text
