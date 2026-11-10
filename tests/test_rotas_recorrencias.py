from sqlalchemy import func, select

from app.models import Lancamento, Recorrencia
from tests.factories import nova_conta


def _recorrencia(db, **kw):
    conta = nova_conta(db)
    rec = Recorrencia(descricao="Spotify", tipo="despesa", conta_id=conta.id, valor=4090, dia=15,
                      inicio="2026-10", eh_assinatura=True, **kw)
    db.add(rec)
    db.commit()
    return conta, rec


def test_subpagina_lista_as_recorrencias(client, db):
    _recorrencia(db)
    r = client.get("/lancamentos/recorrencias")
    assert r.status_code == 200
    assert "Spotify" in r.text


def test_aba_antiga_redireciona(client, db):
    nova_conta(db)
    db.commit()
    r = client.get("/configuracoes?aba=recorrencias", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/lancamentos/recorrencias"


def test_configuracoes_nao_tem_mais_a_aba(client, db):
    nova_conta(db)
    db.commit()
    assert "Recorrências e assinaturas" not in client.get("/configuracoes").text


def test_lancamentos_tem_link_para_a_subpagina(client, db):
    nova_conta(db)
    db.commit()
    assert 'href="/lancamentos/recorrencias"' in client.get("/lancamentos").text


def test_criar_pelo_endereco_novo(client, db):
    conta = nova_conta(db)
    db.commit()
    r = client.post("/lancamentos/recorrencias", data={
        "descricao": "Aluguel", "tipo": "despesa", "conta_id": str(conta.id), "valor": "1.200,00",
        "dia": "10", "inicio": "2026-10"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/lancamentos/recorrencias"
    rec = db.scalars(select(Recorrencia)).one()
    assert rec.valor == 120000
    assert db.scalar(select(func.count(Lancamento.id)).where(Lancamento.recorrencia_id == rec.id)) > 0


def test_criar_com_erro_devolve_400(client, db):
    nova_conta(db)
    db.commit()
    r = client.post("/lancamentos/recorrencias", data={"descricao": "", "tipo": "despesa"})
    assert r.status_code == 400


def test_alterar_e_encerrar_pelo_endereco_novo(client, db):
    conta, rec = _recorrencia(db)
    r = client.post(f"/lancamentos/recorrencias/{rec.id}", data={
        "descricao": "Spotify", "conta_id": str(conta.id), "valor": "45,00", "dia": "15", "a_partir": "2026-10"},
        follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Recorrencia, rec.id).valor == 4500
    r = client.post(f"/lancamentos/recorrencias/{rec.id}/encerrar", data={"a_partir": "2026-12"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Recorrencia, rec.id).fim is not None


def test_enderecos_antigos_de_escrita_nao_existem_mais(client, db):
    conta = nova_conta(db)
    db.commit()
    assert client.post("/configuracoes/recorrencias", data={"descricao": "x"}).status_code in (404, 405)


def test_recorrencias_sem_conceito_de_assinatura(client, db):
    conta = nova_conta(db)
    db.add(Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=120000, dia=5, inicio="2026-10"))
    db.commit()
    html = client.get("/lancamentos/recorrencias").text
    assert "Aluguel" in html
    assert 'name="eh_assinatura"' not in html and "Custo anual das assinaturas" not in html
    assert "Nenhuma assinatura cadastrada." not in html


def test_eh_assinatura_do_formulario_e_ignorado(client, db):
    conta = nova_conta(db)
    db.commit()
    client.post("/lancamentos/recorrencias", data={
        "descricao": "Aluguel", "tipo": "despesa", "conta_id": str(conta.id), "valor": "1.200,00",
        "dia": "10", "inicio": "2026-10", "eh_assinatura": "on"})
    assert db.scalars(select(Recorrencia)).one().eh_assinatura is False
