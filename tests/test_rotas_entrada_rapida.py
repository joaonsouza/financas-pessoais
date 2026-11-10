from datetime import date

from sqlalchemy import func, select

from app.models import Favorito, Lancamento
from tests.factories import nova_categoria, nova_conta


def _base(db):
    conta = nova_conta(db, "Nubank")
    cat = nova_categoria(db, "Mercado")
    db.commit()
    return conta, cat


def _dados(conta, **kw):
    dados = {"tipo": "despesa", "valor": "12,50", "descricao": "Padaria", "data": "2026-10-02",
             "conta_id": str(conta.id), "efetivado": "on", "repetir": "nao"}
    dados.update(kw)
    return dados


def test_salvar_normal_continua_com_hx_refresh(client, db):
    conta, _ = _base(db)
    r = client.post("/lancamentos", data=_dados(conta))
    assert r.headers["HX-Refresh"] == "true"


def test_salvar_e_criar_nova_devolve_modal_vazio_mantendo_data_e_conta(client, db):
    conta, cat = _base(db)
    r = client.post("/lancamentos", data=_dados(conta, continuar="1", categoria_id=str(cat.id)))
    assert r.status_code == 200
    assert "HX-Refresh" not in r.headers
    assert db.scalar(select(func.count(Lancamento.id))) == 1
    assert "Lançamento salvo" in r.text and "check-circle" in r.text
    assert 'data-recarregar="1"' in r.text
    assert 'value="2026-10-02"' in r.text
    assert f'<option value="{conta.id}" selected>' in r.text
    assert 'name="valor" value=""' in r.text and 'x-ref="descricao" value=""' in r.text
    assert 'name="continuar"' in r.text


def test_modal_sem_continuar_nao_marca_recarregar(client, db):
    _base(db)
    texto = client.get("/lancamentos/novo").text
    assert "data-recarregar" not in texto
    assert 'name="continuar"' in texto


def test_erro_apos_continuar_mantem_flag_recarregar(client, db):
    conta, _ = _base(db)
    r = client.post("/lancamentos", data=_dados(conta, valor="abc", recarregar="1"))
    assert r.status_code == 422 and 'data-recarregar="1"' in r.text


def test_cor_por_tipo_e_token_cartao(client, db):
    _base(db)
    assert "text-expense" in client.get("/lancamentos/novo?tipo=despesa").text
    assert "text-income" in client.get("/lancamentos/novo?tipo=receita").text
    assert "text-primary" in client.get("/lancamentos/novo?tipo=transferencia").text
    assert "card-accent" in client.get("/lancamentos/novo?tipo=cartao").text


def test_valor_aceita_conta(client, db):
    conta, _ = _base(db)
    client.post("/lancamentos", data=_dados(conta, valor="45,90+12"))
    assert db.scalars(select(Lancamento)).one().valor == -5790
    assert "Aceita contas: 12,50+8" in client.get("/lancamentos/novo").text


def test_valor_invalido_na_conta_reexibe_erro(client, db):
    conta, _ = _base(db)
    r = client.post("/lancamentos", data=_dados(conta, valor="5-5"))
    assert r.status_code == 422 and "valor inválido" in r.text


def test_edicao_aceita_conta(client, db):
    conta, _ = _base(db)
    client.post("/lancamentos", data=_dados(conta))
    lanc = db.scalars(select(Lancamento)).one()
    client.post(f"/lancamentos/{lanc.id}", data={
        "valor": "2*10", "descricao": "Padaria", "data": "2026-10-02", "conta_id": str(conta.id),
    })
    db.refresh(lanc)
    assert lanc.valor == -2000


def test_favoritar_alterna_e_devolve_botao(client, db):
    conta, cat = _base(db)
    corpo = {"tipo": "despesa", "descricao": "Padaria", "categoria_id": str(cat.id), "conta_id": str(conta.id)}
    r1 = client.post("/lancamentos/favoritos", data=corpo)
    assert r1.status_code == 200 and "fill-current" in r1.text
    assert db.scalar(select(func.count(Favorito.id))) == 1
    r2 = client.post("/lancamentos/favoritos", data=corpo)
    assert "fill-current" not in r2.text and "heart" in r2.text
    assert db.scalar(select(func.count(Favorito.id))) == 0


def test_favorito_descricao_vazia_422(client, db):
    _base(db)
    assert client.post("/lancamentos/favoritos", data={"tipo": "despesa", "descricao": ""}).status_code == 422


def test_modal_mostra_chips_do_tipo_com_dados(client, db):
    conta, cat = _base(db)
    for tipo, desc in (("despesa", "Padaria"), ("receita", "Salário")):
        client.post("/lancamentos/favoritos", data={
            "tipo": tipo, "descricao": desc, "categoria_id": str(cat.id), "conta_id": str(conta.id)})
    texto = client.get("/lancamentos/novo?tipo=despesa").text
    assert 'data-favorito' in texto and 'data-descricao="Padaria"' in texto
    assert f'data-categoria="{cat.id}"' in texto and f'data-conta="{conta.id}"' in texto
    assert "Salário" not in texto


def test_mais_detalhes_fecha_por_padrao_e_abre_com_conteudo(client, db):
    _base(db)
    html = client.get("/lancamentos/novo?tipo=receita").text
    assert "Mais detalhes" in html and 'name="observacao"' in html and "Receita fixa" in html and "Repetir" in html
    assert "detalhes: false" in html
    assert "Lança o mesmo valor" not in client.get("/lancamentos/novo?tipo=transferencia").text


def test_repetir_receita_pelo_formulario_com_observacao(client, db):
    conta, _ = _base(db)
    r = client.post("/lancamentos", data=_dados(conta, tipo="receita", valor="500,00", descricao="Aula fictícia",
                                                repetir="repetida", parcelas="3", observacao="nota fictícia"))
    assert r.headers["HX-Refresh"] == "true"
    lancs = db.scalars(select(Lancamento).order_by(Lancamento.data)).all()
    assert [(l.valor, l.parcela_num, l.observacao) for l in lancs] == [
        (50000, 1, "nota fictícia"), (50000, 2, "nota fictícia"), (50000, 3, "nota fictícia")]


def test_erro_reabre_mais_detalhes_mantendo_o_que_foi_digitado(client, db):
    conta, _ = _base(db)
    r = client.post("/lancamentos", data=_dados(conta, repetir="repetida", parcelas="1", observacao="nota"))
    assert r.status_code == 422 and "2 vezes" in r.text
    assert "detalhes: true" in r.text and ">nota</textarea>" in r.text
