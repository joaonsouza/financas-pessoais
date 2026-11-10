"""A reserva de emergência escolhe a carteira em que aparece (além da consolidada)."""
from app.models import Carteira, Meta
from app.services.invest_carteira import garantir_carteira_padrao, resumo_carteira
from tests.factories import nova_conta
from tests.test_invest_reserva import HOJE, cenario
from tests.test_rotas_investimentos import mercado  # noqa: F401


def _outra_carteira(s):
    outra = Carteira(nome="Ana")
    s.add(outra)
    s.commit()
    return outra


def test_sem_escolha_so_a_consolidada_mostra_a_reserva(session):
    cenario(session)
    padrao = garantir_carteira_padrao(session)
    assert resumo_carteira(session, HOJE, padrao).reserva == []
    assert [a.ativo.codigo for a in resumo_carteira(session, HOJE).reserva] == ["RESV3"]


def test_carteira_escolhida_mostra_a_reserva_fora_da_alocacao(session):
    c = cenario(session)
    padrao = garantir_carteira_padrao(session)
    outra = _outra_carteira(session)
    c["meta"].carteira_id = outra.id
    session.commit()
    escolhida = resumo_carteira(session, HOJE, outra)
    assert [a.ativo.codigo for a in escolhida.reserva] == ["RESV3"] and escolhida.reserva_alvo == 3_600_000
    assert escolhida.patrimonio == 0 and escolhida.ativos == []      # a reserva não entra na alocação
    assert resumo_carteira(session, HOJE, padrao).reserva == []      # só na escolhida
    assert [a.ativo.codigo for a in resumo_carteira(session, HOJE).reserva] == ["RESV3"]   # e na consolidada


def test_formulario_da_reserva_grava_a_carteira(client, db):
    conta = nova_conta(db, "Reserva", tipo="investimento", sl=False)
    garantir_carteira_padrao(db)
    outra = _outra_carteira(db)
    meta = Meta(nome="Reserva", tipo="reserva_emergencia", valor_alvo=100_000, conta_id=conta.id)
    db.add(meta)
    db.commit()
    texto = client.get(f"/planejamento/metas/{meta.id}").text
    assert 'name="carteira_id"' in texto and "Só na consolidada" in texto and "Ana" in texto
    dados = {"nome": "Reserva", "tipo": "reserva_emergencia", "conta_id": str(conta.id), "ativa": "on",
             "voltar": "detalhe"}
    r = client.post(f"/planejamento/metas/{meta.id}", data={**dados, "carteira_id": str(outra.id)})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Meta, meta.id).carteira_id == outra.id
    r = client.post(f"/planejamento/metas/{meta.id}", data={**dados, "carteira_id": "999"})
    assert r.status_code == 422 and "carteira" in r.text
    client.post(f"/planejamento/metas/{meta.id}", data={**dados, "carteira_id": ""})
    db.expire_all()
    assert db.get(Meta, meta.id).carteira_id is None


def test_objetivo_nao_guarda_carteira(client, db):
    nova_conta(db)
    outra = _outra_carteira(db)
    r = client.post("/planejamento/metas", data={"nome": "Viagem", "tipo": "objetivo", "carteira_id": str(outra.id)},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[r.text.find('role="alert"'):][:300]
    db.expire_all()
    assert db.query(Meta).one().carteira_id is None


def test_pagina_da_carteira_escolhida_lista_a_reserva(client, db, mercado):  # noqa: F811
    nova_conta(db)
    c = cenario(db)
    outra = _outra_carteira(db)
    c["meta"].carteira_id = outra.id
    db.commit()
    assert 'id="titulo-reserva-carteira"' in client.get(f"/investimentos?carteira={outra.id}").text
    padrao = garantir_carteira_padrao(db)
    assert 'id="titulo-reserva-carteira"' not in client.get(f"/investimentos?carteira={padrao.id}").text
