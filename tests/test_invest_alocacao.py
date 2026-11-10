from datetime import date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text

from app.models import Ativo, CotacaoCache, MetaClasse, OperacaoInvest
from app.models.investimento import ALOCACOES, alocacao_padrao
from app.services.invest_carteira import migrar_alocacao, migrar_simbolos_cripto, resumo_carteira, sugerir_aporte
from app.services.invest_operacoes import DadosAtivo, criar_ativo
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta
from tests.test_rotas_investimentos import cadastrar, mercado  # noqa: F401  (fixture)


def test_alocacoes_na_ordem_de_exibicao():
    assert list(ALOCACOES) == ["acoes", "renda_fixa", "cripto"]
    assert ALOCACOES["acoes"] == "Renda variável" and ALOCACOES["renda_fixa"] == "Renda fixa"


@pytest.mark.parametrize("classe,codigo,esperado", [
    ("renda_fixa", "CDB Banco X", "renda_fixa"), ("cripto", "BTC", "cripto"),
    ("acao", "PETR4", "acoes"), ("fii", "HGLG11", "acoes"), ("exterior", "AAPL", "acoes"),
    ("etf", "BOVA11", "acoes"), ("etf", "BRAX11", "acoes"),
    ("etf", "HASH11", "cripto"), ("etf", "hash11.sa", "cripto"), ("etf", "QBTC11", "cripto"),
    ("etf", "IMAB11", "renda_fixa"), ("etf", "IMA-B11.SA", "renda_fixa"), ("etf", "b5p211", "renda_fixa"),
])
def test_alocacao_padrao(classe, codigo, esperado):
    assert alocacao_padrao(classe, codigo) == esperado


def test_criar_ativo_define_alocacao_padrao():
    assert criar_ativo(DadosAtivo(classe="etf", codigo="hash11")).alocacao == "cripto"
    assert criar_ativo(DadosAtivo(classe="acao", codigo="petr4")).alocacao == "acoes"


def test_modelo_sem_alocacao_explicita_recebe_o_padrao(session):
    a = Ativo(classe="etf", codigo="IMAB11")
    session.add(a)
    session.flush()
    assert a.alocacao == "renda_fixa"


def _ativo(s, classe, codigo, **kw):
    a = Ativo(classe=classe, codigo=codigo, simbolo_cotacao=f"{codigo}.SA", **kw)
    s.add(a)
    s.flush()
    return a


def _limpar_alocacao(s):
    s.execute(text("UPDATE ativo SET alocacao = ''"))
    s.commit()
    s.expire_all()


def test_migracao_preenche_alocacao_e_converte_metas(session):
    _ativo(session, "etf", "BOVA11")
    _ativo(session, "etf", "HASH11")
    _ativo(session, "renda_fixa", "CDB")
    session.add_all([MetaClasse(classe="etf", meta_pct=D(65)), MetaClasse(classe="renda_fixa", meta_pct=D(30)),
                     MetaClasse(classe="cripto", meta_pct=D(5))])
    session.commit()
    _limpar_alocacao(session)
    migrar_alocacao(session)
    session.commit()
    assert {a.codigo: a.alocacao for a in session.scalars(select(Ativo))} == {
        "BOVA11": "acoes", "HASH11": "cripto", "CDB": "renda_fixa"}
    assert {m.classe: m.meta_pct for m in session.scalars(select(MetaClasse))} == {
        "acoes": 65, "renda_fixa": 30, "cripto": 5}


def test_migracao_soma_acao_e_etf_e_nao_sobrescreve_alocacao_existente(session):
    a = _ativo(session, "etf", "HASH11")
    a.alocacao = "acoes"  # escolha do usuário: não é refeita
    session.add_all([MetaClasse(classe="acao", meta_pct=D(40)), MetaClasse(classe="etf", meta_pct=D(25)),
                     MetaClasse(classe="fii", meta_pct=D(10)), MetaClasse(classe="exterior", meta_pct=D(5)),
                     MetaClasse(classe="cripto", meta_pct=D(5)), MetaClasse(classe="renda_fixa", meta_pct=D(15))])
    session.commit()
    migrar_alocacao(session)
    session.commit()
    assert session.get(Ativo, a.id).alocacao == "acoes"
    assert {m.classe: m.meta_pct for m in session.scalars(select(MetaClasse))} == {
        "acoes": 80, "renda_fixa": 15, "cripto": 5}


def test_migracao_e_idempotente(session):
    _ativo(session, "acao", "PETR4")
    session.add_all([MetaClasse(classe="etf", meta_pct=D(70)), MetaClasse(classe="cripto", meta_pct=D(30))])
    session.commit()
    _limpar_alocacao(session)
    for _ in range(3):
        migrar_alocacao(session)
        session.commit()
    assert {m.classe: m.meta_pct for m in session.scalars(select(MetaClasse))} == {"acoes": 70, "cripto": 30}
    assert session.scalar(select(Ativo.alocacao)) == "acoes"


def _cotacao(s, simbolo, preco):
    s.add(CotacaoCache(simbolo=simbolo, data=HOJE_TESTE, fechamento=D(preco), moeda="BRL", fonte="yahoo",
                       atualizado_em=datetime(2026, 10, 5, 10)))


def _cenario(s):
    """BOVA11 (ETF de ações) 900 + HASH11 (ETF de cripto) 100; metas por alocação 80/20."""
    bova, hash11 = _ativo(s, "etf", "BOVA11", meta_pct=D(100)), _ativo(s, "etf", "HASH11", meta_pct=D(100))
    for a, preco in ((bova, 9000), (hash11, 1000)):
        s.add(OperacaoInvest(ativo_id=a.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(10),
                             preco_unitario=preco))
    _cotacao(s, "BOVA11.SA", "90")
    _cotacao(s, "HASH11.SA", "10")
    s.add_all([MetaClasse(classe="acoes", meta_pct=D(80)), MetaClasse(classe="cripto", meta_pct=D(20))])
    s.commit()
    return bova, hash11


def test_hash11_agrupa_em_cripto_e_conta_na_meta_de_cripto(session):
    _cenario(session)
    c = resumo_carteira(session, HOJE_TESTE)
    assert [(x.classe, x.saldo, [a.ativo.codigo for a in x.ativos]) for x in c.classes] == [
        ("acoes", 90000, ["BOVA11"]), ("cripto", 10000, ["HASH11"])]
    hash11 = c.classes[1].ativos[0]
    assert hash11.meta_classe_pct == 20 and hash11.ideal_pct == 20  # meta_pct é dentro da alocação
    assert c.classes[0].ativos[0].ideal_pct == 80


def test_rebalanceamento_usa_a_alocacao(session):
    _cenario(session)
    sug = {s.ativo.codigo: s.valor for s in sugerir_aporte(resumo_carteira(session, HOJE_TESTE), 100000)}
    # alvo 80/20 sobre 2.000: ações já em 900 (déficit 700), cripto em 100 (déficit 300); ETF de B3 só em cotas
    # inteiras, cota a cota na alocação com mais falta: 8 × 90 de BOVA11 (a última passa 20 da falta das ações, que
    # era a maior) e 7 × 40 de HASH11, sem sobra
    assert sug == {"BOVA11": 72000, "HASH11": 28000}


def test_mudar_alocacao_pela_tela_move_o_ativo_de_bloco(client, db, mercado):  # noqa: F811
    nova_conta(db)
    db.commit()
    cadastrar(client, classe="etf", codigo="bova11")
    db.add(MetaClasse(classe="acoes", meta_pct=D(100)))
    db.commit()
    antes = client.get("/investimentos").text
    assert 'id="bloco-acoes"' in antes and 'id="bloco-cripto"' not in antes
    r = client.post("/investimentos/ativos/1/alocacao", data={"alocacao": "cripto"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Ativo, 1).alocacao == "cripto"
    depois = client.get("/investimentos").text
    assert 'id="bloco-cripto"' in depois
    assert client.post("/investimentos/ativos/1/alocacao", data={"alocacao": "etf"}).status_code == 422
    assert client.post("/investimentos/ativos/99/alocacao", data={"alocacao": "cripto"}).status_code == 404
    assert 'name="alocacao"' in client.get("/investimentos/ativos/1").text


def test_comprar_por_alocacao(session):
    _cenario(session)
    c = {x.ativo.codigo: x for x in resumo_carteira(session, HOJE_TESTE).ativos}
    assert not c["BOVA11"].comprar  # 90% da carteira contra 80% de ideal
    assert c["HASH11"].comprar      # 10% contra 20%


def test_ideal_e_meta_do_ativo_vezes_meta_da_alocacao(session):
    a, b = _ativo(session, "acao", "AAAA3", meta_pct=D(60)), _ativo(session, "fii", "BBBB11", meta_pct=D(40))
    session.add_all([MetaClasse(classe="acoes", meta_pct=D(50)), MetaClasse(classe="renda_fixa", meta_pct=D(50))])
    session.commit()
    for x in (a, b):
        session.add(OperacaoInvest(ativo_id=x.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(1),
                                   preco_unitario=1000))
        _cotacao(session, f"{x.codigo}.SA", "10")
    session.commit()
    c = {x.ativo.codigo: x for x in resumo_carteira(session, HOJE_TESTE).ativos}
    assert (c["AAAA3"].ideal_classe_pct, c["AAAA3"].ideal_pct) == (60, 30)
    assert (c["BBBB11"].ideal_pct) == 20


def test_soma_dos_ativos_por_alocacao_deve_dar_100(client, db, mercado):  # noqa: F811
    nova_conta(db)
    db.commit()
    cadastrar(client, classe="etf", codigo="bova11")
    cadastrar(client, classe="etf", codigo="hash11")
    base = {"classe_acoes": "70", "classe_cripto": "30"}
    r = client.post("/investimentos/metas", data={**base, "ativo_1": "50", "ativo_2": "100"})
    assert r.status_code == 422 and "Renda variável" in r.text and "100%" in r.text
    db.expire_all()
    r = client.post("/investimentos/metas", data={**base, "ativo_1": "100", "ativo_2": "100"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert (db.get(Ativo, 1).meta_pct, db.get(Ativo, 2).meta_pct) == (100, 100)


def test_metas_editadas_nos_blocos_e_soma_100(client, db, mercado):  # noqa: F811
    nova_conta(db)
    db.commit()
    cadastrar(client, classe="etf", codigo="hash11")
    texto = client.get("/investimentos").text
    assert 'name="classe_cripto"' in texto and 'name="ativo_1"' in texto and 'hx-post="/investimentos/metas"' in texto
    assert 'name="classe_etf"' not in texto
    r = client.post("/investimentos/metas", data={"classe_acoes": "60", "classe_cripto": "30", "ativo_1": "100"})
    assert r.status_code == 422 and "100%" in r.text
    r = client.post("/investimentos/metas", data={"classe_acoes": "70", "classe_cripto": "30", "ativo_1": "100"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert {m.classe: m.meta_pct for m in db.scalars(select(MetaClasse))} == {"acoes": 70, "renda_fixa": 0, "cripto": 30}


def test_migracao_troca_par_cripto_em_reais_pelo_em_dolar(session):
    btc = _ativo(session, "cripto", "BTC")
    btc.simbolo_cotacao = "BTC-BRL"  # o Yahoo deixou de cotar os pares em reais
    eth = _ativo(session, "cripto", "ETH")
    eth.simbolo_cotacao = "ETH-EUR"  # escolha do usuário: não é mexida
    session.commit()
    migrar_simbolos_cripto(session)
    migrar_simbolos_cripto(session)
    assert (btc.simbolo_cotacao, eth.simbolo_cotacao) == ("BTC-USD", "ETH-EUR")
