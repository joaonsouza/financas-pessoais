def test_health(client):
    resposta = client.get("/health")
    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}
