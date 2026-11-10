from datetime import date

import pytest
from sqlalchemy import select

from app.models import Categoria, Favorito, Lancamento, Meta, Orcamento, Recorrencia, RegraCategoria
from app.services.gestao_categorias import (
    apagar_regra, definir_mae, impacto, regras_da_categoria, remover_categoria, trocar_regra, uso_por_categoria,
)
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 28)


@pytest.fixture
def base(db):
    conta = nova_conta(db)
    alim = nova_categoria(db, "Alimentação")
    mercado = nova_categoria(db, "Mercado", pai=alim)
    feira = nova_categoria(db, "Feira", pai=alim)
    moradia = nova_categoria(db, "Moradia")
    salario = nova_categoria(db, "Salário", tipo="receita")
    return conta, alim, mercado, feira, moradia, salario


def _ligar_tudo(db, conta, cat):
    """Um de cada coisa que aponta para a categoria."""
    novo_lancamento(db, conta, date(2026, 10, 1), -1000, categoria=cat)
    db.add_all([
        Recorrencia(descricao="Rec", tipo="despesa", conta_id=conta.id, valor=100, dia=5, inicio="2026-10",
                    categoria_id=cat.id),
        Favorito(tipo="despesa", descricao="Fav", categoria_id=cat.id),
        RegraCategoria(padrao="PADARIA", categoria_id=cat.id),
        Orcamento(competencia="2026-10", categoria_id=cat.id, limite=5000),
        Meta(nome="Meta", tipo="objetivo", categoria_id=cat.id),
    ])
    db.flush()


# ---------- uso ----------

def test_uso_12_meses_so_efetivados_e_mae_soma_filhas(db, base):
    conta, alim, mercado, feira, *_ = base
    novo_lancamento(db, conta, date(2026, 10, 1), -1000, categoria=mercado)
    novo_lancamento(db, conta, date(2026, 9, 1), -500, categoria=feira)
    novo_lancamento(db, conta, date(2026, 9, 2), -200, categoria=alim)
    novo_lancamento(db, conta, date(2026, 10, 2), -9999, categoria=mercado, status="pendente")
    novo_lancamento(db, conta, date(2025, 10, 1), -9999, categoria=mercado)       # mais de 12 meses
    uso = uso_por_categoria(db, HOJE)
    assert uso[mercado.id] == (1, 1000) and uso[feira.id] == (1, 500)
    assert uso[alim.id] == (3, 1700)


# ---------- impacto e remoção ----------

def test_impacto_conta_tudo_que_aponta_para_a_categoria(db, base):
    conta, alim, mercado, *_ = base
    _ligar_tudo(db, conta, mercado)
    i = impacto(db, mercado)
    assert (i.lancamentos, i.recorrencias, i.favoritos, i.regras, i.orcamentos, i.metas, i.filhas) == (1, 1, 1, 1, 1, 1, 0)
    assert impacto(db, alim).filhas == 2


def test_juntar_move_tudo_soma_limite_e_descarta_regra_repetida(db, base):
    conta, alim, mercado, feira, *_ = base
    _ligar_tudo(db, conta, feira)
    db.add_all([RegraCategoria(padrao="PADARIA", categoria_id=mercado.id),
                Orcamento(competencia="2026-10", categoria_id=mercado.id, limite=3000)])
    db.flush()
    feira_id = feira.id
    r = remover_categoria(db, feira, mercado)
    assert r.lancamentos == 1 and r.regras == 1
    assert db.get(Categoria, feira_id) is None
    assert db.scalar(select(Lancamento.categoria_id)) == mercado.id
    assert db.scalar(select(Recorrencia.categoria_id)) == mercado.id
    assert db.scalar(select(Favorito.categoria_id)) == mercado.id
    assert db.scalar(select(Meta.categoria_id)) == mercado.id
    assert [r.padrao for r in db.scalars(select(RegraCategoria))] == ["PADARIA"]       # a repetida foi descartada
    assert [(o.categoria_id, o.limite) for o in db.scalars(select(Orcamento))] == [(mercado.id, 8000)]


def test_juntar_mae_leva_as_filhas(db, base):
    conta, alim, mercado, feira, moradia, _ = base
    remover_categoria(db, alim, moradia)
    assert mercado.pai_id == moradia.id and feira.pai_id == moradia.id


def test_excluir_deixa_sem_categoria_e_apaga_regras_e_limites(db, base):
    conta, alim, mercado, *_ = base
    _ligar_tudo(db, conta, mercado)
    remover_categoria(db, mercado, None)
    assert db.scalar(select(Lancamento.categoria_id)) is None
    assert db.scalar(select(Recorrencia.categoria_id)) is None
    assert db.scalar(select(Meta.categoria_id)) is None
    assert list(db.scalars(select(RegraCategoria))) == [] and list(db.scalars(select(Orcamento))) == []


def test_remocao_recusa_combinacoes_invalidas(db, base):
    conta, alim, mercado, feira, moradia, salario = base
    for origem, destino in ((mercado, salario), (mercado, mercado), (alim, mercado), (alim, None)):
        with pytest.raises(ValueError):
            remover_categoria(db, origem, destino)
    outra = nova_categoria(db, "Aluguel", pai=moradia)
    with pytest.raises(ValueError):
        remover_categoria(db, alim, outra)        # filhas precisam de mãe principal


# ---------- mãe ----------

def test_definir_mae_e_recusas(db, base):
    conta, alim, mercado, feira, moradia, salario = base
    definir_mae(db, mercado, moradia.id)
    assert mercado.pai_id == moradia.id
    definir_mae(db, mercado, None)
    assert mercado.pai_id is None
    for cat, pai in ((alim, moradia.id), (feira, salario.id), (moradia, moradia.id), (feira, feira.id)):
        with pytest.raises(ValueError):
            definir_mae(db, cat, pai)


# ---------- regras ----------

def test_regras_listar_trocar_e_apagar(db, base):
    conta, alim, mercado, feira, moradia, salario = base
    b = RegraCategoria(padrao="ZEBRA", categoria_id=mercado.id)
    a = RegraCategoria(padrao="ATACADAO", categoria_id=mercado.id)
    db.add_all([b, a])
    db.flush()
    assert [r.padrao for r in regras_da_categoria(db, mercado.id)] == ["ATACADAO", "ZEBRA"]
    trocar_regra(db, a, feira.id)
    assert a.categoria_id == feira.id
    with pytest.raises(ValueError):
        trocar_regra(db, a, salario.id)
    apagar_regra(db, b)
    assert [r.padrao for r in regras_da_categoria(db, mercado.id)] == []


def test_texto_do_impacto():
    from app.services.gestao_categorias import Impacto
    assert Impacto().texto() == "nada"
    assert Impacto(lancamentos=1).texto() == "1 lançamento"
    assert Impacto(lancamentos=41, regras=6, metas=1).texto() == "41 lançamentos, 6 regras e 1 meta"
