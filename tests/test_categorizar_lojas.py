"""Categorização por lojas conhecidas, aprendizado ao editar e categorização em lote. Dados fictícios."""
from datetime import date

from sqlalchemy import select

from app.models import Categoria, Lancamento, RegraCategoria
from app.services.categorias_padrao import semear_categorias
from app.services.categorizacao import categoria_por_loja, sugerir_categoria
from app.importers.normalizacao import normalizar_descricao as n
from tests.factories import nova_conta, novo_lancamento


def test_lojas_conhecidas():
    casos = {"IFD*GRUPO": "Comer fora/Delivery", "EBN*SPOTIFY": "Assinaturas", "UBER *TRIP": "Combustível/Apps",
             "DROGARIA GAMA": "Farmácia", "LATAMAIR*X": "Lazer", "AUTO POSTO AVENIDA": "Combustível/Apps"}
    for desc, cat in casos.items():
        assert categoria_por_loja(n(desc)) == cat, desc
    for desc in ("IMPOSTO DE RENDA", "TRANSFERENCIA CONTA CORRENTE", "PIX JOAO"):
        assert categoria_por_loja(n(desc)) is None, desc


def test_sugestao_usa_loja_como_ultimo_recurso(session):
    semear_categorias(session)
    session.commit()
    s = sugerir_categoria(session, "IFD*PIZZARIA DO ZE")
    assert (session.get(Categoria, s.categoria_id).nome, s.origem) == ("Comer fora/Delivery", "loja")


def _id(s, nome):
    return s.scalar(select(Categoria.id).where(Categoria.nome == nome))


def test_editar_categoria_aplica_nos_iguais_e_cria_regra(client, db):
    semear_categorias(db)
    conta = nova_conta(db, "Itaú")
    a = novo_lancamento(db, conta, date(2026, 10, 2), -1000, descricao="ROGERIO SOUSA-CT")
    b = novo_lancamento(db, conta, date(2026, 10, 3), -2000, descricao="Rogerio Sousa-CT")
    db.commit()
    lazer = _id(db, "Lazer")
    r = client.post(f"/lancamentos/{a.id}", data={"valor": "10,00", "descricao": "ROGERIO SOUSA-CT",
                                                   "data": "2026-10-02", "categoria_id": str(lazer), "efetivado": "on"})
    assert r.status_code == 200
    db.refresh(b)
    assert b.categoria_id == lazer
    assert db.scalars(select(RegraCategoria)).one().categoria_id == lazer


def test_botao_categorizar_em_lote(client, db):
    semear_categorias(db)
    conta = nova_conta(db, "Itaú")
    novo_lancamento(db, conta, date(2026, 10, 2), -1000, descricao="NETFLIX.COM")
    novo_lancamento(db, conta, date(2026, 10, 2), -1000, descricao="PIX FULANO")
    db.commit()
    assert "2</strong> lançamento(s) sem categoria" in client.get("/configuracoes?aba=categorias").text
    texto = client.post("/configuracoes/categorizar").text
    assert "1 lançamento(s) categorizado(s)" in texto
    assert db.scalar(select(Lancamento.categoria_id).where(Lancamento.descricao == "NETFLIX.COM")) == _id(db, "Assinaturas")
