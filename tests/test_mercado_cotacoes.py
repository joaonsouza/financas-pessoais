import json
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import httpx
from sqlalchemy import select

from app.models import Ativo, CotacaoCache, OperacaoInvest, ProventoCache, SerieIndicador
from app.services.mercado.bcb import Bcb
from app.services.mercado.brapi import Brapi
from app.services.mercado.cotacoes import (
    atualizar_cotacoes, atualizar_series, cotacao_do_ativo, ids_desatualizados,
)
from app.services.mercado.yahoo import Yahoo
from tests.test_mercado_provedores import HttpFalso, sem_rede_real  # noqa: F401

AGORA = datetime(2026, 10, 5, 10, 0)
ROTAS = {"PETR4.SA": "yahoo_petr4.json", "AAPL": "yahoo_aapl.json",
         "USDBRL%3DX": "yahoo_usdbrl.json", "%5EBVSP": "yahoo_bvsp.json", "chart/VT": "yahoo_vt.json"}


def novo_ativo(s, classe="acao", codigo="PETR4", simbolo="PETR4.SA", moeda="BRL", op_em=date(2026, 7, 15)):
    a = Ativo(classe=classe, codigo=codigo, simbolo_cotacao=simbolo, moeda=moeda)
    s.add(a)
    s.flush()
    if op_em:
        s.add(OperacaoInvest(ativo_id=a.id, tipo="compra", data=op_em, quantidade=D("10"), preco_unitario=3500))
    s.commit()
    return a


def chamadas(http, trecho):
    return [r for r in http.requisicoes if trecho in str(r.url)]


def historicas(http, trecho):
    """Buscas de histórico de preço: nem a cotação atual (5 dias) nem os proventos."""
    return [r for r in chamadas(http, trecho) if r.url.params["range"] != "5d" and "events" not in r.url.params]


def test_atualiza_preco_e_variacao_no_cache(session):
    a = novo_ativo(session)
    http = HttpFalso(ROTAS)
    res = atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(http.cliente()))
    assert "PETR4.SA" in res.atualizados and not res.falhas
    linha = session.scalar(select(CotacaoCache).where(CotacaoCache.simbolo == "PETR4.SA", CotacaoCache.data == AGORA.date()))
    assert linha.fechamento == D("38.12") and linha.fonte == "yahoo" and linha.atualizado_em == AGORA
    c = cotacao_do_ativo(session, a)
    assert (c.preco, c.moeda, c.origem, c.desatualizada) == (D("38.12"), "BRL", "cotacao", False)
    assert c.variacao_dia_pct == (D("38.12") - D("38.6")) / D("38.6") * 100   # contra o pregão anterior
    # câmbio e Ibovespa também ficam no cache
    assert session.scalar(select(CotacaoCache.fechamento).where(
        CotacaoCache.simbolo == "USDBRL=X", CotacaoCache.data == AGORA.date())) == D("5.4321")


def test_cache_de_15_minutos(session):
    a = novo_ativo(session)
    http = HttpFalso(ROTAS)
    y = Yahoo(http.cliente())
    atualizar_cotacoes(session, [a], AGORA, yahoo=y)
    n = len(chamadas(http, "PETR4.SA"))
    atualizar_cotacoes(session, [a], AGORA + timedelta(minutes=14), yahoo=y)
    assert len(chamadas(http, "PETR4.SA")) == n  # dentro dos 15 min: nada novo
    atualizar_cotacoes(session, [a], AGORA + timedelta(minutes=16), yahoo=y)
    assert len(chamadas(http, "PETR4.SA")) == n + 1  # preço atual renovado (histórico já completo)
    atualizar_cotacoes(session, [a], AGORA + timedelta(minutes=17), forcar=True, yahoo=y)
    assert len(chamadas(http, "PETR4.SA")) > n + 1
    assert session.query(CotacaoCache).filter_by(simbolo="PETR4.SA", data=AGORA.date()).count() == 1


def test_historico_mensal_baixado_uma_vez_e_completado_incrementalmente(session):
    a = novo_ativo(session)
    http = HttpFalso(ROTAS)
    y = Yahoo(http.cliente())
    atualizar_cotacoes(session, [a], AGORA, yahoo=y)
    mensais = session.scalars(select(CotacaoCache).where(
        CotacaoCache.simbolo == "PETR4.SA", CotacaoCache.fonte == "yahoo:mensal").order_by(CotacaoCache.data)).all()
    assert [(m.data, m.fechamento) for m in mensais] == [
        (date(2026, 7, 31), D("36.8")), (date(2026, 8, 31), D("37.55")), (date(2026, 9, 30), D("38.4"))]
    assert len(historicas(http, "PETR4.SA")) == 1
    # mais tarde no mesmo mês: nenhuma nova chamada de histórico
    atualizar_cotacoes(session, [a], AGORA + timedelta(hours=1), yahoo=y)
    assert len(historicas(http, "PETR4.SA")) == 1
    # virou o mês: completa só outubro
    atualizar_cotacoes(session, [a], datetime(2026, 11, 3, 9, 0), yahoo=y)
    assert len(historicas(http, "PETR4.SA")) == 2
    novo = session.scalar(select(CotacaoCache).where(
        CotacaoCache.simbolo == "PETR4.SA", CotacaoCache.fonte == "yahoo:mensal", CotacaoCache.data >= date(2026, 10, 1)))
    assert novo.data == date(2026, 10, 5) and novo.fechamento == D("38.12")
    assert session.query(CotacaoCache).filter_by(simbolo="PETR4.SA", fonte="yahoo:mensal").count() == 4


def test_historico_exterior_guarda_moeda_do_ativo(session):
    a = novo_ativo(session, "exterior", "AAPL", "AAPL", "USD")
    atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(HttpFalso(ROTAS).cliente()))
    assert session.scalar(select(CotacaoCache.moeda).where(
        CotacaoCache.simbolo == "AAPL", CotacaoCache.fonte == "yahoo:mensal").limit(1)) == "USD"


def test_historico_de_par_em_dolar_guarda_usd_mesmo_com_ativo_em_reais(session):
    a = novo_ativo(session, "cripto", "AAPL", "AAPL-USD", "BRL")
    atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(HttpFalso({**ROTAS, "AAPL-USD": "yahoo_aapl.json"}).cliente()))
    assert session.scalar(select(CotacaoCache.moeda).where(
        CotacaoCache.simbolo == "AAPL-USD", CotacaoCache.fonte == "yahoo:mensal").limit(1)) == "USD"


def test_erro_de_rede_mantem_cache_e_marca_desatualizada(session):
    a = novo_ativo(session)
    atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(HttpFalso(ROTAS).cliente()))
    quebrado = Yahoo(HttpFalso({"chart": httpx.ConnectError("sem rede")}).cliente())
    res = atualizar_cotacoes(session, [a], AGORA + timedelta(hours=1), yahoo=quebrado)  # sem exceção
    assert res.falhas and a.id in res.desatualizados
    assert ids_desatualizados(session) == {a.id}
    c = cotacao_do_ativo(session, a)
    assert c.preco == D("38.12") and c.desatualizada is True
    # volta a rede: some a marca
    atualizar_cotacoes(session, [a], AGORA + timedelta(hours=2), yahoo=Yahoo(HttpFalso(ROTAS).cliente()))
    assert ids_desatualizados(session) == set()
    assert cotacao_do_ativo(session, a).desatualizada is False


def test_brapi_so_entra_quando_yahoo_falha_para_ativo_b3(session):
    b3 = novo_ativo(session)
    ext = novo_ativo(session, "exterior", "AAPL", "AAPL", "USD")
    yahoo = Yahoo(HttpFalso({"chart": httpx.ConnectError("fora")}).cliente())
    http_b = HttpFalso({"brapi.dev/api/quote/PETR4": "brapi_petr4.json"})
    res = atualizar_cotacoes(session, [b3, ext], AGORA, yahoo=yahoo, brapi=Brapi("tok", http_b.cliente()))
    assert res.atualizados.count("PETR4.SA") == 1 and "AAPL" in res.falhas
    assert [r.url.path for r in http_b.requisicoes] == ["/api/quote/PETR4"]
    linha = session.scalar(select(CotacaoCache).where(CotacaoCache.simbolo == "PETR4.SA", CotacaoCache.data == AGORA.date()))
    assert linha.fonte == "brapi" and linha.fechamento == D("38.5") and linha.variacao_dia_pct == D("1.25")
    assert ids_desatualizados(session) == {ext.id}


def test_brapi_usa_token_do_keyring(session, keyring_falso, monkeypatch):
    keyring_falso[("app-financas", "brapi")] = "tok-kr"
    a = novo_ativo(session)
    http_b = HttpFalso({"brapi.dev": "brapi_petr4.json"})
    import app.services.mercado.brapi as mod
    monkeypatch.setattr(mod, "novo_cliente", http_b.cliente)
    atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(HttpFalso({"chart": httpx.ConnectError("x")}).cliente()))
    assert http_b.requisicoes[0].url.params["token"] == "tok-kr"


def test_sem_token_brapi_yahoo_falho_so_marca_desatualizada(session):
    a = novo_ativo(session)
    res = atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(HttpFalso({"chart": httpx.ConnectError("x")}).cliente()))
    assert res.falhas and ids_desatualizados(session) == {a.id}


def test_preco_manual_tem_precedencia_quando_mais_recente(session):
    a = novo_ativo(session)
    atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(HttpFalso(ROTAS).cliente()))
    a.preco_manual, a.preco_manual_em = 4000, date(2026, 10, 4)  # mais antigo que a cotação de 05/10
    session.commit()
    assert cotacao_do_ativo(session, a).origem == "cotacao"
    a.preco_manual_em = date(2026, 10, 6)
    session.commit()
    c = cotacao_do_ativo(session, a)
    assert (c.origem, c.preco, c.variacao_dia_pct) == ("manual", D("40"), None)


def test_preco_manual_sem_cotacao_e_ativo_sem_nada(session):
    a = novo_ativo(session, simbolo="")
    assert cotacao_do_ativo(session, a) is None
    a.preco_manual, a.preco_manual_em = 12345, date(2026, 10, 1)
    session.commit()
    assert cotacao_do_ativo(session, a).preco == D("123.45")


def test_ativo_sem_simbolo_ou_inativo_nao_consulta(session):
    rf = novo_ativo(session, "renda_fixa", "CDB X", "")
    inativo = novo_ativo(session, "acao", "VALE3", "VALE3.SA")
    inativo.ativo = False
    session.commit()
    http = HttpFalso(ROTAS)
    atualizar_cotacoes(session, [rf, inativo], AGORA, yahoo=Yahoo(http.cliente()))
    # cotação e histórico não; os proventos sim, porque contam na rentabilidade de quando havia posição
    assert not [r for r in chamadas(http, "VALE3") if "events" not in r.url.params]


def test_atualizar_series_cdi_e_ipca_incremental(session):
    novo_ativo(session)
    http = HttpFalso({"sgs.12/dados": "bcb_cdi.json", "sgs.433/dados": "bcb_ipca.json"})
    bcb = Bcb(http.cliente())
    assert atualizar_series(session, date(2026, 10, 5), bcb=bcb) is True
    cdi = session.scalars(select(SerieIndicador).where(SerieIndicador.serie == "cdi").order_by(SerieIndicador.data)).all()
    assert [(c.data, c.valor_pct) for c in cdi] == [
        (date(2026, 10, 1), D("0.040168")), (date(2026, 10, 2), D("0.040168")), (date(2026, 10, 5), D("0.0402"))]
    assert session.query(SerieIndicador).filter_by(serie="ipca").count() == 2
    p = [r.url.params for r in http.requisicoes if "sgs.12" in str(r.url)][0]
    assert p["dataInicial"] == "01/07/2026"  # mês da primeira operação
    atualizar_series(session, date(2026, 10, 6), bcb=bcb)
    p2 = [r.url.params for r in http.requisicoes if "sgs.12" in str(r.url)][1]
    assert p2["dataInicial"] == "06/10/2026"
    assert session.query(SerieIndicador).filter_by(serie="cdi").count() == 3  # sem duplicar (fixture repete datas)


def test_atualizar_series_busca_de_novo_quando_surge_operacao_anterior_a_serie(session):
    a = novo_ativo(session)
    http = HttpFalso({"sgs.12/dados": "bcb_cdi.json", "sgs.433/dados": "bcb_ipca.json"})
    bcb = Bcb(http.cliente())
    atualizar_series(session, date(2026, 10, 5), bcb=bcb)
    session.add(OperacaoInvest(ativo_id=a.id, tipo="compra", data=date(2026, 3, 10), quantidade=D("1"), preco_unitario=3000))
    session.commit()
    atualizar_series(session, date(2026, 10, 5), bcb=bcb)
    p = [r.url.params for r in http.requisicoes if "sgs.12" in str(r.url)][-1]
    assert p["dataInicial"] == "01/03/2026"  # histórico importado: completa desde a operação mais antiga


def test_atualizar_series_falha_sem_excecao(session):
    novo_ativo(session)
    bcb = Bcb(HttpFalso({"bcdata": httpx.ConnectError("x")}).cliente())
    assert atualizar_series(session, date(2026, 10, 5), bcb=bcb) is False


def test_atualizar_series_sem_operacoes_nao_chama_rede(session):
    http = HttpFalso({})
    assert atualizar_series(session, date(2026, 10, 5), bcb=Bcb(http.cliente())) is True
    assert http.requisicoes == []


def test_proventos_entram_no_cache_uma_vez_por_dia(session):
    a = novo_ativo(session)
    divs = {"chart": {"result": [{"meta": {"currency": "BRL", "gmtoffset": -10800}, "events": {"dividends": {
        "1": {"amount": 0.5, "date": 1785589200}}}}], "error": None}}  # 01/08/2026
    http = HttpFalso({"events=div": json.dumps(divs), **ROTAS})
    y = Yahoo(http.cliente())
    atualizar_cotacoes(session, [a], AGORA, yahoo=y)
    linhas = session.scalars(select(ProventoCache)).all()
    assert [(p.simbolo, p.data_ex, p.valor) for p in linhas] == [("PETR4.SA", date(2026, 8, 1), D("0.5"))]
    atualizar_cotacoes(session, [a], AGORA + timedelta(hours=2), yahoo=y)
    assert len(chamadas(http, "events=div")) == 1  # mesmo dia: não busca de novo


def test_renda_fixa_e_cripto_nao_buscam_proventos(session):
    a = novo_ativo(session, classe="cripto", codigo="BTC", simbolo="BTC-USD", moeda="USD")
    http = HttpFalso(ROTAS)
    atualizar_cotacoes(session, [a], AGORA, yahoo=Yahoo(http.cliente()))
    assert chamadas(http, "events=div") == []


def test_proventos_de_papel_ja_vendido_buscados_uma_vez(session):
    ativo_ = novo_ativo(session)
    vendido = novo_ativo(session, classe="fii", codigo="HGLG11", simbolo="HGLG11.SA")
    vendido.ativo = False
    session.commit()
    divs = {"chart": {"result": [{"meta": {"currency": "BRL", "gmtoffset": -10800}, "events": {"dividends": {
        "1": {"amount": 1.1, "date": 1785589200}}}}], "error": None}}  # 01/08/2026
    http = HttpFalso({"events=div": json.dumps(divs), **ROTAS})
    y = Yahoo(http.cliente())
    atualizar_cotacoes(session, [ativo_], AGORA, yahoo=y)  # a atualização só recebe os ativos em carteira
    assert session.scalar(select(ProventoCache).where(ProventoCache.simbolo == "HGLG11.SA")).valor == D("1.1")
    atualizar_cotacoes(session, [ativo_], AGORA + timedelta(days=1), yahoo=y)
    hglg = [r for r in chamadas(http, "events=div") if "HGLG11" in str(r.url)]
    assert len(hglg) == 1  # posição encerrada e já no cache: não busca de novo
