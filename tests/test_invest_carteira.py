from datetime import date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import Ativo, CotacaoCache, MetaClasse, OperacaoInvest, SerieIndicador, set_config
from app.models.caixinha import Caixinha
from app.services.invest_carteira import (
    garantir_carteira_padrao, marcar_desejado, resumo_carteira, sobra_aporte, sugerir_aporte,
)
from app.services.mercado.cotacoes import CHAVE_DESATUALIZADAS
from tests.factories import nova_conta

HOJE = date(2026, 10, 5)


def ativo(s, classe, codigo, *, meta="0", simbolo=None, moeda="BRL", **kw):
    a = Ativo(classe=classe, codigo=codigo, simbolo_cotacao=simbolo if simbolo is not None else f"{codigo}.SA",
              moeda=moeda, meta_pct=D(meta), **kw)
    s.add(a)
    s.flush()
    return a


def compra(s, a, qtd, preco, em=date(2026, 8, 3), cambio=None, tipo="compra"):
    s.add(OperacaoInvest(ativo_id=a.id, tipo=tipo, data=em, quantidade=D(qtd), preco_unitario=preco,
                         cambio=None if cambio is None else D(cambio)))
    s.flush()


def cotacao(s, simbolo, preco, var=None, moeda="BRL", dia=HOJE):
    s.add(CotacaoCache(simbolo=simbolo, data=dia, fechamento=D(preco), moeda=moeda, fonte="yahoo",
                       variacao_dia_pct=None if var is None else D(var), atualizado_em=datetime(2026, 10, 5, 10)))
    s.flush()


def metas(s, **valores):
    for classe, v in valores.items():
        s.add(MetaClasse(classe=classe, meta_pct=D(v)))
    s.flush()


def cenario(s):
    metas(s, acoes=70, renda_fixa=30)
    petr = ativo(s, "acao", "PETR4", meta="60")
    vale = ativo(s, "acao", "VALE3", meta="40")
    hglg = ativo(s, "fii", "HGLG11", meta="100", alocacao="renda_fixa")  # só para ter uma segunda alocação
    compra(s, petr, "100", 3000)   # custo 3.000,00
    compra(s, vale, "10", 6000)    # custo 600,00
    compra(s, hglg, "10", 15000)   # custo 1.500,00
    cotacao(s, "PETR4.SA", "40", var="1.5")      # saldo 4.000
    cotacao(s, "VALE3.SA", "50")                  # saldo 500
    cotacao(s, "HGLG11.SA", "160")                # saldo 1.600
    s.commit()
    return petr, vale, hglg


def test_resumo_por_ativo_classe_e_totais(session):
    cenario(session)
    c = resumo_carteira(session, HOJE)
    assert c.patrimonio == 610000 and c.aplicado == 510000
    assert c.lucro_nao_realizado == 100000 and c.lucro_realizado == 0
    assert c.rent_pct == D(100000) / 510000 * 100
    acao, fii = c.classes[0], c.classes[1]
    assert (acao.classe, acao.saldo, acao.custo, fii.saldo) == ("acoes", 450000, 360000, 160000)
    assert acao.pct_atual == D(450000) / 610000 * 100 and acao.meta_pct == 70
    p = acao.ativos[0]
    assert p.ativo.codigo == "PETR4" and p.quantidade == 100
    assert (p.custo, p.saldo, p.cotacao_brl, p.preco_medio_brl) == (300000, 400000, D(4000), D(3000))
    assert p.rent_brl == 100000 and p.rent_pct == D(100000) / 300000 * 100
    assert p.variacao_dia_pct == D("1.5")
    assert p.pct_classe == D(400000) / 450000 * 100 and p.pct_carteira == D(400000) / 610000 * 100
    assert (p.meta_classe_pct, p.meta_ativo_pct, p.meta_carteira_pct) == (70, 60, D("42"))
    assert not p.desatualizada and not p.preco_manual and not p.sem_cotacao


def test_valor_por_local_reparte_pela_quantidade_de_cada_custodia(session):
    cenario(session)
    inter = nova_conta(session, "Inter Invest", tipo="investimento")
    bolsa = Caixinha(conta_id=inter.id, nome="Bolsa")
    session.add(bolsa)
    session.flush()
    petr = session.scalars(select(OperacaoInvest).where(OperacaoInvest.ativo_id == 1)).one()
    petr.custodia_conta_id, petr.custodia_caixinha_id = inter.id, bolsa.id      # 100 PETR4 na Bolsa
    compra(session, session.get(Ativo, 1), "100", 3000)                          # mais 100 sem local
    session.commit()
    c = resumo_carteira(session, HOJE)
    # PETR4: 200 x 40 = 8.000, metade em cada; VALE3 e HGLG11 sem local
    assert c.por_corretora == {"Inter Invest · Bolsa": 400000, "Sem local": 400000 + 210000}


def test_posicao_zerada_sem_meta_fica_fora(session):
    a = ativo(session, "acao", "ITUB4")
    compra(session, a, "10", 3000)
    compra(session, a, "10", 3500, em=date(2026, 9, 1), tipo="venda")
    cotacao(session, "ITUB4.SA", "36")
    session.commit()
    c = resumo_carteira(session, HOJE)
    assert c.ativos == []


def test_lucro_realizado_entra_nos_totais(session):
    a = ativo(session, "acao", "ITUB4", meta="100")
    compra(session, a, "10", 3000)
    compra(session, a, "4", 3500, em=date(2026, 9, 1), tipo="venda")
    cotacao(session, "ITUB4.SA", "36")
    session.commit()
    c = resumo_carteira(session, HOJE)
    assert c.lucro_realizado == 2000 and c.ativos[0].lucro_realizado == 2000
    assert c.ativos[0].quantidade == 6


def test_exterior_converte_pelo_cambio_em_cache(session):
    metas(session, acoes=100)
    a = ativo(session, "exterior", "AAPL", simbolo="AAPL", moeda="USD", meta="100")
    compra(session, a, "2", 15000, cambio="5.00")   # US$ 150 x 2 x 5 = 1.500,00
    cotacao(session, "AAPL", "200", moeda="USD")
    cotacao(session, "USDBRL=X", "5.50")
    session.commit()
    c = resumo_carteira(session, HOJE)
    p = c.ativos[0]
    assert p.custo == 150000 and p.cotacao_brl == D("110000.00") and p.saldo == 220000
    assert c.cambio == D("5.50")


def test_sem_cotacao_usa_custo_e_marca(session):
    a = ativo(session, "acao", "BBAS3")
    compra(session, a, "10", 2000)
    session.commit()
    p = resumo_carteira(session, HOJE).ativos[0]
    assert p.sem_cotacao and p.saldo == 20000 and p.rent_brl == 0 and p.cotacao_brl is None


def test_selo_desatualizada_e_preco_manual(session):
    petr, vale, _ = cenario(session)
    set_config(session, CHAVE_DESATUALIZADAS, [petr.id])
    vale.preco_manual, vale.preco_manual_em = 5500, HOJE
    session.commit()
    c = {x.ativo.codigo: x for x in resumo_carteira(session, HOJE).ativos}
    assert c["PETR4"].desatualizada and not c["PETR4"].preco_manual
    assert c["VALE3"].preco_manual and c["VALE3"].saldo == 55000


def test_renda_fixa_usa_valor_atualizado(session):
    metas(session, renda_fixa=100)
    a = ativo(session, "renda_fixa", "CDB X", simbolo="", meta="100", indexador="pre", taxa=D("10"),
              vencimento=date(2030, 1, 1))
    compra(session, a, "1", 100000, em=date(2026, 10, 1), tipo="aplicacao")
    for dia in (2, 5):
        session.add(SerieIndicador(serie="cdi", data=date(2026, 10, dia), valor_pct=D("0.05")))
    session.commit()
    p = resumo_carteira(session, HOJE).ativos[0]
    assert p.custo == 100000 and p.saldo > 100000 and p.rent_brl == p.saldo - 100000
    assert p.cotacao_brl is None and not p.sem_cotacao


def test_meta_sem_posicao_aparece_na_classe(session):
    metas(session, cripto=10)
    ativo(session, "cripto", "BTC", simbolo="BTC-BRL", meta="100")
    session.commit()
    c = resumo_carteira(session, HOJE)
    assert [x.classe for x in c.classes] == ["cripto"] and c.patrimonio == 0


# ---------- rebalanceamento ----------

def test_sugestao_so_em_ativos_abaixo_da_meta(session):
    cenario(session)
    c = resumo_carteira(session, HOJE)
    # total 6.100 + 1.000 = 7.100; ideal acao 4.970 (atual 4.500, deficit 470); fii 2.130 (atual 1.600, deficit 530)
    # na acao so VALE3 tem deficit (PETR4 acima da meta)
    sug = sugerir_aporte(c, 100000)
    por = {x.ativo.codigo: x for x in sug}
    assert set(por) == {"VALE3", "HGLG11"}
    assert por["HGLG11"].quantidade == 3         # 53.000 / 160 = 3,31 -> 3
    assert por["HGLG11"].valor == 48000
    # a classe com mais falta no fim é a de FIIs (faltam 50), mas a cota (160) não cabe nos 70 que sobram: em vez de
    # deixar o dinheiro parado, compra mais uma VALE3 (ações ainda tinham 20 de falta)
    assert por["VALE3"].quantidade == 10 and por["VALE3"].valor == 50000
    assert sobra_aporte(sug, 100000) == 2000


def test_sobra_compra_cota_inteira_onde_mais_falta(session):
    """Caso real: R$ 224 em renda fixa, dois ETFs 50/50 (R$ 137,39 e R$ 127,94). Meio a meio (R$ 112 cada) não
    compra nenhum; a sobra tem de comprar 1 cota de um deles."""
    metas(session, acoes=80, renda_fixa=20)
    acao = ativo(session, "acao", "AUVP11", meta="100")
    b5 = ativo(session, "etf", "B5MB11", meta="50", alocacao="renda_fixa")
    lf = ativo(session, "etf", "LFTB11", meta="50", alocacao="renda_fixa")
    compra(session, acao, 20, 13000)
    cotacao(session, "AUVP11.SA", "130")
    cotacao(session, "B5MB11.SA", "137.39")
    cotacao(session, "LFTB11.SA", "127.94")
    session.commit()
    c = resumo_carteira(session, HOJE)
    sug = [x for x in sugerir_aporte(c, 22400) if x.quantidade]
    assert len(sug) == 1 and sug[0].quantidade == 1 and sug[0].ativo.codigo in ("B5MB11", "LFTB11")
    assert sobra_aporte(sug, 22400) < 12794  # o que sobra não compra mais nenhuma cota
    assert all(x.quantidade != 0 for x in sugerir_aporte(c, 22400))  # sem linhas de 0 cotas


def test_sugestao_cripto_aceita_8_casas_e_renda_fixa_sem_quantidade(session):
    metas(session, cripto=50, renda_fixa=50)
    ativo(session, "cripto", "BTC", simbolo="BTC-BRL", meta="100")
    ativo(session, "renda_fixa", "CDB X", simbolo="", meta="100", indexador="pre", taxa=D("10"))
    cotacao(session, "BTC-BRL", "300000")
    session.commit()
    c = resumo_carteira(session, HOJE)
    sug = {x.ativo.codigo: x for x in sugerir_aporte(c, 100000)}
    assert sug["BTC"].quantidade == D("0.00166666") and sug["BTC"].valor == 49999
    assert sug["CDB X"].quantidade is None and sug["CDB X"].valor == 50000


def test_sugestao_sem_aporte(session):
    cenario(session)
    c = resumo_carteira(session, HOJE)
    assert sugerir_aporte(c, 0) == []
    assert sugerir_aporte(c, -5) == []


def test_sugestao_ativo_sem_cotacao_nao_gasta(session):
    metas(session, acoes=100)
    ativo(session, "acao", "ABEV3", meta="100")
    session.commit()
    c = resumo_carteira(session, HOJE)
    sug = sugerir_aporte(c, 100000)
    assert [(x.valor, x.quantidade) for x in sug] == [(0, None)]
    assert sobra_aporte(sug, 100000) == 100000


# ---------- % ideal e "Comprar?" ----------

def test_ideal_e_meta_do_ativo_vezes_meta_da_alocacao(session):
    cenario(session)
    c = {x.ativo.codigo: x for x in resumo_carteira(session, HOJE).ativos}
    assert (c["PETR4"].ideal_classe_pct, c["PETR4"].ideal_pct) == (60, D("42"))
    assert (c["VALE3"].ideal_pct, c["HGLG11"].ideal_pct) == (D("28"), D("30"))


def test_ativo_com_meta_e_sem_posicao_aparece(session):
    metas(session, acoes=100)
    ativo(session, "acao", "WEGE3", meta="100")
    session.commit()
    c = resumo_carteira(session, HOJE)
    assert [x.ativo.codigo for x in c.ativos] == ["WEGE3"] and c.ativos[0].ideal_pct == 100


def test_comprar_sim_abaixo_do_ideal_e_nao_nos_demais(session):
    cenario(session)
    c = {x.ativo.codigo: x for x in resumo_carteira(session, HOJE).ativos}
    assert c["VALE3"].comprar          # 8,2% da carteira contra 28% de ideal
    assert not c["PETR4"].comprar      # 65,6% contra 42%
    assert c["HGLG11"].comprar         # 26,2% contra 30%


def test_comprar_nao_quando_ideal_zero(session):
    cenario(session)
    session.get(Ativo, 1).meta_pct, session.get(Ativo, 2).meta_pct = D(100), D(0)
    session.commit()
    c = {x.ativo.codigo: x for x in resumo_carteira(session, HOJE).ativos}
    assert c["VALE3"].ideal_pct == 0 and not c["VALE3"].comprar


def test_resumo_da_classe_para_o_cabecalho_do_bloco(session):
    petr, _, _ = cenario(session)
    compra(session, petr, "10", 3500, em=date(2026, 9, 1), tipo="venda")   # lucro realizado de 50,00
    session.commit()
    acao = resumo_carteira(session, HOJE).classes[0]
    # PETR4 90 x 40 = 3.600 (custo 2.700) + VALE3 500 (custo 600)
    assert (acao.saldo, acao.custo, acao.lucro_realizado) == (410000, 330000, 5000)
    assert acao.variacao_pct == D(80000) / 330000 * 100
    assert acao.rent_pct == D(85000) / 330000 * 100       # com o lucro realizado
    assert acao.n_ativos == 2


def test_ativo_sem_operacao_aparece_mesmo_sem_meta(session):
    """Cadastrado sem compra ("ainda não comprei"): fica na lista para receber meta; não vai para a reserva."""
    metas(session, acoes=100)
    marcar_desejado(session, ativo(session, "acao", "WEGE3"), {garantir_carteira_padrao(session).id})
    ativo(session, "acao", "ITSA4")  # sem operação e sem marcar em nenhuma carteira: não aparece
    session.commit()
    c = resumo_carteira(session, HOJE)
    assert [(x.ativo.codigo, x.saldo, x.quantidade) for x in c.ativos] == [("WEGE3", 0, 0)]
    assert c.reserva == [] and c.patrimonio == 0


def test_sobra_respeita_a_classe_com_mais_falta_antes_do_ativo(session):
    """Renda fixa (gap 20) vem antes; a sobra dela não pode ir para um ativo de outra classe com falta maior em R$.
    Caso da Ana com R$ 1.000: o MUND11 (renda variável, muito abaixo da meta dentro da classe) levava a sobra
    da renda fixa."""
    metas(session, acoes=75, renda_fixa=20, cripto=5)
    velho = ativo(session, "acao", "VELH3")                     # posição antiga, sem meta: pesa na classe
    auvp = ativo(session, "etf", "AUVP11", meta="20")
    mund = ativo(session, "etf", "MUND11", meta="80")
    b5 = ativo(session, "etf", "B5MB11", meta="50", alocacao="renda_fixa")
    lf = ativo(session, "etf", "LFTB11", meta="50", alocacao="renda_fixa")
    gbtc = ativo(session, "etf", "GBTC11", meta="100", alocacao="cripto")
    compra(session, velho, 1, 213040)
    compra(session, auvp, 3, 12977)
    compra(session, gbtc, 1, 16575)
    for simbolo, preco in (("VELH3.SA", "2130.40"), ("AUVP11.SA", "129.77"), ("MUND11.SA", "103.49"),
                           ("B5MB11.SA", "137.39"), ("LFTB11.SA", "127.94"), ("GBTC11.SA", "165.75")):
        cotacao(session, simbolo, preco)
    session.commit()
    c = resumo_carteira(session, HOJE)
    sug = sugerir_aporte(c, 100000)
    por = {x.ativo.alocacao: 0 for x in sug}
    for x in sug:
        por[x.ativo.alocacao] += x.valor
    assert sug[0].ativo.alocacao == "renda_fixa"                 # a lista começa pela classe com mais gap
    assert por["renda_fixa"] >= 65000                             # a renda fixa leva a parte dela (~R$ 737)
    assert por.get("acoes", 0) <= 31100                           # a renda variável fica perto dos ~R$ 244
    assert sobra_aporte(sug, 100000) < 10349                      # e quase nada fica parado: nem a cota mais barata cabe


def test_falta_para_a_meta_dentro_da_classe(session):
    """Base: o maior entre o saldo da classe e o alvo dela. Ações: saldo 4.500 > alvo 4.270 (70% de 6.100) -> base
    4.500; PETR4 60% = 2.700 (tem 4.000: acima), VALE3 40% = 1.800 (tem 500: faltam 1.300). Renda fixa: saldo 1.600 <
    alvo 1.830 (30%) -> HGLG11 100% de 1.830: faltam 230."""
    cenario(session)
    falta = {a.ativo.codigo: a.falta_classe for a in resumo_carteira(session, HOJE).ativos}
    assert falta == {"PETR4": -130000, "VALE3": 130000, "HGLG11": 23000}


def test_classe_tem_falta_em_reais_e_ativos_ordenados_pela_meta(session):
    """Ações: meta 70% de 6.100 = 4.270, tem 4.500 -> passa 230; renda fixa 30% = 1.830, tem 1.600 -> faltam 230.
    Dentro da classe, a maior meta primeiro (PETR4 60 antes de VALE3 40; com meta igual, o maior saldo)."""
    petr, vale, _ = cenario(session)
    c = resumo_carteira(session, HOJE)
    assert {x.classe: x.falta for x in c.classes} == {"acoes": -23000, "renda_fixa": 23000}
    petr.meta_pct, vale.meta_pct = D(30), D(70)
    session.commit()
    assert [a.ativo.codigo for a in resumo_carteira(session, HOJE).classes[0].ativos] == ["VALE3", "PETR4"]
    petr.meta_pct, vale.meta_pct = D(50), D(50)
    session.commit()
    assert [a.ativo.codigo for a in resumo_carteira(session, HOJE).classes[0].ativos] == ["PETR4", "VALE3"]


def test_aporte_vai_inteiro_para_a_classe_com_mais_falta_antes_de_dividir(session):
    """Caso da Ana: R$ 150, renda variável acima da meta, renda fixa (meta 20%) vazia com cotas de R$ 137 e
    R$ 128, alternativo (meta 5%) vazio com cota de R$ 29. Dividir proporcionalmente (120/30) comprava só 1 GBTC11;
    o certo é 1 cota de renda fixa, a classe com mais falta."""
    metas(session, acoes=75, renda_fixa=20, cripto=5)
    rv = ativo(session, "etf", "WRLD11", meta="100")
    ativo(session, "etf", "B5MB11", meta="50", alocacao="renda_fixa")
    ativo(session, "etf", "LFTB11", meta="50", alocacao="renda_fixa")
    ativo(session, "etf", "GBTC11", meta="100", alocacao="cripto")
    compra(session, rv, 1, 251389)
    for simbolo, preco in (("WRLD11.SA", "2513.89"), ("B5MB11.SA", "137.39"), ("LFTB11.SA", "127.94"),
                           ("GBTC11.SA", "29.37")):
        cotacao(session, simbolo, preco)
    session.commit()
    sug = sugerir_aporte(resumo_carteira(session, HOJE), 15000)
    assert [(x.ativo.codigo, x.quantidade) for x in sug] == [("B5MB11", 1)]
    assert sobra_aporte(sug, 15000) == 1261
    # a prioridade não trava: sem dinheiro para a cota da renda fixa, aproveita no alternativo (3 × 29,37)
    sug = sugerir_aporte(resumo_carteira(session, HOJE), 10000)
    assert [(x.ativo.codigo, x.quantidade) for x in sug] == [("GBTC11", 3)] and sobra_aporte(sug, 10000) == 1189
    # R$ 300: 1 B5MB11 (137,39) e, com a renda fixa ainda na frente, 1 LFTB11 (127,94); sobram 34,67: 1 GBTC11
    sug = sugerir_aporte(resumo_carteira(session, HOJE), 30000)
    assert [(x.ativo.codigo, x.quantidade) for x in sug] == [("B5MB11", 1), ("LFTB11", 1), ("GBTC11", 1)]
    assert sobra_aporte(sug, 30000) == 530


def test_plano_do_aporte_mostra_antes_e_depois_e_quem_fica_de_fora(session):
    """Cenário da Ana com R$ 150: 1 B5MB11. Antes/depois em % e falta em reais, por classe e por ativo."""
    from app.services.invest_carteira import plano_aporte
    metas(session, acoes=75, renda_fixa=20, cripto=5)
    rv = ativo(session, "etf", "WRLD11", meta="100")
    ativo(session, "etf", "B5MB11", meta="50", alocacao="renda_fixa")
    ativo(session, "etf", "LFTB11", meta="50", alocacao="renda_fixa")
    ativo(session, "etf", "GBTC11", meta="100", alocacao="cripto")
    compra(session, rv, 1, 251389)
    for simbolo, preco in (("WRLD11.SA", "2513.89"), ("B5MB11.SA", "137.39"), ("LFTB11.SA", "127.94"),
                           ("GBTC11.SA", "29.37")):
        cotacao(session, simbolo, preco)
    session.commit()
    carteira = resumo_carteira(session, HOJE)
    plano = plano_aporte(carteira, 15000)
    assert plano.sugestoes == sugerir_aporte(carteira, 15000)
    por = {c.classe: c for c in plano.classes}
    assert [c.classe for c in plano.classes] == ["renda_fixa", "cripto", "acoes"]   # ordem do gap
    rf = por["renda_fixa"]
    # total com o aporte 2.663,89: renda fixa 20% = 532,78; comprou 137,39 -> faltam 395,39
    assert (rf.falta_antes, rf.recebeu, rf.falta_depois, rf.fora) == (53278, 13739, 39539, False)
    assert rf.pct_antes == 0 and round(rf.pct_depois, 2) == D("5.18")             # 137,39 / 2.651,28
    assert por["acoes"].fora and round(por["acoes"].pct_depois, 2) == D("94.82")
    b5, lf = rf.ativos
    assert (b5.ativo.codigo, b5.falta_antes, b5.falta_depois, b5.pct_depois) == ("B5MB11", 26639, 12900, 100)
    assert (lf.ativo.codigo, lf.falta_antes, lf.falta_depois, lf.pct_depois) == ("LFTB11", 26639, 26639, 0)
    assert [a.ativo.codigo for a in por["acoes"].ativos if a.fora] == ["WRLD11"]


def _tesouro(s, codigo, vencimento, pu_compra, pu_venda, meta="100"):
    """Título do Tesouro por títulos, com o preço do dia (centavos por título)."""
    from app.models import TaxaTesouro
    a = ativo(s, "renda_fixa", codigo, meta=meta, simbolo="", indexador="cdi", vencimento=vencimento, por_titulos=True)
    s.add(TaxaTesouro(titulo=codigo.rsplit(" ", 1)[0], vencimento=vencimento, data=HOJE, taxa_compra=D("0.1"),
                      pu_compra=pu_compra, pu_venda=pu_venda))
    s.flush()
    return a


def test_aporte_no_tesouro_compra_lotes_de_um_centesimo_de_titulo(session):
    """Caso da Ana: Selic 2031 a R$ 20.090,00 só se compra de 0,01 em 0,01 título (R$ 200,90). O plano
    sugeria "aplicar R$ 130,24", que o Tesouro não aceita."""
    metas(session, renda_fixa=80, acoes=20)
    selic = _tesouro(session, "Tesouro Selic 2031", date(2031, 3, 1), pu_compra=2009000, pu_venda=2008238)
    ativo(session, "etf", "PACB11", meta="100")
    compra(session, selic, "0.01", 2000000, tipo="aplicacao")    # saldo 0,01 x 20.082,38 = 200,82
    cotacao(session, "PACB11.SA", "11.22")
    session.commit()
    # R$ 220: a renda fixa (falta 135,84) leva 0,01 título pelo PU de compra; o resto, 1 cota de PACB11
    sug = sugerir_aporte(resumo_carteira(session, HOJE), 22000)
    assert [(x.ativo.codigo, x.quantidade, x.valor) for x in sug] == [            # na ordem dos blocos (gap)
        ("PACB11", 1, 1122), ("Tesouro Selic 2031", D("0.01"), 20090)]
    assert sobra_aporte(sug, 22000) == 788
    # R$ 150: 0,01 título não cabe; vai para a renda variável e não sobra "aplicar" quebrado no Tesouro
    sug = sugerir_aporte(resumo_carteira(session, HOJE), 15000)
    assert [(x.ativo.codigo, x.quantidade) for x in sug] == [("PACB11", 7)]


def test_aporte_no_tesouro_respeita_o_minimo_de_30_reais(session):
    """IPCA+ 2050 a R$ 914,28: 0,01 título dá R$ 9,14, abaixo do mínimo de R$ 30; a primeira compra é de 0,04."""
    metas(session, renda_fixa=100)
    _tesouro(session, "Tesouro IPCA+ 2050", date(2050, 8, 15), pu_compra=91428, pu_venda=89000)
    session.commit()
    # R$ 50: 0,04 (36,58) e mais 0,01 (cabe e ainda há falta); 0,05 x 914,28 = 45,71
    (sg,) = sugerir_aporte(resumo_carteira(session, HOJE), 5000)
    assert (sg.quantidade, sg.valor) == (D("0.05"), 4572)
    assert sugerir_aporte(resumo_carteira(session, HOJE), 2500) == []      # menos que o mínimo: não compra


def test_renda_fixa_na_curva_varia_pelo_ultimo_dia_util_do_cdi(session):
    """CDB 100% do CDI: a variação do dia é o CDI do último dia útil da série, sobre o que já estava aplicado
    na véspera (o aporte de hoje não conta como rendimento)."""
    metas(session, renda_fixa=100)
    a = ativo(session, "renda_fixa", "CDB Y", simbolo="", meta="100", indexador="cdi", taxa=D("100"),
              vencimento=date(2030, 1, 1))
    compra(session, a, "1", 10_000_000, em=date(2026, 10, 1), tipo="aplicacao")
    compra(session, a, "1", 50_000, em=HOJE, tipo="aplicacao")
    for dia, taxa in ((2, "0.05"), (5, "0.06")):
        session.add(SerieIndicador(serie="cdi", data=date(2026, 10, dia), valor_pct=D(taxa)))
    session.commit()
    p = resumo_carteira(session, HOJE).ativos[0]
    assert p.variacao_dia_pct == D("0.06")


def test_renda_fixa_sem_dois_dias_de_cdi_nao_tem_variacao(session):
    metas(session, renda_fixa=100)
    a = ativo(session, "renda_fixa", "CDB Y", simbolo="", meta="100", indexador="cdi", taxa=D("100"))
    compra(session, a, "1", 100_000, em=date(2026, 10, 1), tipo="aplicacao")
    session.add(SerieIndicador(serie="cdi", data=date(2026, 10, 2), valor_pct=D("0.05")))
    session.commit()
    assert resumo_carteira(session, HOJE).ativos[0].variacao_dia_pct is None


def test_tesouro_varia_pelo_preco_de_venda_do_dia_anterior(session):
    from app.models import TaxaTesouro
    metas(session, renda_fixa=100)
    selic = _tesouro(session, "Tesouro Selic 2031", date(2031, 3, 1), pu_compra=2_011_000, pu_venda=2_010_000)
    session.add(TaxaTesouro(titulo="Tesouro Selic", vencimento=date(2031, 3, 1), data=date(2026, 10, 2),
                            taxa_compra=D("0.1"), pu_compra=2_001_000, pu_venda=2_000_000))
    compra(session, selic, "1", 2_000_000, em=date(2026, 9, 1), tipo="aplicacao")
    session.commit()
    assert resumo_carteira(session, HOJE).ativos[0].variacao_dia_pct == D("0.5")


def test_tesouro_sem_preco_anterior_nao_tem_variacao(session):
    metas(session, renda_fixa=100)
    selic = _tesouro(session, "Tesouro Selic 2031", date(2031, 3, 1), pu_compra=2_011_000, pu_venda=2_010_000)
    compra(session, selic, "1", 2_000_000, em=date(2026, 9, 1), tipo="aplicacao")
    session.commit()
    assert resumo_carteira(session, HOJE).ativos[0].variacao_dia_pct is None
