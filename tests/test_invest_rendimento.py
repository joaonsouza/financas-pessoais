"""Rendimento embutido do ativo (% a.a.), editado na tela do ativo."""
from decimal import Decimal as D

from app.models import Ativo
from tests.factories import nova_conta
from tests.test_mercado_provedores import sem_rede_real  # noqa: F401  (bloqueia a rede real)
from tests.test_rotas_investimentos import cadastrar, mercado  # noqa: F401


def test_salvar_e_limpar_rendimento_na_tela_do_ativo(client, db, mercado):
    nova_conta(db)
    db.commit()
    cadastrar(client, classe="cripto", codigo="usdc", quantidade="10", preco="5,00", data="2025-10-05")

    assert "Rendimento embutido" in client.get("/investimentos/ativos/1").text
    r = client.post("/investimentos/ativos/1/rendimento", data={"rendimento": "4"}, follow_redirects=False)
    assert r.status_code == 303
    a = db.get(Ativo, 1)
    db.refresh(a)
    assert a.rendimento_aa == D("4")
    # um ano depois da compra (hoje dos testes: 05/10/2026): 10 viraram 10,4
    assert "10,4" in client.get("/investimentos/ativos/1").text

    assert client.post("/investimentos/ativos/1/rendimento", data={"rendimento": "-1"}).status_code == 422
    client.post("/investimentos/ativos/1/rendimento", data={"rendimento": ""})
    db.refresh(a)
    assert a.rendimento_aa is None
