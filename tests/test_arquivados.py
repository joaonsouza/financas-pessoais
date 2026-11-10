"""Botões redondos no topo de Contas e Cartões, e as listas de arquivados."""
from app.models import Conta
from tests.factories import nova_conta, novo_cartao


def test_contas_tem_botoes_no_topo(client, db):
    nova_conta(db, "Itaú")
    db.commit()
    texto = client.get("/contas?mes=2026-10").text

    assert 'aria-label="Nova conta"' in texto
    assert "/desempenho?mes=2026-10#patrimonio" in texto
    assert "Ver arquivadas" in texto


def test_conta_arquivada_aparece_so_na_lista_de_arquivadas_e_volta(client, db):
    ativa = nova_conta(db, "Itaú")
    velha = nova_conta(db, "Banco Antigo")
    db.commit()
    client.post(f"/contas/{velha.id}/arquivar?mes=2026-10")

    normal = client.get("/contas?mes=2026-10").text
    arquivadas = client.get("/contas?mes=2026-10&arquivadas=1").text

    assert "Banco Antigo" not in normal
    assert "Banco Antigo" in arquivadas and f"/contas/{velha.id}/desarquivar" in arquivadas
    assert f"/contas/{ativa.id}/desarquivar" not in arquivadas

    resposta = client.post(f"/contas/{velha.id}/desarquivar?mes=2026-10", follow_redirects=False)
    assert resposta.status_code == 303
    db.expire_all()
    assert db.get(Conta, velha.id).ativa


def test_cartoes_tem_botoes_no_topo(client, db):
    novo_cartao(db, "Cartão Itaú", pagadora=nova_conta(db))
    db.commit()
    texto = client.get("/cartoes").text

    assert 'aria-label="Novo cartão"' in texto
    assert "Ver arquivados" in texto


def test_arquivar_e_desarquivar_cartao(client, db):
    cartao = novo_cartao(db, "Cartão Velho", pagadora=nova_conta(db))
    db.commit()

    assert f"/cartoes/{cartao.id}/arquivar" in client.get("/cartoes").text
    resposta = client.post(f"/cartoes/{cartao.id}/arquivar", follow_redirects=False)
    assert resposta.status_code == 303
    db.expire_all()
    assert not db.get(Conta, cartao.id).ativa
    assert "Cartão Velho" not in client.get("/cartoes").text
    assert f"/cartoes/{cartao.id}/desarquivar" in client.get("/cartoes?arquivados=1").text

    client.post(f"/cartoes/{cartao.id}/desarquivar")
    db.expire_all()
    assert db.get(Conta, cartao.id).ativa


def test_desarquivar_conta_inexistente_da_404(client, db):
    nova_conta(db)  # sem conta nenhuma o app manda para o setup
    db.commit()
    assert client.post("/contas/999/desarquivar").status_code == 404
    assert client.post("/cartoes/999/arquivar").status_code == 404
