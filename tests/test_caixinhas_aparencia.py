"""Ícone e cor da caixinha: escolhidos ao criar e ao editar."""
from app.models import Caixinha
from tests.factories import nova_conta


def test_criar_caixinha_com_icone_e_cor(client, db):
    conta = nova_conta(db, "Caixa")
    db.commit()

    client.post(f"/contas/{conta.id}/caixinhas", data={"nome": "Viagem", "icone": "plane", "cor": "#3b82f6"})

    cx = db.query(Caixinha).filter_by(nome="Viagem").one()
    assert (cx.icone, cx.cor) == ("plane", "#3b82f6")


def test_criar_sem_escolher_fica_no_padrao_e_ignora_valor_fora_da_lista(client, db):
    conta = nova_conta(db, "Caixa")
    db.commit()

    client.post(f"/contas/{conta.id}/caixinhas", data={"nome": "IPTU", "icone": "bomba", "cor": "red"})

    cx = db.query(Caixinha).filter_by(nome="IPTU").one()
    assert (cx.icone, cx.cor) == ("piggy-bank", "#10b981")


def test_editar_caixinha_troca_nome_icone_e_cor(client, db):
    conta = nova_conta(db, "Caixa")
    db.commit()
    client.post(f"/contas/{conta.id}/caixinhas", data={"nome": "IPTU"})
    cx = db.query(Caixinha).filter_by(nome="IPTU").one()

    r = client.post(f"/contas/{conta.id}/caixinhas/{cx.id}/editar",
                    data={"nome": "IPTU 2027", "icone": "house", "cor": "#f97316"}, follow_redirects=False)

    assert r.status_code == 303
    db.expire_all()
    cx = db.get(Caixinha, cx.id)
    assert (cx.nome, cx.icone, cx.cor) == ("IPTU 2027", "house", "#f97316")


def test_tela_da_conta_tem_seletor_de_icone_e_cor(client, db):
    conta = nova_conta(db, "Caixa")
    db.commit()
    client.post(f"/contas/{conta.id}/caixinhas", data={"nome": "IPTU"})

    texto = client.get(f"/contas/{conta.id}").text

    assert texto.count('name="icone"') >= 2 and texto.count('name="cor"') >= 2  # criar e editar
    assert "/caixinhas/" in texto and "/editar" in texto
