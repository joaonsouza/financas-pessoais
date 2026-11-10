from datetime import date

from sqlalchemy import func, select

from app.models import Lancamento
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _base(db):
    conta = nova_conta(db, "Nubank")
    cartao = novo_cartao(db, "Cartão Nubank", pagadora=conta)
    categoria = nova_categoria(db, "Mercado")
    db.commit()
    return conta, cartao, categoria


def test_lista_agrupada_por_dia(client, db):
    conta, _, categoria = _base(db)
    novo_lancamento(db, conta, date(2026, 10, 2), -1250, categoria=categoria, descricao="Padaria")
    db.commit()
    texto = client.get("/lancamentos").text
    assert "02/10/2026" in texto
    assert "Padaria" in texto
    assert "-R$ 12,50" in texto


def test_modal_novo_cartao_lista_so_cartoes(client, db):
    _base(db)
    texto = client.get("/lancamentos/novo?tipo=cartao").text
    assert 'name="valor"' in texto
    assert "Cartão Nubank" in texto
    assert ">Nubank<" not in texto


def test_criar_lancamento(client, db):
    conta, _, categoria = _base(db)
    resposta = client.post("/lancamentos", data={
        "tipo": "despesa", "valor": "12,50", "descricao": "Padaria", "data": "2026-10-02",
        "conta_id": str(conta.id), "categoria_id": str(categoria.id), "tags": "casa, viagem",
        "efetivado": "on", "repetir": "nao",
    })
    assert resposta.status_code == 200
    assert resposta.headers["HX-Refresh"] == "true"
    lanc = db.scalars(select(Lancamento)).one()
    assert (lanc.valor, lanc.status, sorted(t.nome for t in lanc.tags)) == (-1250, "efetivado", ["casa", "viagem"])


def test_criar_lancamento_invalido_reexibe_modal(client, db):
    conta, _, _ = _base(db)
    resposta = client.post("/lancamentos", data={
        "tipo": "despesa", "valor": "abc", "descricao": "Padaria", "data": "2026-10-02",
        "conta_id": str(conta.id), "repetir": "nao",
    })
    assert resposta.status_code == 422
    assert "valor inválido" in resposta.text
    assert 'value="Padaria"' in resposta.text


def test_alternar_e_excluir(client, db):
    conta, _, _ = _base(db)
    lanc = novo_lancamento(db, conta, date(2026, 10, 2), -100, status="pendente", descricao="Água")
    db.commit()

    resposta = client.post(f"/lancamentos/{lanc.id}/alternar")
    assert resposta.status_code == 200
    assert f'id="lanc-{lanc.id}"' in resposta.text
    db.refresh(lanc)
    assert lanc.status == "efetivado"

    resposta = client.post(f"/lancamentos/{lanc.id}/excluir")
    assert resposta.headers["HX-Refresh"] == "true"
    db.expire_all()
    assert db.scalar(select(func.count(Lancamento.id))) == 0


def test_editar_lancamento(client, db):
    conta, _, categoria = _base(db)
    lanc = novo_lancamento(db, conta, date(2026, 10, 2), -100, descricao="Agua")
    db.commit()

    assert 'value="Agua"' in client.get(f"/lancamentos/{lanc.id}/editar").text
    resposta = client.post(f"/lancamentos/{lanc.id}", data={
        "tipo": "despesa", "valor": "2,00", "descricao": "Água", "data": "2026-10-03",
        "conta_id": str(conta.id), "categoria_id": str(categoria.id), "tags": "",
    })
    assert resposta.headers["HX-Refresh"] == "true"
    db.refresh(lanc)
    assert (lanc.valor, lanc.descricao, lanc.data, lanc.status) == (-200, "Água", date(2026, 10, 3), "pendente")


def test_sugestao(client, db):
    conta, _, categoria = _base(db)
    novo_lancamento(db, conta, date(2026, 10, 2), -100, categoria=categoria, descricao="Padaria")
    db.commit()
    assert client.get("/lancamentos/sugestao?descricao=Padaria").json() == {
        "categoria_id": categoria.id, "conta_id": conta.id,
    }


def test_lancamento_inexistente_404(client, db):
    _base(db)
    assert client.get("/lancamentos/999/editar").status_code == 404


def test_criar_lancamento_ignorado_e_selo_na_lista(client, db):
    conta, _, _ = _base(db)
    client.post("/lancamentos", data={
        "tipo": "despesa", "valor": "50,00", "descricao": "Estorno", "data": "2026-10-02",
        "conta_id": str(conta.id), "efetivado": "on", "ignorar": "on", "repetir": "nao",
    })
    lanc = db.scalars(select(Lancamento)).one()
    assert lanc.ignorar is True
    texto = client.get("/lancamentos").text
    assert "Estorno" in texto and "ignorada" in texto and "eye-off" in texto
    assert "R$ 0,00" in texto  # totais não contam a ignorada


def test_modal_tem_toggle_ignorar_e_edicao_preserva(client, db):
    conta, _, _ = _base(db)
    assert 'name="ignorar"' in client.get("/lancamentos/novo").text
    lanc = novo_lancamento(db, conta, date(2026, 10, 2), -1000, ignorar=True)
    db.commit()
    editar = client.get(f"/lancamentos/{lanc.id}/editar").text
    assert 'name="ignorar" checked' in editar
    client.post(f"/lancamentos/{lanc.id}", data={
        "valor": "10,00", "descricao": "Teste", "data": "2026-10-02", "conta_id": str(conta.id), "efetivado": "on",
    })
    db.refresh(lanc)
    assert lanc.ignorar is False


def test_modal_de_despesa_mostra_lembrete_das_72h_sem_bloquear(client, db):
    _base(db)
    for tipo in ("despesa", "cartao"):
        texto = client.get(f"/lancamentos/novo?tipo={tipo}").text
        assert "Regra das 72 h: esperar 3 dias antes de parcelar algo novo." in texto
    assert "Regra das 72 h" not in client.get("/lancamentos/novo?tipo=receita").text


def test_criar_receita_fixa_anual(client, db):
    from app.models import Recorrencia
    conta, _, _ = _base(db)
    r = client.post("/lancamentos", data={
        "tipo": "receita", "valor": "5000", "descricao": "13º", "data": "2026-12-20",
        "conta_id": str(conta.id), "repetir": "fixa", "intervalo": "12", "fim": "2030-12",
    })
    assert r.status_code == 200
    db.expire_all()
    rec = db.scalar(select(Recorrencia))
    assert (rec.intervalo_meses, rec.fim) == (12, "2030-12")


def test_modal_novo_tem_seletor_de_intervalo(client, db):
    _base(db)
    texto = client.get("/lancamentos/novo?tipo=despesa").text
    assert 'name="intervalo"' in texto and "Todo ano" in texto and 'name="fim_data"' in texto


def test_criar_fixa_semanal_ate_data(client, db):
    from app.models import Recorrencia
    conta, _, _ = _base(db)
    r = client.post("/lancamentos", data={
        "tipo": "despesa", "valor": "50", "descricao": "Diarista", "data": "2026-10-08", "conta_id": str(conta.id),
        "repetir": "fixa", "unidade": "semana", "intervalo": "2", "dias": ["0", "3"],
        "fim_tipo": "data", "fim_data": "2026-12-31",
    })
    assert r.status_code == 200
    db.expire_all()
    rec = db.scalar(select(Recorrencia))
    assert (rec.frequencia, rec.intervalo_semanas, rec.dias_semana) == ("semanal", 2, "0,3")
    assert str(rec.fim_data) == "2026-12-31"


def test_criar_fixa_apos_n_vezes_e_erro_sem_numero(client, db):
    from app.models import Recorrencia
    conta, _, _ = _base(db)
    base = {"tipo": "despesa", "valor": "50", "descricao": "Curso", "data": "2026-10-08",
            "conta_id": str(conta.id), "repetir": "fixa", "unidade": "mes", "intervalo": "1", "fim_tipo": "vezes"}
    r = client.post("/lancamentos", data={**base, "fim_vezes": ""})
    assert r.status_code == 422 and "quantas ocorrências" in r.text
    assert client.post("/lancamentos", data={**base, "fim_vezes": "5"}).status_code == 200
    db.expire_all()
    assert db.scalar(select(Recorrencia)).max_ocorrencias == 5


def test_modal_novo_tem_painel_de_recorrencia_personalizada(client, db):
    _base(db)
    texto = client.get("/lancamentos/novo?tipo=despesa").text
    assert "Personalizado" in texto and 'name="fim_tipo"' in texto and 'name="dias"' in texto
