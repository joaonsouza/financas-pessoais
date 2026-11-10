"""Títulos do Tesouro Direto: leitura do arquivo público, cache local, taxa do dia e taxa por aplicação."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.models import Ativo, OperacaoInvest, TaxaTesouro
from app.services.invest_renda_fixa import valor_atualizado
from app.services.mercado.tesouro import (
    atualizar_tesouro, ler_csv, taxa_na_data, titulo_do_codigo, titulos_a_venda,
)
from tests.factories import nova_conta
from tests.test_rotas_investimentos import ROTAS_OK, mercado, op_form  # noqa: F401
from tests.test_mercado_provedores import sem_rede_real  # noqa: F401

CSV = ROTAS_OK["PrecoTaxaTesouroDireto"]


class TesouroFalso:
    def __init__(self, texto=CSV):
        self.chamadas = 0
        self.texto = texto

    def historico(self):
        self.chamadas += 1
        return ler_csv(self.texto)


def test_ler_csv_converte_virgula_e_preco_em_centavos():
    p = ler_csv(CSV)[0]
    assert (p.titulo, p.vencimento, p.data, p.taxa_compra, p.pu_compra) == (
        "Tesouro IPCA+", date(2050, 8, 15), date(2026, 9, 21), D("7.17"), 91428)
    assert ler_csv("Tipo Titulo;Data Vencimento\nlixo;xx\n") == []


def test_atualiza_uma_vez_por_dia_e_so_acrescenta_dias_novos(session):
    falso = TesouroFalso()
    assert atualizar_tesouro(session, date(2026, 10, 2), tesouro=falso)
    atualizar_tesouro(session, date(2026, 10, 2), tesouro=falso)
    assert falso.chamadas == 1 and session.query(TaxaTesouro).count() == 4
    atualizar_tesouro(session, date(2026, 10, 3), tesouro=falso)  # outro dia: baixa de novo, sem duplicar
    assert falso.chamadas == 2 and session.query(TaxaTesouro).count() == 4


def test_titulos_a_venda_sem_cupom_e_taxa_do_dia(session):
    atualizar_tesouro(session, date(2026, 10, 2), tesouro=TesouroFalso())
    assert [(t.nome, t.indexador) for t in titulos_a_venda(session)] == [
        ("Tesouro IPCA+ 2050", "ipca"), ("Tesouro Selic 2029", "cdi")]
    t = taxa_na_data(session, "Tesouro IPCA+", date(2050, 8, 15), date(2026, 9, 27))  # fim de semana: dia útil antes
    assert (t.data, t.taxa_compra) == (date(2026, 9, 21), D("7.17"))
    assert taxa_na_data(session, "Tesouro IPCA+", date(2050, 8, 15), date(2026, 9, 1)) is None
    assert titulo_do_codigo("Tesouro Ipca+ 2050") == "Tesouro IPCA+" and titulo_do_codigo("CDB Banco X") is None


def test_cada_aplicacao_rende_pela_sua_taxa():
    from types import SimpleNamespace as O
    ativo = O(indexador="pre", taxa=D("10"), vencimento=None)
    cdi = {date(2026, 1, d): D("0.05") for d in range(2, 31)}
    ops = [O(tipo="aplicacao", data=date(2026, 1, 1), quantidade=D(1), preco_unitario=100000, taxa=D("20")),
           O(tipo="aplicacao", data=date(2026, 1, 1), quantidade=D(1), preco_unitario=100000, taxa=None)]
    so_20 = valor_atualizado(ativo, ops[:1], cdi, {}, date(2026, 1, 30))
    so_10 = valor_atualizado(ativo, ops[1:], cdi, {}, date(2026, 1, 30))
    assert so_20 > so_10 > 100000 and valor_atualizado(ativo, ops, cdi, {}, date(2026, 1, 30)) == so_20 + so_10


def test_rotas_de_titulos_e_taxa(client, db, mercado):  # noqa: F811
    nova_conta(db)
    db.commit()
    titulos = client.get("/investimentos/tesouro/titulos").json()
    assert {"titulo": "Tesouro IPCA+", "nome": "Tesouro IPCA+ 2050", "vencimento": "2050-08-15",
            "indexador": "ipca"} in titulos
    r = client.get("/investimentos/tesouro/taxa", params={"titulo": "Tesouro IPCA+", "vencimento": "2050-08-15",
                                                          "data": "2026-10-01"})
    assert r.json() == {"taxa": "7,06", "pu": 94148, "pu_venda": 91633, "data": "2026-10-01"}
    ordem = client.get("/investimentos/tesouro/taxa", params={"titulo": "Tesouro IPCA+", "vencimento": "2050-08-15",
                                                              "data": "2026-10-02", "anterior": "1"}).json()
    assert ordem["data"] == "2026-10-01"  # liquidação em 02/10: a ordem foi no dia útil anterior
    selic = client.get("/investimentos/tesouro/taxa", params={"titulo": "Tesouro Selic", "vencimento": "2029-03-01",
                                                              "data": "2026-10-01"}).json()
    assert selic["taxa"] == "100"  # vira % do CDI
    assert client.get("/investimentos/tesouro/taxa", params={"titulo": "Tesouro IPCA+", "vencimento": "2050-08-15",
                                                             "data": "2020-01-01"}).status_code == 404


def test_aplicacao_guarda_a_taxa_propria(client, db, mercado):  # noqa: F811
    nova_conta(db)
    db.commit()
    base = dict(classe="renda_fixa", codigo="Tesouro IPCA+ 2050", tipo="aplicacao", indexador="ipca",
                vencimento="2050-08-15", preco="9,14", quantidade="")
    assert client.post("/investimentos/operacoes", data=op_form(**base, taxa="7,17", data="2026-09-21")).status_code == 200
    a = db.scalars(select(Ativo)).one()
    client.post("/investimentos/operacoes", data=op_form(ativo_id=str(a.id), tipo="aplicacao", preco="28,24",
                                                        quantidade="", taxa_op="7,06", data="2026-10-01"))
    taxas = [o.taxa for o in db.scalars(select(OperacaoInvest).order_by(OperacaoInvest.data))]
    assert taxas == [D("7.17"), D("7.06")] and a.taxa == D("7.17")



def test_tesouro_por_titulos_quantidade_preco_medio_e_preco_de_mercado(client, db, mercado):  # noqa: F811
    from app.services.invest_carteira import resumo_carteira
    from app.services.mercado.tesouro import atualizar_tesouro
    nova_conta(db)
    db.commit()
    atualizar_tesouro(db, date(2026, 10, 2), tesouro=TesouroFalso())
    base = dict(classe="renda_fixa", codigo="Tesouro IPCA+ 2050", tipo="aplicacao", indexador="ipca",
                vencimento="2050-08-15", por_titulos="1")
    r = client.post("/investimentos/operacoes", data=op_form(**base, taxa="7,17", data="2026-09-22",
                                                             quantidade="0,01", preco="914,00"))
    assert r.status_code == 200, r.text
    a = db.scalars(select(Ativo)).one()
    assert a.por_titulos
    client.post("/investimentos/operacoes", data=op_form(ativo_id=str(a.id), tipo="aplicacao", taxa_op="7,06",
                                                        data="2026-10-02", quantidade="0,03", preco="941,50"))
    (linha,) = resumo_carteira(db, date(2026, 10, 5)).ativos
    assert linha.quantidade == D("0.04") and linha.custo == 914 + 2825      # 0,03 x 941,50 = 28,245
    assert linha.cotacao_brl == D(91633) and linha.saldo == 3665           # 0,04 x 916,33 (venda em 01/10)
    assert round(linha.preco_medio_brl) == 93475 and linha.variacao_pct < 0
    venda = client.post("/investimentos/operacoes", data=op_form(ativo_id=str(a.id), tipo="resgate", data="2026-10-05",
                                                                quantidade="0,05", preco="916,33"))
    assert venda.status_code == 422 and "acima da posição" in venda.text
    texto = client.get("/investimentos").text
    assert "0,04" in texto.split("Tesouro IPCA+ 2050")[1][:2500]
