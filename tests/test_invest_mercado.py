"""Cards de topo dos investimentos: variação do dia da carteira e indicadores de mercado (CDI, IPCA, Ibovespa, dólar)."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from app.models import CotacaoCache, SerieIndicador
from app.services.invest_historico import PontoMes
from app.services.invest_resumo import acima_da_inflacao, mercado, mes_atual, variacao_do_dia

HOJE = date(2026, 10, 28)
AGORA = datetime(2026, 10, 28, 15, 0)


class _Ativo:
    def __init__(self, saldo, variacao):
        self.saldo, self.variacao_dia_pct = saldo, variacao


class _Carteira:
    def __init__(self, ativos):
        self.ativos = ativos


def test_variacao_do_dia_soma_o_que_cada_ativo_mudou_desde_o_fechamento_anterior():
    # 110 que subiram 10% (eram 100) e 95 que caíram 5% (eram 100); renda fixa sem variação fica de fora
    carteira = _Carteira([_Ativo(11_000, D(10)), _Ativo(9_500, D(-5)), _Ativo(50_000, None)])
    valor, pct = variacao_do_dia(carteira)
    assert valor == 500                                   # +1.000 − 500
    assert round(pct, 4) == round(D(500) / D(70_000) * 100, 4)   # sobre o patrimônio de ontem (70.500 − 500)


def test_variacao_do_dia_sem_cotacao_com_variacao_e_none():
    assert variacao_do_dia(_Carteira([_Ativo(5_000, None)])) is None


def test_acima_da_inflacao():
    assert round(acima_da_inflacao(D("10.25"), D(5)), 6) == D(5)
    assert acima_da_inflacao(None, D(5)) is None and acima_da_inflacao(D(5), None) is None


def test_mercado_cdi_ipca_ibovespa_e_dolar(session):
    # CDI: 0,05% ao dia em 3 dias do último ano e um de antes (fora da conta)
    for dias in (1, 2, 3, 400):
        session.add(SerieIndicador(serie="cdi", data=HOJE - timedelta(days=dias), valor_pct=D("0.05")))
    # IPCA: 13 meses de 0,5%; os 12 mais recentes entram
    for i in range(13):
        mes = date(2025 + (i + 8) // 12, (i + 8) % 12 + 1, 1)   # set/25 … set/26
        session.add(SerieIndicador(serie="ipca", data=mes, valor_pct=D("0.5")))
    for simbolo, preco, var in (("^BVSP", "170891.44", "-2.84"), ("USDBRL=X", "5.3748", "0.58")):
        session.add(CotacaoCache(simbolo=simbolo, data=HOJE, fechamento=D(preco), moeda="BRL", fonte="yahoo",
                                 variacao_dia_pct=D(var), atualizado_em=AGORA))
        session.add(CotacaoCache(simbolo=simbolo, data=date(2026, 9, 30), fechamento=D(1), moeda="BRL",
                                 fonte="yahoo:mensal", atualizado_em=AGORA))   # histórico mensal não conta
    session.commit()
    m = mercado(session, HOJE)
    assert round(m.cdi_12m_pct, 6) == round((D("1.0005") ** 3 - 1) * 100, 6)
    assert round(m.ipca_12m_pct, 6) == round((D("1.005") ** 12 - 1) * 100, 6) and m.ipca_ate == date(2026, 9, 1)
    assert round(m.cdi_aa_pct, 2) == round((D("1.0005") ** 252 - 1) * 100, 2)
    assert (m.ibov, m.ibov_var_pct) == (D("170891.44"), D("-2.84"))
    assert (m.dolar, m.dolar_var_pct) == (D("5.3748"), D("0.58"))


def test_mercado_sem_dados(session):
    m = mercado(session, HOJE)
    assert m.cdi_12m_pct is None and m.ipca_12m_pct is None and m.cdi_aa_pct is None
    assert m.ibov is None and m.dolar is None


def _ponto(comp, patrimonio, aplicado, rent_mes):
    return PontoMes(comp, patrimonio, aplicado, D(rent_mes), D(0), None, None)


def test_mes_atual_separa_aportes_do_rendimento():
    pontos = [_ponto("2026-09", 100_000, 90_000, "1"), _ponto("2026-10", 112_000, 100_000, "2.1")]
    m = mes_atual(pontos)
    assert (m.aportes, m.rendimento, m.rent_pct) == (10_000, 2_000, D("2.1"))
    assert mes_atual(pontos[-1:]) is None and mes_atual([]) is None
