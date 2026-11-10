"""Telas do Open Finance: aba em Configurações, sincronização por conta em Importar e selo no dashboard."""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Conta, Lancamento
from app.services.openfinance.pluggy import KEYRING_ID, KEYRING_SEGREDO, KEYRING_SERVICO
from app.services.openfinance.sincronizar import adicionar_item, registrar_sincronizacao
from app.web.templating import templates
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao
from tests.test_openfinance_sincronizar import cliente_fake


@pytest.fixture(autouse=True)
def _app_configurado(db, monkeypatch):
    """Sem nenhuma conta, o app redireciona para o setup: cria uma antes de cada teste.
    As telas do Open Finance ficam desligadas por padrão; aqui são ligadas para serem testadas."""
    monkeypatch.setitem(templates.env.globals, "open_finance_ativo", True)
    nova_conta(db, "Conta Base")
    db.commit()


def test_open_finance_fica_escondido_por_padrao(client, db, keyring_falso, app_teste, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "open_finance_ativo", False)
    _configurar(db, keyring_falso, app_teste)
    registrar_sincronizacao(db, "acc-x", HOJE_TESTE)
    db.commit()
    assert "aba=openfinance" not in client.get("/configuracoes").text
    assert "Sincronizar Open Finance" not in client.get("/importar").text
    assert "Última sincronização" not in client.get("/").text


def _configurar(db, keyring_falso, app_teste, **kw):
    keyring_falso[(KEYRING_SERVICO, KEYRING_ID)] = "id-ficticio-9876"
    keyring_falso[(KEYRING_SERVICO, KEYRING_SEGREDO)] = "segredo-ficticio"
    adicionar_item(db, "item-1")
    db.commit()
    app_teste.state.cliente_pluggy = cliente_fake(**kw)


def _token_grupo(resposta) -> str:
    assert resposta.status_code == 303, resposta.text
    return resposta.headers["location"].rsplit("/", 1)[1]


# ---------- Configurações › Open Finance ----------

def test_aba_mostra_passos_e_guarda_chaves_sem_exibir_o_segredo(client, keyring_falso):
    html = client.get("/configuracoes?aba=openfinance").text
    assert "meu.pluggy.ai" in html and "dashboard.pluggy.ai" in html and "Nenhuma chave guardada." in html
    r = client.post("/configuracoes/openfinance/chaves", data={"client_id": "00000000-0000-4000-8000-00000000abcd", "client_secret": "super-segredo"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert keyring_falso[(KEYRING_SERVICO, KEYRING_SEGREDO)] == "super-segredo"
    html = client.get("/configuracoes?aba=openfinance").text
    assert "abcd" in html and "super-segredo" not in html and "00000000-0000-4000-8000-00000000abcd" not in html
    client.post("/configuracoes/openfinance/chaves/remover")
    assert (KEYRING_SERVICO, KEYRING_ID) not in keyring_falso


def test_chaves_vazias_dao_erro(client):
    r = client.post("/configuracoes/openfinance/chaves", data={"client_id": " ", "client_secret": "x"})
    assert r.status_code == 400 and "informe o Client ID" in r.text


def test_adicionar_e_remover_item(client):
    client.post("/configuracoes/openfinance/itens", data={"item_id": "item-xyz"})
    assert "item-xyz" in client.get("/configuracoes?aba=openfinance").text
    client.post("/configuracoes/openfinance/itens/remover", data={"item_id": "item-xyz"})
    assert "item-xyz" not in client.get("/configuracoes?aba=openfinance").text
    assert client.post("/configuracoes/openfinance/itens", data={"item_id": ""}).status_code == 400


def test_testar_conexao_mostra_status_dos_itens(client, db, app_teste, keyring_falso):
    _configurar(db, keyring_falso, app_teste)
    r = client.post("/configuracoes/openfinance/testar")
    assert r.status_code == 200 and "Conexão funcionando: 1 de 1" in r.text
    assert "Banco Ficticio" in r.text and "Conexão em dia" in r.text


def test_testar_sem_chaves_ou_itens(client, db, app_teste):
    assert "Guarde o Client ID" in client.post("/configuracoes/openfinance/testar").text
    app_teste.state.cliente_pluggy = cliente_fake()
    assert "Adicione ao menos um ID" in client.post("/configuracoes/openfinance/testar").text


def test_testar_com_chaves_recusadas_mostra_erro(client, db, app_teste, keyring_falso):
    import httpx

    from app.services.openfinance.pluggy import ClientePluggy

    _configurar(db, keyring_falso, app_teste)
    app_teste.state.cliente_pluggy = ClientePluggy(
        "i", "s", httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={}))))
    r = client.post("/configuracoes/openfinance/testar")
    assert r.status_code == 502 and "recusou as chaves" in r.text


# ---------- Importar › Sincronizar ----------

def test_botao_so_aparece_com_chaves_e_itens(client, db, app_teste, keyring_falso):
    assert "Sincronizar Open Finance" not in client.get("/importar").text
    _configurar(db, keyring_falso, app_teste)
    assert "Sincronizar Open Finance" in client.get("/importar").text


def test_sincronizar_sem_configuracao(client):
    r = client.post("/importar/sincronizar")
    assert r.status_code == 422 and "Configure as chaves" in r.text


def test_fluxo_vincular_confirmar_uma_e_todas_sem_duplicar(client, db, app_teste, keyring_falso):
    corrente = nova_conta(db, "Corrente")
    cartao = novo_cartao(db, "Cartão", pagadora=corrente, fechamento=8, vencimento=15)
    _configurar(db, keyring_falso, app_teste)
    token = _token_grupo(client.post("/importar/sincronizar", follow_redirects=False))
    pagina = client.get(f"/importar/sincronizacao/{token}")
    assert pagina.status_code == 200
    assert "Conta Corrente Ficticia" in pagina.text and "Cartao Ficticio Gold" in pagina.text
    assert "Qual conta do app é esta?" in pagina.text and "Qual cartão do app é esta?" in pagina.text
    assert "Confirmar todas" not in pagina.text  # nada vinculado ainda

    # vincula o cartão e confirma só ele
    r = client.post(f"/importar/sincronizacao/{token}/1/vincular", data={"conta_id": str(cartao.id)}, follow_redirects=False)
    assert r.status_code == 303
    pagina = client.get(f"/importar/sincronizacao/{token}")
    assert "3 transações" in pagina.text and "Confirmar todas" in pagina.text
    client.post(f"/importar/sincronizacao/{token}/1/confirmar")
    db.expire_all()
    assert db.get(Conta, cartao.id).identificador_externo == "pluggy:acc-cartao-0001"  # vínculo gravado
    assert db.scalar(select(func.count(Lancamento.id)).where(Lancamento.conta_id == cartao.id,
                                                               Lancamento.status == "efetivado")) == 3
    assert "Importada: 3 novas" in client.get(f"/importar/sincronizacao/{token}").text

    # vincula e confirma a corrente com "confirmar todas"
    client.post(f"/importar/sincronizacao/{token}/0/vincular", data={"conta_id": str(corrente.id)})
    client.post(f"/importar/sincronizacao/{token}/confirmar-todas")
    db.expire_all()
    assert db.get(Conta, corrente.id).identificador_externo == "pluggy:acc-corrente-0001"
    total = db.scalar(select(func.count(Lancamento.id)))

    # a tela inicial e uma nova sincronização: tudo duplicado, nada novo
    novo = _token_grupo(client.post("/importar/sincronizar", follow_redirects=False))
    pag = client.get(f"/importar/sincronizacao/{novo}").text
    assert "Qual conta do app" not in pag and "já importadas" in pag
    client.post(f"/importar/sincronizacao/{novo}/confirmar-todas")
    db.expire_all()
    assert db.scalar(select(func.count(Lancamento.id))) == total


def test_revisao_individual_registra_sincronizacao_e_vinculo(client, db, app_teste, keyring_falso):
    cartao = novo_cartao(db, "Cartão", pagadora=nova_conta(db, "Corrente"), fechamento=8, vencimento=15)
    _configurar(db, keyring_falso, app_teste)
    token = _token_grupo(client.post("/importar/sincronizar", follow_redirects=False))
    client.post(f"/importar/sincronizacao/{token}/1/vincular", data={"conta_id": str(cartao.id)})
    pagina = client.get(f"/importar/sincronizacao/{token}").text
    link = pagina.split('href="/importar/')[1].split('"')[0]
    revisao = client.get(f"/importar/{link}")
    assert revisao.status_code == 200 and "Open Finance" in revisao.text
    proposta = app_teste.state.armazem_importacao.obter(link).proposta
    marcadas = {f"marcada_{ln.indice}": "1" for ln in proposta.linhas if ln.marcada}
    r = client.post(f"/importar/{link}/confirmar", data=marcadas, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Conta, cartao.id).identificador_externo == "pluggy:acc-cartao-0001"
    html = client.get("/").text
    assert "Última sincronização: hoje" in html


def test_grupo_expirado(client):
    r = client.get("/importar/sincronizacao/inexistente")
    assert r.status_code == 404 and "expirou" in r.text


def test_erro_de_rede_na_sincronizacao(client, db, app_teste, keyring_falso):
    import httpx

    from app.services.openfinance.pluggy import ClientePluggy

    _configurar(db, keyring_falso, app_teste)

    def cai(req):
        raise httpx.ConnectError("x")

    app_teste.state.cliente_pluggy = ClientePluggy("i", "s", httpx.Client(transport=httpx.MockTransport(cai)))
    r = client.post("/importar/sincronizar")
    assert r.status_code == 502 and "Sem conexão" in r.text


# ---------- Dashboard ----------

def test_selo_do_dashboard(client, db):
    assert "Última sincronização" not in client.get("/").text
    adicionar_item(db, "item-1")
    db.commit()
    assert "Última sincronização: ainda não sincronizado" in client.get("/").text
    registrar_sincronizacao(db, "acc-1", date(2026, 10, 2))
    db.commit()
    html = client.get("/").text
    assert "Última sincronização: há 3 dias" in html and 'href="/importar"' in html
    assert HOJE_TESTE == date(2026, 10, 5)
