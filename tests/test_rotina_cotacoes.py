import httpx

from app import main
from app.services.mercado.bcb import Bcb
from app.services.mercado.tesouro import Tesouro
from app.services.mercado.yahoo import Yahoo
from app.web.deps import Provedores
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta
from tests.test_mercado_provedores import HttpFalso, sem_rede_real  # noqa: F401  (bloqueia a rede real)
from tests.test_rotas_investimentos import AGORA, ROTAS_OK, cadastrar


def _provedores(http: HttpFalso) -> Provedores:
    return Provedores(yahoo=Yahoo(http.cliente()), bcb=Bcb(http.cliente()), tesouro=Tesouro(http.cliente()))


def test_rotina_deixa_a_cotacao_pronta_sem_clicar_em_atualizar(client, db, app_teste):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    main.atualizar_mercado_em_segundo_plano(app_teste.state.fabrica_sessao, AGORA, HOJE_TESTE,
                                            _provedores(HttpFalso(ROTAS_OK)))
    texto = client.get("/investimentos").text
    assert "R$ 38,12" in texto and "R$ 3.812,00" in texto


def test_rotina_com_provedor_fora_do_ar_nao_levanta(client, db, app_teste):
    nova_conta(db)
    db.commit()
    cadastrar(client)
    sem_rede = HttpFalso({"query1": httpx.ConnectError("sem rede"), "bcdata": httpx.ConnectError("sem rede"),
                          "PrecoTaxaTesouroDireto": httpx.ConnectError("sem rede")})
    main.atualizar_mercado_em_segundo_plano(app_teste.state.fabrica_sessao, AGORA, HOJE_TESTE, _provedores(sem_rede))
    assert client.get("/investimentos").status_code == 200
