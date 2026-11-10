from datetime import date, datetime
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Ativo, CotacaoCache, MetaClasse, OperacaoInvest
from app.services.mercado.bcb import Bcb
from app.services.mercado.tesouro import Tesouro
from app.services.mercado.yahoo import Yahoo
from app.web.deps import Provedores, get_agora, get_provedores
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta
from tests.test_mercado_provedores import HttpFalso, sem_rede_real  # noqa: F401  (bloqueia a rede real)

AGORA = datetime(2026, 10, 5, 10, 0)
ROTAS_OK = {"PETR4.SA": "yahoo_petr4.json", "VALE3.SA": "yahoo_petr4.json", "AAPL": "yahoo_aapl.json",
            "USDBRL%3DX": "yahoo_usdbrl.json", "%5EBVSP": "yahoo_bvsp.json", "chart/VT": "yahoo_vt.json",
            "sgs.12/dados": "bcb_cdi.json", "sgs.433/dados": "bcb_ipca.json",
            "PrecoTaxaTesouroDireto": (
                "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;"
                "PU Venda Manha;PU Base Manha\n"
                "Tesouro IPCA+;15/08/2050;21/09/2026;7,17;7,29;914,28;889,89;889,89\n"
                "Tesouro IPCA+;15/08/2050;01/10/2026;7,06;7,18;941,48;916,33;916,33\n"
                "Tesouro Selic;01/03/2029;01/10/2026;0,05;0,06;20046,46;20031,46;20031,46\n"
                "Tesouro IPCA+ com Juros Semestrais;15/08/2050;01/10/2026;7,19;7,31;4179,74;4121,42;4121,42\n")}


@pytest.fixture
def mercado(app_teste):
    """Provedores falsos; o teste escolhe as rotas (`mercado.http = HttpFalso(...)`)."""
    class Ctx:
        http = HttpFalso(ROTAS_OK)

    ctx = Ctx()
    app_teste.dependency_overrides[get_agora] = lambda: AGORA
    app_teste.dependency_overrides[get_provedores] = lambda: Provedores(
        yahoo=Yahoo(ctx.http.cliente()), bcb=Bcb(ctx.http.cliente()), tesouro=Tesouro(ctx.http.cliente()))
    return ctx


def op_form(**kw):
    base = {"ativo_id": "novo", "classe": "acao", "codigo": "petr4", "tipo": "compra", "data": "2026-08-03",
            "quantidade": "100", "preco": "30,00", "taxas": "", "cambio": ""}
    base.update(kw)
    return base


def cadastrar(client, **kw):
    r = client.post("/investimentos/operacoes", data=op_form(**kw))
    assert r.status_code == 200, r.text
    return r


def test_menu_tem_investimentos(client, db):
    nova_conta(db)
    db.commit()
    texto = client.get("/investimentos").text
    assert 'href="/investimentos' in texto and "trending-up" in texto


def test_pagina_vazia(client, db, mercado):
    nova_conta(db)
    db.commit()
    resposta = client.get("/investimentos")
    assert resposta.status_code == 200 and "Nenhum investimento cadastrado" in resposta.text
    assert mercado.http.requisicoes == []  # a página nunca vai à rede


def test_cria_ativo_novo_com_simbolo_sugerido(client, db, mercado):
    nova_conta(db)
    db.commit()
    r = cadastrar(client)
    assert r.headers["HX-Refresh"] == "true"
    a = db.scalars(select(Ativo)).one()
    assert (a.classe, a.codigo, a.simbolo_cotacao, a.moeda) == ("acao", "PETR4", "PETR4.SA", "BRL")
    op = db.scalars(select(OperacaoInvest)).one()
    assert (op.tipo, op.quantidade, op.preco_unitario, op.data) == ("compra", D("100"), 3000, date(2026, 8, 3))


def test_cripto_quantidade_fracionada_e_simbolo(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, classe="cripto", codigo="btc", quantidade="0,00123456", preco="350000,00")
    a = db.scalars(select(Ativo)).one()
    assert a.simbolo_cotacao == "BTC-USD"
    assert db.scalars(select(OperacaoInvest)).one().quantidade == D("0.00123456")


def test_exterior_exige_cambio_e_usa_dolar(client, db, mercado):
    nova_conta(db)
    db.commit()
    r = client.post("/investimentos/operacoes", data=op_form(classe="exterior", codigo="aapl", preco="150,00"))
    assert r.status_code == 422 and "câmbio" in r.text
    cadastrar(client, classe="exterior", codigo="aapl", preco="150,00", cambio="5,10")
    a = db.scalars(select(Ativo)).one()
    assert a.moeda == "USD" and a.simbolo_cotacao == "AAPL"
    op = db.scalars(select(OperacaoInvest)).one()
    assert op.cambio == D("5.1") and op.preco_unitario == 15000


def test_modal_preenche_cambio_com_a_cotacao_do_dia(client, db):
    nova_conta(db)
    db.add(CotacaoCache(simbolo="USDBRL=X", data=HOJE_TESTE, fechamento=D("5.4321"), moeda="BRL", fonte="yahoo",
                        atualizado_em=AGORA))
    db.commit()
    assert 'value="5,4321"' in client.get("/investimentos/operacoes/nova").text


def test_renda_fixa_aplicacao_com_titulo_novo(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, classe="renda_fixa", codigo="CDB Banco X", tipo="aplicacao", preco="5.000,00",
              indexador="cdi", taxa="110", vencimento="2028-01-10", quantidade="")
    a = db.scalars(select(Ativo)).one()
    assert (a.indexador, a.taxa, a.vencimento, a.simbolo_cotacao) == ("cdi", D("110"), date(2028, 1, 10), "")
    op = db.scalars(select(OperacaoInvest)).one()
    assert (op.quantidade, op.preco_unitario) == (D(1), 500000)


def test_renda_fixa_sem_taxa_e_tipo_invalido(client, db, mercado):
    nova_conta(db)
    db.commit()
    r = client.post("/investimentos/operacoes", data=op_form(classe="renda_fixa", codigo="CDB", tipo="aplicacao",
                                                           preco="100,00", indexador="cdi", taxa=""))
    assert r.status_code == 422 and "taxa" in r.text
    r = client.post("/investimentos/operacoes", data=op_form(tipo="aplicacao"))   # aplicação não existe em ações
    assert r.status_code == 422 and "tipo de operação inválido" in r.text
    assert db.scalars(select(OperacaoInvest)).all() == []


def test_venda_acima_da_posicao_e_recusada(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    r = client.post("/investimentos/operacoes", data=op_form(ativo_id="1", tipo="venda", quantidade="101",
                                                           data="2026-09-01"))
    assert r.status_code == 422 and "acima da posição" in r.text
    assert len(db.scalars(select(OperacaoInvest)).all()) == 1


def test_salvar_e_criar_nova_mantem_modal(client, db, mercado):
    nova_conta(db)
    db.commit()
    r = client.post("/investimentos/operacoes", data={**op_form(), "continuar": "1"})
    assert r.status_code == 200 and "Operação salva" in r.text and "HX-Refresh" not in r.headers
    assert db.scalars(select(Ativo)).one().codigo == "PETR4"


def test_ativo_duplicado_na_mesma_classe(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    r = client.post("/investimentos/operacoes", data=op_form())
    assert r.status_code == 422 and "já está cadastrado" in r.text


def test_editar_e_excluir_operacao(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    cadastrar(client, ativo_id="1", quantidade="50", preco="31,00", data="2026-09-01")
    assert "Editar operação" in client.get("/investimentos/operacoes/2/editar").text
    r = client.post("/investimentos/operacoes/2", data=op_form(ativo_id="1", quantidade="40", preco="32,50",
                                                              data="2026-09-02"))
    assert r.headers["HX-Refresh"] == "true"
    db.expire_all()
    op = db.get(OperacaoInvest, 2)
    assert (op.quantidade, op.preco_unitario, op.data) == (D("40"), 3250, date(2026, 9, 2))
    # excluir a compra inicial deixaria uma venda sem posição? aqui só há compras: pode excluir
    assert client.post("/investimentos/operacoes/2/excluir").status_code == 200
    db.expire_all()
    assert db.get(OperacaoInvest, 2) is None


def test_excluir_compra_que_sustenta_venda_e_recusado(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    client.post("/investimentos/operacoes", data=op_form(ativo_id="1", tipo="venda", quantidade="40",
                                                       data="2026-09-01"))
    assert client.post("/investimentos/operacoes/1/excluir").status_code == 409
    db.expire_all()
    assert db.get(OperacaoInvest, 1) is not None


def test_excluir_a_ultima_operacao_apaga_o_ativo(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    cadastrar(client, codigo="vale3", preco="60,00")
    client.post("/investimentos/metas", data={"classe_acoes": "100", "ativo_1": "50", "ativo_2": "50"})
    r = client.post("/investimentos/operacoes/1/excluir",
                    headers={"HX-Request": "true", "HX-Current-URL": "http://x/investimentos/ativos/1"})
    assert r.headers.get("HX-Redirect") == "/investimentos"
    db.expire_all()
    assert db.get(Ativo, 1) is None and db.get(Ativo, 2) is not None
    assert "PETR4" not in client.get("/investimentos").text


# ---------- cotações: injeção, cache e falha ----------

def test_atualizar_cotacoes_preenche_precos(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    r = client.post("/investimentos/atualizar")
    assert r.headers["HX-Refresh"] == "true"
    texto = client.get("/investimentos").text
    assert "R$ 38,12" in texto and "R$ 3.812,00" in texto    # cotação e saldo de 100 ações
    assert "cotação desatualizada" not in texto


def test_pagina_renderiza_do_cache_quando_provedor_falha(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    client.post("/investimentos/atualizar")
    mercado.http.rotas = {"query1": httpx.ConnectError("sem rede"), "bcdata": httpx.ConnectError("sem rede")}
    r = client.post("/investimentos/atualizar")
    assert r.status_code == 200
    resposta = client.get("/investimentos")
    assert resposta.status_code == 200
    assert "cotação desatualizada" in resposta.text and "R$ 3.812,00" in resposta.text   # último valor do cache


def test_atualizar_sem_nenhum_cache_nao_quebra(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    mercado.http.rotas = {"query1": httpx.ConnectError("sem rede")}
    assert client.post("/investimentos/atualizar").status_code == 200
    texto = client.get("/investimentos").text
    assert "sem cotação" in texto and "R$ 3.000,00" in texto     # entra pelo custo


def test_pagina_com_historico_traz_dados_do_grafico(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, data="2026-07-15")
    client.post("/investimentos/atualizar")
    texto = client.get("/investimentos").text
    assert "grafico-evolucao" in texto and '"cdi"' in texto and '"ibov"' in texto
    assert "Evolução do patrimônio" in texto and '"ganho"' in texto


# ---------- detalhe, preço manual, metas, rebalanceamento ----------

def test_detalhe_do_ativo_e_preco_manual(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    texto = client.get("/investimentos/ativos/1").text
    assert "PETR4" in texto and "Operações" in texto and "03/08/2026" in texto
    r = client.post("/investimentos/ativos/1/preco-manual", data={"preco": "45,00"}, follow_redirects=False)
    assert r.status_code == 303
    a = db.get(Ativo, 1)
    db.refresh(a)
    assert (a.preco_manual, a.preco_manual_em) == (4500, HOJE_TESTE)
    pagina = client.get("/investimentos").text
    assert "preço manual" in pagina and "R$ 4.500,00" in pagina
    client.post("/investimentos/ativos/1/preco-manual", data={"preco": ""})
    db.refresh(a)
    assert a.preco_manual is None
    assert client.get("/investimentos/ativos/99").status_code == 404


def test_metas_validam_somas_e_salvam(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    cadastrar(client, codigo="vale3", preco="60,00")
    dados = {"classe_acoes": "100", "ativo_1": "60", "ativo_2": "40"}
    r = client.get("/investimentos/metas", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/investimentos#meus-ativos"
    r = client.post("/investimentos/metas", data={**dados, "classe_cripto": "10"})
    assert r.status_code == 422 and "soma das metas das alocações" in r.text and "110" in r.text
    r = client.post("/investimentos/metas", data={**dados, "ativo_2": "30"})
    assert r.status_code == 422 and "ativos de Renda variável" in r.text
    r = client.post("/investimentos/metas", data=dados, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(MetaClasse, "acoes").meta_pct == D("100")
    assert (db.get(Ativo, 1).meta_pct, db.get(Ativo, 2).meta_pct) == (D("60"), D("40"))
    assert 'name="ativo_1" data-meta-ativo="acoes" value="60"' in client.get("/investimentos").text


def test_metas_pela_tela_recarregam_e_campo_ausente_mantem_valor(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    cadastrar(client, codigo="vale3", preco="60,00")
    r = client.post("/investimentos/metas", data={"classe_acoes": "100", "ativo_1": "60", "ativo_2": "40"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"
    r = client.post("/investimentos/metas", data={"classe_acoes": "100", "ativo_1": "60"},  # VALE3 fora da tela
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    db.expire_all()
    assert (db.get(Ativo, 1).meta_pct, db.get(Ativo, 2).meta_pct) == (D("60"), D("40"))
    r = client.post("/investimentos/metas", data={"classe_acoes": "100", "ativo_1": "70"}, headers={"HX-Request": "true"})
    assert r.status_code == 422 and 'role="alert"' in r.text and "110" in r.text


def test_lancamentos_lista_e_filtra_inclusive_ativo_encerrado(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)                                                       # PETR4 compra 100 x 30
    cadastrar(client, classe="cripto", codigo="btc", quantidade="0,5", preco="100,00")
    client.post("/investimentos/operacoes", data=op_form(ativo_id="1", tipo="venda", quantidade="100", preco="35,00"))
    db.get(Ativo, 1).ativo = False
    db.commit()
    texto = client.get("/investimentos/lancamentos").text
    assert "PETR4" in texto and "BTC" in texto and "encerrado" in texto and "Venda" in texto
    assert "R$ 3.500,00" in texto and "R$ 3.050,00" in texto              # vendas; compras 3.000 + 50
    so_cripto = client.get("/investimentos/lancamentos?alocacao=cripto").text
    assert "/investimentos/ativos/1\"" not in so_cripto and "/investimentos/ativos/2\"" in so_cripto
    so_vendas = client.get("/investimentos/lancamentos?tipo=saida").text.split("<tbody")[1]
    assert "Venda" in so_vendas and "Compra" not in so_vendas
    ano = client.get(f"/investimentos/lancamentos?ano={HOJE_TESTE.year + 1}").text
    assert "Nenhum lançamento com esses filtros" in ano


def test_botao_global_tem_investimento(client, db):
    nova_conta(db)
    db.commit()
    texto = client.get("/").text
    assert 'hx-get="/investimentos/operacoes/nova"' in texto and "Investimento" in texto


def test_rebalancear_sugere_so_abaixo_da_meta(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, preco="30,00")                                   # PETR4: 100 x 30
    cadastrar(client, codigo="vale3", quantidade="10", preco="60,00")  # VALE3: 10 x 60
    client.post("/investimentos/metas", data={"classe_acoes": "100", "ativo_1": "50", "ativo_2": "50"})
    for simbolo, preco in (("PETR4.SA", "40"), ("VALE3.SA", "50")):
        db.add(CotacaoCache(simbolo=simbolo, data=HOJE_TESTE, fechamento=D(preco), moeda="BRL", fonte="yahoo",
                            atualizado_em=AGORA))
    db.commit()
    # PETR4 = 4.000 (89%), VALE3 = 500: só VALE3 está abaixo da meta de 50%
    texto = client.get("/investimentos/rebalancear?aporte=1.000,00").text
    assert "VALE3" in texto and "R$ 1.000,00" in texto
    linhas = texto.split("<tbody")[1].split("</tbody>")[0]
    assert "PETR4" not in linhas and "20" in linhas        # 1.000 / 50 = 20 cotas
    assert "Sobra do aporte" in texto


def test_rebalancear_efetivar_abre_o_modal_preenchido(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, preco="30,00")
    cadastrar(client, codigo="vale3", quantidade="10", preco="60,00")
    client.post("/investimentos/metas", data={"classe_acoes": "100", "ativo_1": "50", "ativo_2": "50"})
    for simbolo, preco in (("PETR4.SA", "40"), ("VALE3.SA", "50")):
        db.add(CotacaoCache(simbolo=simbolo, data=HOJE_TESTE, fechamento=D(preco), moeda="BRL", fonte="yahoo",
                            atualizado_em=AGORA))
    db.commit()
    texto = client.get("/investimentos/rebalancear?aporte=1.000,00").text
    url = "/investimentos/operacoes/nova?ativo_id=2&amp;tipo=compra&amp;quantidade=20&amp;preco=50%2C00"
    assert f'hx-get="{url}"' in texto and "Efetivar" in texto
    modal = client.get(url.replace("&amp;", "&")).text
    modal = modal.replace("&#34;", '"')
    assert 'value="20"' in modal and 'preco: "50,00"' in modal and '"2"' in modal


def test_rebalancear_aporte_invalido(client, db):
    nova_conta(db)
    db.commit()
    r = client.get("/investimentos/rebalancear?aporte=abc")
    assert r.status_code == 422 and "maior que zero" in r.text
    assert client.get("/investimentos/rebalancear").status_code == 200


# ---------- aba Resumo: cards, filtros, blocos por alocação ----------

def carteira_com_metas(client, db):
    nova_conta(db)
    db.commit()
    cadastrar(client, preco="30,00")                                   # PETR4: 100 x 30
    cadastrar(client, codigo="vale3", quantidade="10", preco="60,00")  # VALE3: 10 x 60
    r = client.post("/investimentos/metas", data={"classe_acoes": "100", "ativo_1": "20", "ativo_2": "80"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    for simbolo, preco in (("PETR4.SA", "40"), ("VALE3.SA", "50")):
        db.add(CotacaoCache(simbolo=simbolo, data=HOJE_TESTE, fechamento=D(preco), moeda="BRL", fonte="yahoo",
                            atualizado_em=AGORA))
    db.commit()


def test_pagina_traz_cards_blocos_colunas_e_comprar(client, db, mercado):
    carteira_com_metas(client, db)
    texto = client.get("/investimentos").text
    for rotulo in ("Patrimônio total", "Valor investido", "Ganho de capital", "Rentabilidade", "12M",
                   "Meus ativos", "Editar colunas", "% Ideal", "Comprar?", "Preço atual", "Variação"):
        assert rotulo in texto, rotulo
    assert "R$ 4.500,00" in texto and "R$ 3.600,00" in texto and "R$ 900,00" in texto   # patrimônio, investido, ganho
    linhas = texto.split("<tbody")[1].split("</tbody>")[0]
    vale, petr = linhas.split("PETR4", 1)               # a maior meta primeiro: VALE3 (80) antes de PETR4 (20)
    assert "VALE3" in vale and "80,0%" in vale and ">Sim<" in vale    # VALE3: 11,1% contra 80%
    assert "20,0%" in petr and ">Não<" in petr          # PETR4: 88,9% da carteira contra 20% de ideal
    assert "meta 100,0%" in texto                       # cabeçalho do bloco: alocação contra a meta da classe


def test_pagina_aceita_filtros_de_periodo_e_classe(client, db, mercado):
    carteira_com_metas(client, db)
    texto = client.get("/investimentos?periodo=24&classe=acoes&rosca=acoes").text
    assert 'value="24" selected' in texto and "PETR4" in texto.split("grafico-alocacao")[1]
    assert client.get("/investimentos?periodo=xx&classe=yy&rosca=zz").status_code == 200



def test_grafico_de_rentabilidade_tem_periodo_proprio(client, db, mercado):
    carteira_com_metas(client, db)
    texto = client.get("/investimentos?periodo=24&periodo_rent=12").text
    rent = texto.split('id="filtro-periodo-rent"')[1].split("</select>")[0]
    assert 'value="12" selected' in rent
    evolucao = texto.split('id="filtro-periodo"')[1].split("</select>")[0]
    assert 'value="24" selected' in evolucao
    assert '<input type="hidden" name="periodo_rent" value="12">' in texto  # os outros filtros mantêm o período
    assert client.get("/investimentos?periodo_rent=xx").status_code == 200

def test_rebalancear_mostra_o_ideal_pela_meta(client, db, mercado):
    carteira_com_metas(client, db)
    texto = client.get("/investimentos/rebalancear?aporte=1.000,00").text
    linhas = texto.split("<tbody")[1].split("</tbody>")[0]
    assert "VALE3" in linhas and "80,0%" in linhas and "PETR4" not in linhas


# ---------- Configurações › Investimentos ----------

def test_aba_investimentos_guarda_token_sem_exibir(client, db, keyring_falso):
    nova_conta(db)
    db.commit()
    assert "Nenhum token guardado" in client.get("/configuracoes?aba=investimentos").text
    r = client.post("/configuracoes/investimentos/token", data={"token": "abc123XYZ789"}, follow_redirects=False)
    assert r.status_code == 303 and keyring_falso[("app-financas", "brapi")] == "abc123XYZ789"
    texto = client.get("/configuracoes?aba=investimentos").text
    assert "Z789" in texto and "abc123XYZ789" not in texto
    client.post("/configuracoes/investimentos/token/remover")
    assert ("app-financas", "brapi") not in keyring_falso


def test_atualizar_historico_pela_configuracao(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, data="2026-07-15")
    r = client.post("/configuracoes/investimentos/historico")
    assert r.status_code == 200 and "Histórico de cotações atualizado" in r.text
    assert db.scalars(select(CotacaoCache).where(CotacaoCache.fonte == "yahoo:mensal")).first() is not None


def test_atualizar_historico_com_falha_avisa_sem_quebrar(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    mercado.http.rotas = {"query1": httpx.ConnectError("x"), "bcdata": httpx.ConnectError("x")}
    r = client.post("/configuracoes/investimentos/historico")
    assert r.status_code == 502 and "Não foi possível atualizar" in r.text


def test_cadastrar_ativo_sem_compra(client, db, mercado):
    nova_conta(db)
    db.commit()
    r = client.post("/investimentos/operacoes", data=op_form(so_cadastrar="1", data="", quantidade="", preco=""))
    assert r.status_code == 200, r.text
    a = db.scalar(select(Ativo).where(Ativo.codigo == "PETR4"))
    assert a is not None and db.scalars(select(OperacaoInvest)).all() == []
    assert 'name="ativo_%d"' % a.id in client.get("/investimentos").text


def test_cadastro_escolhe_a_alocacao_do_ativo(client, db, mercado):
    nova_conta(db)
    db.commit()
    r = client.post("/investimentos/operacoes", data=op_form(so_cadastrar="1", alocacao="cripto"))
    assert r.status_code == 200, r.text
    assert db.scalar(select(Ativo).where(Ativo.codigo == "PETR4")).alocacao == "cripto"
    client.post("/investimentos/operacoes", data=op_form(codigo="ITSA4", so_cadastrar="1", alocacao="nada"))
    assert db.scalar(select(Ativo).where(Ativo.codigo == "ITSA4")).alocacao == "acoes"  # inválida: a padrão
    modal = client.get("/investimentos/operacoes/nova").text
    assert 'name="alocacao"' in modal and ">Alternativo</option>" in modal


def test_so_cadastrar_com_ativo_existente_nao_cria_operacao(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    client.post("/investimentos/operacoes", data=op_form(ativo_id="1", so_cadastrar="1", quantidade="5"))
    assert len(db.scalars(select(OperacaoInvest)).all()) == 1


def test_rendimento_pela_rota_soma_quantidade_sem_custo_e_sem_conta(client, db, mercado):
    conta = nova_conta(db)
    db.commit()
    cadastrar(client, classe="cripto", codigo="USDC", simbolo="USDC-USD", quantidade="10", preco="5,00")
    a = db.scalar(select(Ativo).where(Ativo.codigo == "USDC"))
    # informa o saldo total da corretora (10,25); guarda o que passou da posição (0,25)
    r = client.post("/investimentos/operacoes", data=op_form(
        ativo_id=str(a.id), tipo="rendimento", quantidade="10,25", preco="9,99", taxas="1,00", conta_id=str(conta.id)))
    assert r.status_code == 200, r.text
    rend = db.scalar(select(OperacaoInvest).where(OperacaoInvest.tipo == "rendimento"))
    assert rend.quantidade == D("0.25") and rend.preco_unitario == 0 and rend.taxas == 0 and rend.lancamento_id is None
    lista = client.get("/investimentos/lancamentos?tipo=rendimento").text.split("<tbody")[1]
    assert "Rendimento" in lista and "sem custo" in lista
    # a edição mostra o saldo total e recalcula a diferença
    assert 'value="10,25"' in client.get(f"/investimentos/operacoes/{rend.id}/editar").text
    r = client.post(f"/investimentos/operacoes/{rend.id}", data=op_form(
        ativo_id=str(a.id), tipo="rendimento", data="2026-08-04", quantidade="10,4"))
    assert r.status_code == 200, r.text
    db.refresh(rend)
    assert rend.quantidade == D("0.4")
    # saldo que não passa da posição: recusado
    r = client.post("/investimentos/operacoes", data=op_form(ativo_id=str(a.id), tipo="rendimento", data="2026-08-05", quantidade="10,4"))
    assert r.status_code == 422 and "precisa ser maior que a posição" in r.text


def test_novo_ativo_com_codigo_de_um_encerrado_reativa_o_mesmo(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, classe="etf", codigo="LFTB11", simbolo="LFTB11.SA")
    a = db.scalar(select(Ativo).where(Ativo.codigo == "LFTB11"))
    a.ativo = False  # encerrado: some da lista do formulário
    db.commit()
    r = client.post("/investimentos/operacoes", data=op_form(classe="etf", codigo="lftb11", so_cadastrar="1",
                                                             alocacao="renda_fixa"))
    assert r.status_code == 200, r.text
    db.refresh(a)
    assert a.ativo and a.alocacao == "renda_fixa"
    assert db.scalar(select(func.count()).select_from(Ativo).where(Ativo.codigo == "LFTB11")) == 1
    # já ativo: não duplica e diz para escolher na lista
    r = client.post("/investimentos/operacoes", data=op_form(classe="etf", codigo="LFTB11"))
    assert r.status_code == 422 and "escolha-o na lista" in r.text


def test_colunas_de_alocacao_na_base_da_classe(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    html = client.get("/investimentos").text
    assert ">Na classe<" in html and ">Falta para a meta<" in html and ">Meta<" in html
    assert "da carteira inteira · ideal na carteira" in html  # % da carteira no title do "Na classe"
