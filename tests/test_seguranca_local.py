"""O app roda sem login em 127.0.0.1: só aceita pedido que venha do próprio navegador apontado para ele.

- Host diferente de 127.0.0.1/localhost (DNS rebinding: um site com DNS próprio apontando para 127.0.0.1) -> 400.
- POST vindo de outro site (CSRF: formulário escondido numa página qualquer) -> 403.
"""
import pytest


def test_host_local_passa(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health", headers={"Host": "localhost:8000"}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8000", "127.0.0.1.evil.example.com"])
def test_host_estranho_barrado(client, host):
    r = client.get("/health", headers={"Host": host})
    assert r.status_code == 400


def test_post_de_outro_site_barrado_por_origin(client):
    r = client.post("/configuracoes/ia/chave", data={"chave": "sk-ant-do-atacante"},
                    headers={"Origin": "https://evil.example.com"}, follow_redirects=False)
    assert r.status_code == 403


def test_post_com_origin_null_barrado(client):
    r = client.post("/configuracoes/ia/chave", data={"chave": "x"}, headers={"Origin": "null"}, follow_redirects=False)
    assert r.status_code == 403


def test_post_cross_site_barrado_por_sec_fetch_site(client):
    r = client.post("/configuracoes/ia/chave", data={"chave": "x"},
                    headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403


def test_post_de_outra_porta_local_barrado(client):
    """Outro programa servindo em localhost:3000 também não pode mexer nos dados."""
    r = client.post("/configuracoes/ia/chave", data={"chave": "x"},
                    headers={"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"}, follow_redirects=False)
    assert r.status_code == 403


def test_post_do_proprio_app_passa(client):
    r = client.post("/configuracoes/ia/chave", data={"chave": "sk-ant-teste"},
                    headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert r.status_code != 403


def test_get_de_outro_site_passa(client):
    """Link vindo de fora (favorito, e-mail) abre normalmente; GET não altera nada."""
    r = client.get("/health", headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.com"})
    assert r.status_code == 200
