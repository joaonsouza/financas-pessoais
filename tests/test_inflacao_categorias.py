from sqlalchemy import select

from app.models import Categoria
from app.services.categorias_padrao import migrar_inflacao_categorias, semear_categorias
from tests.factories import nova_categoria


def test_migracao_marca_pelo_nome_uma_vez_so(db):
    aluguel = nova_categoria(db, "Aluguel")
    energia = nova_categoria(db, "Energia")
    faxina = nova_categoria(db, "Faxina")
    mercado = nova_categoria(db, "Mercado")
    migrar_inflacao_categorias(db)
    assert (aluguel.inflacao, energia.inflacao, faxina.inflacao, mercado.inflacao) == ("fixo", "consumo", "fixo", None)
    aluguel.inflacao = None          # o João tirou na tela
    migrar_inflacao_categorias(db)
    assert aluguel.inflacao is None  # a migração não volta a marcar


def test_receita_com_nome_de_conta_fixa_nao_entra(db):
    receita = nova_categoria(db, "Aluguel", tipo="receita")
    migrar_inflacao_categorias(db)
    assert receita.inflacao is None


def test_categorias_semeadas_ja_vem_marcadas(db):
    semear_categorias(db)
    por_nome = {c.nome: c.inflacao for c in db.scalars(select(Categoria))}
    assert por_nome["Aluguel"] == "fixo" and por_nome["Assinaturas"] == "fixo"
    assert por_nome["Gás"] == "consumo" and por_nome["Água"] == "consumo"
    assert por_nome["Mercado"] is None and por_nome["Salário"] is None
