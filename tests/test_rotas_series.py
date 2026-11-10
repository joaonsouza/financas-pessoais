from datetime import date

from sqlalchemy import select

from app.models import Lancamento, Recorrencia
from app.services.lancamentos import criar_lancamento, criar_lancamento_repetido
from app.services.recorrencia import gerar_pendentes
from tests.factories import nova_conta


def _aluguel(db):
    conta = nova_conta(db)
    rec = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=10000, dia=10, inicio="2026-10")
    db.add(rec)
    db.flush()
    gerar_pendentes(db, date(2026, 10, 5))
    db.commit()
    ocorrencias = list(db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)
                                  .order_by(Lancamento.competencia_recorrencia)))
    return conta, rec, ocorrencias


def _descricoes(db, rec_id):
    db.expire_all()
    return [l.descricao for l in db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec_id)
                                            .order_by(Lancamento.competencia_recorrencia))]


def test_form_de_edicao_pergunta_o_alcance(client, db):
    conta, _, ocorrencias = _aluguel(db)
    avulso = criar_lancamento(db, conta=conta, data=date(2026, 10, 3), descricao="Pão", valor_abs=500, tipo="despesa")
    parcelas = criar_lancamento_repetido(db, conta=conta, data=date(2026, 10, 1), descricao="Curso",
                                         valor_abs=5000, vezes=3, tipo="despesa")
    db.commit()

    recorrente = client.get(f"/lancamentos/{ocorrencias[0].id}/editar").text
    parcelada = client.get(f"/lancamentos/{parcelas[0].id}/editar").text
    simples = client.get(f"/lancamentos/{avulso.id}/editar").text

    assert "Este e os próximos" in recorrente and 'name="escopo"' in recorrente
    assert "Esta e as próximas" in parcelada
    assert 'name="escopo"' not in simples


def test_salvar_edicao_com_alcance(client, db):
    conta, rec, ocorrencias = _aluguel(db)

    resposta = client.post(f"/lancamentos/{ocorrencias[1].id}", data={
        "valor": "100,00", "descricao": "Aluguel novo", "data": "2026-11-10", "conta_id": str(conta.id),
        "escopo": "proximos",
    })

    assert resposta.headers["HX-Refresh"] == "true"
    assert _descricoes(db, rec.id) == ["Aluguel", "Aluguel novo", "Aluguel novo"]


def test_salvar_edicao_sem_alcance_muda_so_este(client, db):
    conta, rec, ocorrencias = _aluguel(db)

    client.post(f"/lancamentos/{ocorrencias[1].id}", data={
        "valor": "100,00", "descricao": "Aluguel novo", "data": "2026-11-10", "conta_id": str(conta.id),
    })

    assert _descricoes(db, rec.id) == ["Aluguel", "Aluguel novo", "Aluguel"]


def test_modal_de_exclusao_e_excluir_todos(client, db):
    _, rec, ocorrencias = _aluguel(db)
    rec_id = rec.id

    modal = client.get(f"/lancamentos/{ocorrencias[1].id}/excluir").text
    assert "Este e os próximos" in modal and 'value="todos"' in modal

    resposta = client.post(f"/lancamentos/{ocorrencias[1].id}/excluir", data={"escopo": "todos"})

    assert resposta.headers["HX-Refresh"] == "true"
    assert _descricoes(db, rec_id) == []
    assert db.get(Recorrencia, rec_id) is None


def test_lista_abre_o_modal_para_serie_e_confirma_para_simples(client, db):
    conta, _, ocorrencias = _aluguel(db)
    avulso = criar_lancamento(db, conta=conta, data=date(2026, 10, 3), descricao="Pão", valor_abs=500, tipo="despesa")
    db.commit()

    texto = client.get("/lancamentos?mes=2026-10").text

    assert f'hx-get="/lancamentos/{ocorrencias[0].id}/excluir"' in texto
    assert f'hx-post="/lancamentos/{avulso.id}/excluir" hx-confirm' in texto
