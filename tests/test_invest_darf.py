"""DARF mensal de renda variável e cripto: precisa pagar? Quanto, com qual código e até quando."""
from datetime import date
from decimal import Decimal as D

from app.models import Ativo, OperacaoInvest
from app.services.invest_darf import apuracao, categoria, vencimento
from tests.factories import nova_conta

HOJE = date(2026, 10, 5)


def _ativo(s, classe, codigo, alocacao=None):
    a = Ativo(classe=classe, codigo=codigo, simbolo_cotacao=f"{codigo}.SA", alocacao=alocacao or "acoes")
    s.add(a)
    s.flush()
    return a


def _op(s, a, tipo, dia, qtd, preco):
    s.add(OperacaoInvest(ativo_id=a.id, tipo=tipo, data=dia, quantidade=D(qtd), preco_unitario=preco))
    s.flush()


def _linhas(s):
    return {(l.competencia, l.codigo): l for l in apuracao(s, HOJE)}


def test_categoria_do_ativo():
    assert categoria(Ativo(classe="acao", codigo="PETR4")) == "acao"
    assert categoria(Ativo(classe="acao", codigo="AAPL34")) == "comum"          # BDR: sem isenção
    assert categoria(Ativo(classe="etf", codigo="IVVB11", alocacao="acoes")) == "comum"
    assert categoria(Ativo(classe="etf", codigo="LFTB11", alocacao="renda_fixa")) is None   # IR na fonte
    assert categoria(Ativo(classe="etf", codigo="XPTO11", alocacao="renda_fixa")) is None
    assert categoria(Ativo(classe="fii", codigo="HGLG11")) == "fii"
    assert categoria(Ativo(classe="cripto", codigo="BTC")) == "cripto"
    assert categoria(Ativo(classe="exterior", codigo="VOO")) is None
    assert categoria(Ativo(classe="renda_fixa", codigo="CDB")) is None


def test_vencimento_e_o_ultimo_dia_util_do_mes_seguinte():
    assert vencimento("2026-03") == date(2026, 4, 30)       # quinta
    assert vencimento("2026-04") == date(2026, 5, 29)       # 31/05 é domingo
    assert vencimento("2026-12") == date(2027, 1, 29)       # 31/01/2027 é domingo


def test_etf_com_lucro_paga_15_por_cento_no_6015(session):
    etf = _ativo(session, "etf", "IVVB11")
    _op(session, etf, "compra", date(2026, 3, 2), 10, 10000)
    _op(session, etf, "venda", date(2026, 3, 10), 10, 12000)      # lucro 200
    l = _linhas(session)[("2026-03", "6015")]
    assert (l.vendido, l.resultado, l.imposto, l.valor, l.situacao) == (120000, 20000, 3000, 3000, "pagar")
    assert l.vencimento == date(2026, 4, 30) and l.vencido


def test_acoes_ate_20_mil_sao_isentas_e_o_prejuizo_delas_abate_depois(session):
    petr = _ativo(session, "acao", "PETR4")
    etf = _ativo(session, "etf", "BOVA11")
    _op(session, petr, "compra", date(2026, 1, 5), 100, 5000)
    _op(session, petr, "venda", date(2026, 2, 3), 50, 6000)         # vende 3.000, lucro 500: isento
    _op(session, petr, "venda", date(2026, 3, 3), 50, 3000)         # vende 1.500, prejuízo 1.000 (mês isento)
    _op(session, etf, "compra", date(2026, 1, 5), 10, 10000)
    _op(session, etf, "venda", date(2026, 4, 6), 10, 22000)         # lucro 1.200 - prejuízo 1.000 = 200
    l = _linhas(session)
    assert l[("2026-02", "6015")].situacao == "isento"
    assert l[("2026-03", "6015")].situacao == "prejuizo"
    assert (l[("2026-04", "6015")].imposto, l[("2026-04", "6015")].situacao) == (3000, "pagar")


def test_fii_paga_20_por_cento_e_tem_prejuizo_separado(session):
    fii = _ativo(session, "fii", "HGLG11")
    etf = _ativo(session, "etf", "BOVA11")
    _op(session, fii, "compra", date(2026, 1, 5), 10, 15000)
    _op(session, etf, "compra", date(2026, 1, 5), 10, 10000)
    _op(session, fii, "venda", date(2026, 2, 3), 5, 13000)          # prejuízo 100 de FII
    _op(session, etf, "venda", date(2026, 3, 3), 10, 11000)         # lucro 100 de ETF: o de FII não abate
    _op(session, fii, "venda", date(2026, 4, 6), 5, 17000)          # lucro 100 de FII - 100 = 0
    l = _linhas(session)
    assert l[("2026-03", "6015")].imposto == 1500
    assert l[("2026-04", "6015")].situacao == "prejuizo" and l[("2026-04", "6015")].imposto == 0


def test_cripto_isenta_ate_35_mil_e_acima_paga_15_por_cento_no_4600(session):
    btc = _ativo(session, "cripto", "BTC", "cripto")
    eth = _ativo(session, "cripto", "ETH", "cripto")
    _op(session, btc, "compra", date(2026, 1, 5), 1, 3000000)
    _op(session, eth, "compra", date(2026, 1, 5), 1, 1500000)
    _op(session, btc, "venda", date(2026, 2, 3), D("0.5"), 4500000)   # vende 22.500, lucro 7.500: isento
    _op(session, btc, "venda", date(2026, 3, 3), D("0.5"), 6000000)   # vende 30.000, lucro 15.000
    _op(session, eth, "venda", date(2026, 3, 4), 1, 1000000)          # vende 10.000, prejuízo 5.000 (não abate)
    l = _linhas(session)
    assert l[("2026-02", "4600")].situacao == "isento"
    m = l[("2026-03", "4600")]                                         # 40.000 vendidos > 35.000
    assert (m.vendido, m.imposto, m.situacao) == (4000000, 225000, "pagar")


def test_imposto_abaixo_de_10_reais_acumula_para_o_mes_seguinte(session):
    etf = _ativo(session, "etf", "BOVA11")
    _op(session, etf, "compra", date(2026, 1, 5), 10, 10000)
    _op(session, etf, "venda", date(2026, 2, 3), 1, 14000)          # lucro 40: imposto 6
    _op(session, etf, "venda", date(2026, 3, 3), 1, 14000)          # mais 6: 12 no total
    l = _linhas(session)
    assert (l[("2026-02", "6015")].situacao, l[("2026-02", "6015")].valor) == ("acumula", 0)
    assert (l[("2026-03", "6015")].situacao, l[("2026-03", "6015")].valor) == ("pagar", 1200)


def test_etf_de_renda_fixa_e_exterior_nao_entram(session):
    lftb = _ativo(session, "etf", "LFTB11", "renda_fixa")
    _op(session, lftb, "compra", date(2026, 1, 5), 10, 10000)
    _op(session, lftb, "venda", date(2026, 2, 3), 10, 12000)
    assert apuracao(session, HOJE) == []


def test_tela_mostra_o_pendente_e_marca_como_pago(client, db):
    nova_conta(db)
    etf = _ativo(db, "etf", "IVVB11")
    _op(db, etf, "compra", date(2026, 3, 2), 10, 10000)
    _op(db, etf, "venda", date(2026, 3, 10), 10, 12000)
    db.commit()
    html = client.get("/investimentos/darf").text
    assert "R$ 30,00" in html and "a pagar" in html and "6015" in html and "30/04/2026" in html
    assert "/investimentos/darf" in client.get("/investimentos").text            # link na tela da carteira
    r = client.post("/investimentos/darf/2026-03/6015/pago", follow_redirects=False)
    assert r.status_code == 303
    html = client.get("/investimentos/darf").text
    assert "Nada a pagar" in html and "Pago" in html
    client.post("/investimentos/darf/2026-03/6015/pago", follow_redirects=False)   # desfazer
    assert "Nada a pagar" not in client.get("/investimentos/darf").text
