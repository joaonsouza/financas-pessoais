from tests.factories import nova_conta


def test_sem_contas_redireciona_para_setup(client):
    resposta = client.get("/", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/setup"


def test_layout_com_menu_e_mes_atual(client, db):
    nova_conta(db)
    db.commit()
    resposta = client.get("/")
    assert resposta.status_code == 200
    assert "Lançamentos" in resposta.text
    assert "Outubro 2026" in resposta.text
    assert 'id="modal"' in resposta.text


def test_seletor_de_mes(client, db):
    nova_conta(db)
    db.commit()
    assert "Dezembro 2026" in client.get("/?mes=2026-12").text
    assert "?mes=2026-11" in client.get("/?mes=2026-12").text
    assert "Outubro 2026" in client.get("/?mes=2026-99").text


def test_arquivos_estaticos(client):
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
