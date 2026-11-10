from datetime import date
from decimal import Decimal as D

from app.services.invest_carteira import resumo_carteira
from app.services.invest_historico import PontoMes, evolucao_mensal
from app.services.invest_resumo import (
    CORES_ATIVOS, CORES_CLASSE, barras_evolucao, cards_resumo, distribuicao, rentabilidade_12m,
    rentabilidade_no_periodo,
)
from tests.test_invest_carteira import HOJE, ativo, cenario, compra, metas
from tests.test_invest_historico import ATE, cenario_petr


def ponto(competencia, patrimonio=0, aplicado=0, acum="0"):
    return PontoMes(competencia=competencia, patrimonio=patrimonio, aplicado_acumulado=aplicado,
                    rent_mes_pct=D(0), rent_acum_pct=D(acum), cdi_acum_pct=None, ibov_acum_pct=None)


def meses(n, ano=2025, mes=1):
    resultado = []
    for _ in range(n):
        resultado.append(f"{ano:04d}-{mes:02d}")
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return resultado


# ---------- cards ----------

def test_cards_investido_ganho_e_rentabilidade_total(session):
    cenario_petr(session)
    carteira = resumo_carteira(session, ATE)
    pontos = evolucao_mensal(session, ATE)
    c = cards_resumo(carteira, pontos)
    assert (c.patrimonio, c.investido, c.ganho) == (51000, 46500, 4500)   # 15 x 34; custo 30.000 + 16.500
    assert c.variacao_pct == D(4500) / 46500 * 100
    assert c.rent_total_pct == pontos[-1].rent_acum_pct
    assert c.rent_12m_pct is None          # só 4 meses de histórico: "—"


def test_cards_sem_historico(session):
    cenario(session)
    c = cards_resumo(resumo_carteira(session, HOJE), [])
    assert c.rent_total_pct == D(100000) / 510000 * 100 and c.rent_total_simples   # sem histórico: a simples
    assert c.rent_12m_pct is None
    assert c.ganho == 100000 and c.investido == 510000


def test_cards_total_simples_quando_o_historico_so_tem_hoje(session):
    from app.models import CotacaoCache, OperacaoInvest
    from tests.test_invest_carteira import cotacao
    metas(session, acoes=100)
    for codigo, qtd, custo, preco in (("BOVA11", "10", 10000, "120"), ("IVVB11", "5", 30000, "330")):
        a = ativo(session, "etf", codigo)
        session.add(OperacaoInvest(ativo_id=a.id, tipo="ajuste_posicao", data=HOJE, quantidade=D(qtd),
                                   preco_unitario=custo))
        cotacao(session, f"{codigo}.SA", preco)
    session.commit()
    carteira = resumo_carteira(session, HOJE)
    pontos = evolucao_mensal(session, HOJE)
    assert len(pontos) == 1                             # só o ponto de hoje: Dietz não mede nada
    c = cards_resumo(carteira, pontos)
    assert (c.investido, c.ganho) == (250000, 35000)    # custo 1.000 + 1.500; ganho 1.200 + 1.650 - 2.500
    assert c.rent_total_pct == D(35000) / 250000 * 100 and c.rent_total_simples
    assert c.rent_12m_pct is None


def test_cards_total_pelo_historico_nao_e_simples(session):
    cenario_petr(session)
    c = cards_resumo(resumo_carteira(session, ATE), evolucao_mensal(session, ATE))
    assert not c.rent_total_simples


def test_rentabilidade_12m_precisa_de_13_pontos():
    acum = ["10"] + ["15"] * 11 + ["21"]
    pontos = [ponto(m, acum=a) for m, a in zip(meses(13), acum)]
    assert rentabilidade_12m(pontos) == (D("1.21") / D("1.10") - 1) * 100       # 10%
    assert rentabilidade_12m(pontos[1:]) is None
    assert rentabilidade_12m([]) is None


# ---------- evolução empilhada ----------

def test_barras_aplicado_embaixo_e_ganho_em_cima():
    pontos = [ponto("2026-07", 32000, 30000), ponto("2026-08", 28000, 30000)]
    jul, ago = barras_evolucao(pontos, None)
    assert (jul.rotulo, jul.base, jul.topo) == ("07/26", 30000, 2000)
    assert (jul.aplicado, jul.ganho, jul.patrimonio) == (30000, 2000, 32000)
    # ganho negativo: o segmento de ganho some e a barra de aplicado mostra o patrimônio
    assert (ago.base, ago.topo) == (28000, 0)
    assert (ago.aplicado, ago.ganho, ago.patrimonio) == (30000, -2000, 28000)


def test_barras_filtro_de_periodo():
    pontos = [ponto(m, 100, 100) for m in meses(30)]
    assert len(barras_evolucao(pontos, 12)) == 12 and barras_evolucao(pontos, 12)[-1].rotulo == "06/27"
    assert len(barras_evolucao(pontos, 24)) == 24
    assert len(barras_evolucao(pontos, None)) == 30
    assert len(barras_evolucao(pontos[:5], 12)) == 5


def test_barras_com_filtro_de_alocacao(session):
    cenario_petr(session)
    assert [b.patrimonio for b in barras_evolucao(evolucao_mensal(session, ATE, alocacao="acoes"), 12)] == \
        [32000, 52500, 49500, 51000]
    assert barras_evolucao(evolucao_mensal(session, ATE, alocacao="renda_fixa"), 12) == []


# ---------- rosca ----------

def test_distribuicao_por_classe(session):
    cenario(session)
    fatias = distribuicao(resumo_carteira(session, HOJE))
    assert [(f.rotulo, f.valor, f.cor) for f in fatias] == [
        ("Renda variável", 450000, CORES_CLASSE["acoes"]), ("Renda fixa", 160000, CORES_CLASSE["renda_fixa"])]
    assert fatias[0].pct == D(450000) / 610000 * 100


def test_distribuicao_dos_ativos_de_uma_classe(session):
    cenario(session)
    fatias = distribuicao(resumo_carteira(session, HOJE), "acoes")
    assert [(f.rotulo, f.valor) for f in fatias] == [("PETR4", 400000), ("VALE3", 50000)]
    assert fatias[0].pct == D(400000) / 450000 * 100          # % dentro da classe
    assert [f.cor for f in fatias] == list(CORES_ATIVOS[:2])
    assert distribuicao(resumo_carteira(session, HOJE), "cripto") == []


def test_distribuicao_agrupa_o_excedente_em_outros(session):
    metas(session, acoes=100)
    for i in range(len(CORES_ATIVOS) + 2):
        a = ativo(session, "acao", f"AT{i:02d}3")
        compra(session, a, "1", 1000 * (i + 1))
    session.commit()
    fatias = distribuicao(resumo_carteira(session, date(2026, 10, 5)), "acoes")
    assert len(fatias) == len(CORES_ATIVOS) + 1
    assert fatias[-1].rotulo == "Outros" and fatias[-1].valor == 1000 + 2000   # os dois menores
    assert fatias[0].valor == 1000 * (len(CORES_ATIVOS) + 2)                    # do maior para o menor
    assert len(set(f.cor for f in fatias)) == len(fatias)


def test_rentabilidade_no_periodo_recomeca_do_zero_no_recorte():
    comps = meses(4)
    acumulados = [("10", "1", "3"), ("21", "2.01", None), ("32.1", "3.0301", None), ("10", "4.0604", "-5")]
    pontos = [PontoMes(c, 0, 0, D(0), D(r), D(cdi), D(ibov) if ibov else None, D(cdi) * 2)
              for c, (r, cdi, ibov) in zip(comps, acumulados)]
    ultimos = rentabilidade_no_periodo(pontos, 2)
    assert [p.competencia for p in ultimos] == comps[-3:]  # o mês anterior ao recorte abre a linha em 0%
    # carteira: 1,321/1,21 e 1,10/1,21; CDI 1% ao mês; Ibovespa sem base no mês anterior conta de 0%
    assert [round(p.rent_acum_pct, 4) for p in ultimos] == [D("0"), D("9.1736"), D("-9.0909")]
    assert [round(p.cdi_acum_pct, 4) for p in ultimos] == [D("0"), D("1.0000"), D("2.0100")]
    assert [p.ibov_acum_pct for p in ultimos] == [None, None, D("-5")]
    assert [round(p.global_acum_pct, 4) for p in ultimos] == [D("0"), D("1.9614"), D("3.9423")]  # 1,060602 e 1,081208 sobre 1,0402


def test_rentabilidade_no_periodo_sem_historico_suficiente_mostra_tudo_desde_o_zero():
    pontos = [ponto(c, acum=str(i + 1)) for i, c in enumerate(meses(3))]
    for meses_recorte in (12, None):
        inicio, *resto = rentabilidade_no_periodo(pontos, meses_recorte)
        assert resto == pontos  # nada refeito: o histórico já é medido desde antes da primeira operação
        assert (inicio.competencia, inicio.rent_acum_pct, inicio.cdi_acum_pct) == ("2024-12", D(0), None)
    assert rentabilidade_no_periodo([], None) == []
