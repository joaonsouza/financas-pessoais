"""Ordem dos cartões escolhida pelo João, a mesma em todas as telas. Dados fictícios."""
from app.services.faturas import cartoes_ativos, reordenar_cartoes
from tests.factories import nova_conta, novo_cartao


def _tres(db):
    conta = nova_conta(db, "Conta")
    a, b, c = (novo_cartao(db, nome, pagadora=conta) for nome in ("Cartão A", "Cartão B", "Cartão C"))
    db.commit()
    return a, b, c


def _nomes(db):
    return [c.nome for c in cartoes_ativos(db)]


def test_sem_ordem_e_alfabetica(db):
    _tres(db)
    assert _nomes(db) == ["Cartão A", "Cartão B", "Cartão C"]


def test_reordenar_e_os_que_faltam_vao_para_o_fim(db):
    a, b, c = _tres(db)
    reordenar_cartoes(db, [c.id, a.id])
    db.commit()
    assert _nomes(db) == ["Cartão C", "Cartão A", "Cartão B"]


def test_cartao_novo_entra_no_fim(client, db):
    a, b, c = _tres(db)
    reordenar_cartoes(db, [c.id, b.id, a.id])
    db.commit()
    client.post("/cartoes", data={"nome": "Cartão 0", "limite": "100,00", "pagadora_id": str(a.conta_pagamento_id),
                                  "fechamento": "3", "vencimento": "10"})
    assert _nomes(db) == ["Cartão C", "Cartão B", "Cartão A", "Cartão 0"]


def test_rota_salva_e_as_telas_seguem_a_ordem(client, db):
    a, b, c = _tres(db)
    assert client.post("/cartoes/ordem", data={"ids": [str(c.id), str(b.id), str(a.id)]}).status_code == 204
    for url in ("/cartoes", "/?mes=2026-10"):
        texto = client.get(url).text
        assert texto.index("Cartão C") < texto.index("Cartão B") < texto.index("Cartão A"), url
    texto = client.get("/cartoes").text
    assert 'data-cartao="' in texto and "Organizar" in texto
