from datetime import date, datetime, timedelta
from decimal import Decimal as D

from app.models import Ativo, CotacaoCache, OperacaoInvest, ProventoCache, SerieIndicador
from app.services.invest_historico import evolucao_mensal

ATE = date(2026, 10, 5)


def ativo(s, classe="acao", codigo="PETR4", simbolo="PETR4.SA", moeda="BRL", **kw):
    a = Ativo(classe=classe, codigo=codigo, simbolo_cotacao=simbolo, moeda=moeda, **kw)
    s.add(a)
    s.flush()
    return a


def op(s, a, tipo, dia, qtd, preco, cambio=None, taxas=0):
    s.add(OperacaoInvest(ativo_id=a.id, tipo=tipo, data=dia, quantidade=D(qtd), preco_unitario=preco,
                         cambio=None if cambio is None else D(cambio), taxas=taxas))
    s.flush()


def provento(s, simbolo, data_ex, valor):
    s.add(ProventoCache(simbolo=simbolo, data_ex=data_ex, valor=D(valor), atualizado_em=datetime(2026, 10, 5, 10)))
    s.flush()


def fechamento(s, simbolo, dia, valor, mensal=True, moeda="BRL"):
    s.add(CotacaoCache(simbolo=simbolo, data=dia, fechamento=D(valor), moeda=moeda,
                       fonte="yahoo:mensal" if mensal else "yahoo", atualizado_em=datetime(2026, 10, 5, 10)))
    s.flush()


def cenario_petr(s):
    a = ativo(s)
    op(s, a, "compra", date(2026, 7, 1), "10", 3000)     # fluxo 30.000 no 1º dia de julho
    op(s, a, "compra", date(2026, 8, 16), "5", 3300)     # fluxo 16.500 no dia 16 de agosto
    for dia, v in ((date(2026, 7, 31), "32"), (date(2026, 8, 31), "35"), (date(2026, 9, 30), "33")):
        fechamento(s, "PETR4.SA", dia, v)
    fechamento(s, "PETR4.SA", ATE, "34", mensal=False)
    s.commit()
    return a


def test_sem_operacoes_devolve_vazio(session):
    assert evolucao_mensal(session, ATE) == []


def test_patrimonio_e_aplicado_por_mes(session):
    cenario_petr(session)
    pts = evolucao_mensal(session, ATE)
    assert [p.competencia for p in pts] == ["2026-07", "2026-08", "2026-09", "2026-10"]
    assert [p.patrimonio for p in pts] == [32000, 52500, 49500, 51000]   # 10x32; 15x35; 15x33; 15x34 (preço atual)
    assert [p.aplicado_acumulado for p in pts] == [30000, 46500, 46500, 46500]


def test_dietz_modificado_e_encadeamento(session):
    cenario_petr(session)
    jul, ago, set_, _ = evolucao_mensal(session, ATE)[:4]
    # julho: início em 30/06; aporte no dia 1 (30 dias restantes de 31)
    r_jul = D(32000 - 0 - 30000) / (D(30000) * 30 / 31)
    assert jul.rent_mes_pct == r_jul * 100
    # agosto: aporte no dia 16 (15 dias restantes de 31)
    r_ago = D(52500 - 32000 - 16500) / (D(32000) + D(16500) * 15 / 31)
    assert ago.rent_mes_pct == r_ago * 100
    assert ago.rent_acum_pct == ((1 + r_jul) * (1 + r_ago) - 1) * 100
    # setembro: sem fluxo
    r_set = D(49500 - 52500) / D(52500)
    assert set_.rent_mes_pct == r_set * 100
    assert set_.rent_acum_pct == ((1 + r_jul) * (1 + r_ago) * (1 + r_set) - 1) * 100


def test_venda_e_fluxo_negativo(session):
    a = ativo(session)
    op(session, a, "compra", date(2026, 7, 1), "10", 3000)
    op(session, a, "venda", date(2026, 8, 31), "4", 3400, taxas=100)   # sai 13.600 - 100 no último dia
    fechamento(session, "PETR4.SA", date(2026, 7, 31), "32")
    fechamento(session, "PETR4.SA", date(2026, 8, 31), "35")
    session.commit()
    jul, ago = evolucao_mensal(session, date(2026, 8, 31))
    assert ago.patrimonio == 21000 and ago.aplicado_acumulado == 30000 - 13500
    # venda no último dia: peso zero; r = (21000 - 32000 + 13500) / 32000
    assert ago.rent_mes_pct == D(21000 - 32000 + 13500) / 32000 * 100


def test_ajuste_de_posicao_conta_como_aporte(session):
    a = ativo(session)
    op(session, a, "ajuste_posicao", date(2026, 7, 1), "10", 3000)
    fechamento(session, "PETR4.SA", date(2026, 7, 31), "33")
    session.commit()
    (jul,) = evolucao_mensal(session, date(2026, 7, 31))
    assert jul.aplicado_acumulado == 30000 and jul.patrimonio == 33000
    assert jul.rent_mes_pct == D(3000) / (D(30000) * 30 / 31) * 100



def test_provento_entra_na_rentabilidade_sem_mudar_o_aplicado(session):
    cenario_petr(session)
    provento(session, "PETR4.SA", date(2026, 8, 16), "1.00")  # data ex no dia da 2ª compra: só as 10 antigas recebem
    session.commit()
    jul, ago, *_ = evolucao_mensal(session, ATE)
    assert [p.aplicado_acumulado for p in (jul, ago)] == [30000, 46500]
    # agosto: R$ 10 saem como provento no dia 16, com o mesmo peso (15/31) da compra
    esperado = (D(52500) - D(32000) - D(15500)) / (D(32000) + D(15500) * 15 / 31) * 100
    assert abs(ago.rent_mes_pct - esperado) < D("1e-20")


def test_provento_fora_da_posicao_e_ignorado(session):
    cenario_petr(session)
    antes = evolucao_mensal(session, ATE)
    provento(session, "PETR4.SA", date(2026, 7, 1), "5.00")  # data ex no dia da 1ª compra: ainda sem cotas
    session.commit()
    assert evolucao_mensal(session, ATE) == antes


def test_provento_no_exterior_em_reais_e_liquido_do_imposto_retido(session):
    a = ativo(session, "exterior", "AAPL", "AAPL", "USD")
    op(session, a, "compra", date(2026, 7, 1), "2", 15000, cambio="5.00")
    fechamento(session, "AAPL", date(2026, 7, 31), "200", moeda="USD")
    fechamento(session, "USDBRL=X", date(2026, 7, 10), "5.00")
    fechamento(session, "USDBRL=X", date(2026, 7, 31), "5.50")
    provento(session, "AAPL", date(2026, 7, 15), "1.00")
    session.commit()
    (jul,) = evolucao_mensal(session, date(2026, 7, 31))
    # 2 × US$ 1,00 × 5,00 × 70% (30% retidos nos EUA) = R$ 7,00, pago no dia 15 (peso 16/31)
    esperado = (D(220000) - D(149300)) / (D(150000) * 30 / 31 - D(700) * 16 / 31) * 100
    assert abs(jul.rent_mes_pct - esperado) < D("1e-20")

def test_cdi_e_ibovespa_acumulados(session):
    cenario_petr(session)
    d = date(2026, 6, 30)
    while d < ATE:
        d += timedelta(days=1)
        if d.weekday() < 5:
            session.add(SerieIndicador(serie="cdi", data=d, valor_pct=D("0.05")))
    fechamento(session, "^BVSP", date(2026, 6, 30), "100000")
    fechamento(session, "^BVSP", date(2026, 7, 31), "110000")
    fechamento(session, "^BVSP", date(2026, 8, 31), "99000")
    fechamento(session, "^BVSP", date(2026, 9, 30), "104500")
    fechamento(session, "^BVSP", ATE, "105000", mensal=False)
    session.commit()
    pts = evolucao_mensal(session, ATE)
    assert [p.ibov_acum_pct for p in pts] == [D(10), D(-1), D("4.5"), D(5)]
    uteis_jul = sum(1 for dia in range(1, 32) if date(2026, 7, dia).weekday() < 5)
    assert pts[0].cdi_acum_pct == (D("1.0005") ** uteis_jul - 1) * 100
    assert pts[0].cdi_acum_pct < pts[1].cdi_acum_pct < pts[3].cdi_acum_pct



def test_indice_global_acumulado_em_reais(session):
    cenario_petr(session)
    for dia, vt, dolar in ((date(2026, 6, 30), "100", "5"), (date(2026, 7, 31), "110", "5"),
                           (date(2026, 8, 31), "110", "5.5")):
        fechamento(session, "VT", dia, vt)
        fechamento(session, "USDBRL=X", dia, dolar)
    fechamento(session, "VT", ATE, "100", mensal=False)
    session.commit()
    pts = evolucao_mensal(session, ATE)
    # VT × dólar: 500 → 550 (+10%) → 605 (+21%, só o câmbio) → setembro sem fechamento novo → 550
    assert [p.global_acum_pct for p in pts] == [D(10), D(21), D(21), D(10)]

def test_sem_series_cdi_e_ibov_ficam_nulos(session):
    cenario_petr(session)
    pts = evolucao_mensal(session, ATE)
    assert all(p.cdi_acum_pct is None and p.ibov_acum_pct is None and p.global_acum_pct is None for p in pts)


def test_exterior_usa_cambio_mensal(session):
    a = ativo(session, "exterior", "AAPL", "AAPL", "USD")
    op(session, a, "compra", date(2026, 7, 1), "2", 15000, cambio="5.00")
    fechamento(session, "AAPL", date(2026, 7, 31), "200", moeda="USD")
    fechamento(session, "USDBRL=X", date(2026, 7, 31), "5.50")
    session.commit()
    (jul,) = evolucao_mensal(session, date(2026, 7, 31))
    assert jul.patrimonio == 220000 and jul.aplicado_acumulado == 150000


def test_cripto_cotada_em_dolar_usa_cambio_mensal(session):
    a = ativo(session, "cripto", "BTC", "BTC-USD", "BRL")  # operações em reais, cotação do Yahoo em dólar
    op(session, a, "compra", date(2026, 7, 1), "0.01", 50000000)
    fechamento(session, "BTC-USD", date(2026, 7, 31), "100000", moeda="USD")
    fechamento(session, "USDBRL=X", date(2026, 7, 31), "5.50")
    session.commit()
    (jul,) = evolucao_mensal(session, date(2026, 7, 31))
    assert jul.patrimonio == 550000 and jul.aplicado_acumulado == 500000


def test_renda_fixa_entra_pelo_valor_atualizado(session):
    a = ativo(session, "renda_fixa", "CDB X", "", indexador="pre", taxa=D("10"), vencimento=date(2030, 1, 1))
    op(session, a, "aplicacao", date(2026, 7, 1), "1", 100000)
    for dia in (2, 3, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23, 24, 27, 28, 29, 30, 31):
        session.add(SerieIndicador(serie="cdi", data=date(2026, 7, dia), valor_pct=D("0.05")))
    session.commit()
    (jul,) = evolucao_mensal(session, date(2026, 7, 31))
    assert jul.patrimonio > 100000 and jul.aplicado_acumulado == 100000 and jul.rent_mes_pct > 0


def test_sem_preco_em_cache_a_posicao_vale_o_custo(session):
    a = ativo(session)
    op(session, a, "compra", date(2026, 7, 1), "10", 3000)
    session.commit()
    (jul,) = evolucao_mensal(session, date(2026, 7, 31))
    assert jul.patrimonio == 30000 and jul.rent_mes_pct == 0


def test_filtro_de_alocacao_considera_so_os_ativos_dela(session):
    cenario_petr(session)
    fii = ativo(session, classe="fii", codigo="HGLG11", simbolo="HGLG11.SA", alocacao="renda_fixa")
    op(session, fii, "compra", date(2026, 9, 10), "2", 15000)
    fechamento(session, "HGLG11.SA", date(2026, 9, 30), "160")
    fechamento(session, "HGLG11.SA", ATE, "170", mensal=False)
    session.commit()
    pts = evolucao_mensal(session, ATE, alocacao="renda_fixa")
    assert [p.competencia for p in pts] == ["2026-09", "2026-10"]
    assert [(p.patrimonio, p.aplicado_acumulado) for p in pts] == [(32000, 30000), (34000, 30000)]
    assert [p.patrimonio for p in evolucao_mensal(session, ATE)][-1] == 51000 + 34000
    assert evolucao_mensal(session, ATE, alocacao="cripto") == []
