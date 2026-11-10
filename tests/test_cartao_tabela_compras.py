from datetime import date

from sqlalchemy import select

from app.models import Lancamento
from app.services.faturas import obter_ou_criar_fatura, pagar_fatura
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _fatura(db):
    conta = nova_conta(db, "Nubank")
    cartao = novo_cartao(db, "Cartão Nubank", pagadora=conta, fechamento=3, vencimento=10)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")
    mercado = nova_categoria(db, "Mercado")
    transporte = nova_categoria(db, "Transporte")
    novo_lancamento(db, cartao, date(2026, 9, 10), -5000, categoria=transporte, descricao="Uber", fatura_id=fatura.id)
    novo_lancamento(db, cartao, date(2026, 9, 25), -2000, categoria=mercado, descricao="Padaria", fatura_id=fatura.id)
    novo_lancamento(db, cartao, date(2026, 9, 15), -9000, descricao="Amazon", fatura_id=fatura.id)
    db.commit()
    return cartao


def _ordem(texto: str) -> list[str]:
    nomes = ["Uber", "Padaria", "Amazon"]
    return sorted(nomes, key=lambda n: texto.index(f">{n}<"))


def test_tabela_tem_colunas_e_padrao_data_desc(client, db):
    cartao = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    for coluna in ("Data", "Título", "Categoria", "Valor"):
        assert f">{coluna}<" in texto
    assert _ordem(texto) == ["Padaria", "Amazon", "Uber"]
    assert 'aria-sort="descending"' in texto
    assert "Sem categoria" in texto


def test_ordenar_por_valor(client, db):
    cartao = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10&ordem=valor&dir=desc").text
    assert _ordem(texto) == ["Amazon", "Uber", "Padaria"]
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10&ordem=valor&dir=asc").text
    assert _ordem(texto) == ["Padaria", "Uber", "Amazon"]


def test_ordenar_por_titulo_e_categoria(client, db):
    cartao = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10&ordem=titulo&dir=asc").text
    assert _ordem(texto) == ["Amazon", "Padaria", "Uber"]
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10&ordem=categoria&dir=asc").text
    # sem categoria vai para o fim
    assert _ordem(texto) == ["Padaria", "Uber", "Amazon"]


def test_cabecalho_alterna_direcao_e_mantem_fatura(client, db):
    cartao = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    # data já ativa em desc: clicar inverte; outra coluna começa no padrão dela
    assert 'href="?fatura=2026-10&amp;ordem=data&amp;dir=asc"' in texto
    assert 'href="?fatura=2026-10&amp;ordem=valor&amp;dir=desc"' in texto
    assert 'href="?fatura=2026-10&amp;ordem=titulo&amp;dir=asc"' in texto


def test_navegacao_entre_faturas_mantem_ordem(client, db):
    cartao = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10&ordem=valor&dir=asc").text
    assert 'href="?fatura=2026-09&amp;ordem=valor&amp;dir=asc"' in texto


def test_ordem_invalida_cai_no_padrao(client, db):
    cartao = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10&ordem=xyz&dir=lado").text
    assert _ordem(texto) == ["Padaria", "Amazon", "Uber"]


def test_cada_compra_tem_editar_e_excluir(client, db):
    cartao = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    for lanc in db.scalars(select(Lancamento).where(Lancamento.conta_id == cartao.id)):
        assert f'hx-get="/lancamentos/{lanc.id}/editar"' in texto
        assert f'hx-post="/lancamentos/{lanc.id}/excluir"' in texto


def test_compra_parcelada_pergunta_o_alcance_ao_excluir(client, db):
    cartao = _fatura(db)
    client.post("/lancamentos", data={
        "tipo": "cartao", "valor": "300,00", "descricao": "TV", "data": "2026-09-20", "conta_id": str(cartao.id),
        "repetir": "parcelada", "parcelas": "3"})
    tv = db.scalars(select(Lancamento).where(Lancamento.descricao == "TV").order_by(Lancamento.data)).first()
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    assert f'hx-get="/lancamentos/{tv.id}/excluir"' in texto


def _fatura_paga(db):
    cartao = _fatura(db)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")
    pagar_fatura(db, fatura, 16000, date(2026, 10, 10))
    db.commit()
    uber = db.scalar(select(Lancamento).where(Lancamento.descricao == "Uber"))
    return cartao, uber


def test_fatura_paga_nao_oferece_excluir(client, db):
    cartao, uber = _fatura_paga(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    assert f'hx-get="/lancamentos/{uber.id}/editar"' in texto
    assert f'/lancamentos/{uber.id}/excluir"' not in texto


def test_fatura_paga_recusa_excluir_compra(client, db):
    _, uber = _fatura_paga(db)
    assert client.post(f"/lancamentos/{uber.id}/excluir").status_code == 422
    assert db.get(Lancamento, uber.id) is not None


def test_fatura_paga_trava_valor_mas_deixa_mudar_categoria(client, db):
    cartao, uber = _fatura_paga(db)
    assert "Fatura já paga" in client.get(f"/lancamentos/{uber.id}/editar").text
    base = {"tipo": "cartao", "descricao": "Uber", "data": "2026-09-10", "conta_id": str(cartao.id),
            "fatura": "2026-10"}
    assert client.post(f"/lancamentos/{uber.id}", data={**base, "valor": "70,00"}).status_code == 422
    db.expire_all()
    assert db.get(Lancamento, uber.id).valor == -5000
    outra = nova_categoria(db, "Apps")
    db.commit()
    resposta = client.post(f"/lancamentos/{uber.id}", data={**base, "valor": "50,00", "categoria_id": str(outra.id)})
    assert resposta.status_code == 200
    db.expire_all()
    assert db.get(Lancamento, uber.id).categoria_id == outra.id
