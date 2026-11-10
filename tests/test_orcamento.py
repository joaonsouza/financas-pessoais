from datetime import date

from sqlalchemy import select

from app.models import Orcamento, set_metas_503020
from app.services.orcamento import (
    copiar_orcamento, definir_limite, faixa_de, gasto_categoria, painel_orcamento, teto_mes,
)
from tests.factories import nova_categoria, nova_conta, novo_lancamento

OUT = "2026-10"


def test_faixas_em_80_e_100():
    assert faixa_de(7999, 10000) == "ok"
    assert faixa_de(8000, 10000) == "atencao"
    assert faixa_de(9999, 10000) == "atencao"
    assert faixa_de(10000, 10000) == "estourado"
    assert faixa_de(5, 0) == "estourado"


def test_gasto_categoria_inclui_subcategorias_e_separa_efetivado_de_projetado(db):
    conta = nova_conta(db)
    pai = nova_categoria(db, "Mercado")
    filha = nova_categoria(db, "Feira", pai=pai)
    outra = nova_categoria(db, "Lazer")
    novo_lancamento(db, conta, date(2026, 10, 2), -10000, categoria=pai)
    novo_lancamento(db, conta, date(2026, 10, 3), -5000, categoria=filha)
    novo_lancamento(db, conta, date(2026, 10, 20), -7000, categoria=pai, status="pendente")
    novo_lancamento(db, conta, date(2026, 10, 4), -9999, categoria=pai, ignorar=True)
    novo_lancamento(db, conta, date(2026, 10, 5), -1111, categoria=pai, status="cancelado")
    novo_lancamento(db, conta, date(2026, 10, 6), -3000, categoria=outra)
    novo_lancamento(db, conta, date(2026, 9, 30), -4000, categoria=pai)
    assert gasto_categoria(db, OUT, pai.id) == (15000, 22000)
    assert gasto_categoria(db, OUT, filha.id) == (5000, 5000)


def test_painel_orcamento_linhas_e_faixas(db):
    conta = nova_conta(db)
    mercado = nova_categoria(db, "Mercado")
    lazer = nova_categoria(db, "Lazer")
    casa = nova_categoria(db, "Casa")
    novo_lancamento(db, conta, date(2026, 10, 2), -85000, categoria=mercado)
    novo_lancamento(db, conta, date(2026, 10, 3), -30000, categoria=lazer)
    novo_lancamento(db, conta, date(2026, 10, 4), -1000, categoria=casa)
    for cat, lim in ((mercado, 100000), (lazer, 30000), (casa, 50000)):
        definir_limite(db, OUT, cat.id, lim)
    linhas = {l.categoria.nome: l for l in painel_orcamento(db, OUT)}
    assert (linhas["Mercado"].pct, linhas["Mercado"].faixa) == (85, "atencao")
    assert (linhas["Lazer"].pct, linhas["Lazer"].faixa) == (100, "estourado")
    assert (linhas["Casa"].pct, linhas["Casa"].faixa) == (2, "ok")
    assert linhas["Mercado"].efetivado == 85000 and linhas["Mercado"].limite == 100000


def test_teto_total_usa_todas_as_despesas(db):
    conta = nova_conta(db)
    cat = nova_categoria(db, "Mercado")
    novo_lancamento(db, conta, date(2026, 10, 2), -40000, categoria=cat)
    novo_lancamento(db, conta, date(2026, 10, 3), -10000)  # sem categoria também conta
    novo_lancamento(db, conta, date(2026, 10, 25), -50000, status="pendente")
    assert teto_mes(db, OUT) is None
    definir_limite(db, OUT, None, 100000)
    teto = teto_mes(db, OUT)
    assert teto.categoria is None
    assert (teto.efetivado, teto.projetado, teto.pct, teto.faixa) == (50000, 100000, 100, "estourado")
    assert painel_orcamento(db, OUT) == []  # o teto não entra na lista de categorias


def test_definir_limite_atualiza_e_zero_remove(db):
    cat = nova_categoria(db, "Mercado")
    definir_limite(db, OUT, cat.id, 1000)
    definir_limite(db, OUT, cat.id, 2000)
    assert db.scalar(select(Orcamento.limite).where(Orcamento.categoria_id == cat.id)) == 2000
    definir_limite(db, OUT, cat.id, 0)
    assert db.scalar(select(Orcamento).where(Orcamento.categoria_id == cat.id)) is None


def test_copiar_orcamento_nao_sobrescreve(db):
    a, b, c = (nova_categoria(db, n) for n in ("A", "B", "C"))
    for cat, lim in ((a, 100), (b, 200), (c, 300)):
        definir_limite(db, "2026-09", cat.id, lim)
    definir_limite(db, "2026-09", None, 9000)
    definir_limite(db, OUT, b.id, 777)  # já existe: preservado
    assert copiar_orcamento(db, "2026-09", OUT) == 3  # A, C e o teto
    limites = {o.categoria_id: o.limite for o in db.scalars(select(Orcamento).where(Orcamento.competencia == OUT))}
    assert limites == {a.id: 100, b.id: 777, c.id: 300, None: 9000}
    assert copiar_orcamento(db, "2026-09", OUT) == 0


def _receita(db, valor=1_000_000):
    novo_lancamento(db, nova_conta(db), date(2026, 10, 1), valor, descricao="Salário")


def test_teto_automatico_e_80_por_cento_das_receitas(db):
    _receita(db)
    teto = teto_mes(db, OUT)
    assert (teto.limite, teto.automatico, teto.categoria) == (800_000, True, None)
    assert db.scalar(select(Orcamento)) is None  # não é salvo


def test_teto_automatico_segue_a_terceira_meta(db):
    _receita(db)
    set_metas_503020(db, [60, 30, 10])
    assert teto_mes(db, OUT).limite == 900_000


def test_teto_automatico_sem_receitas_e_none(db):
    novo_lancamento(db, nova_conta(db), date(2026, 10, 2), -10_000)
    assert teto_mes(db, OUT) is None


def test_teto_salvo_vale_mais_que_o_automatico(db):
    _receita(db)
    definir_limite(db, OUT, None, 123_000)
    teto = teto_mes(db, OUT)
    assert (teto.limite, teto.automatico) == (123_000, False)
    definir_limite(db, OUT, None, 0)
    assert teto_mes(db, OUT).automatico is True


def test_copiar_orcamento_nao_copia_o_teto_automatico(db):
    _receita(db)
    cat = nova_categoria(db, "A")
    definir_limite(db, "2026-09", cat.id, 100)
    assert copiar_orcamento(db, "2026-09", OUT) == 1
    assert db.scalar(select(Orcamento).where(Orcamento.categoria_id.is_(None))) is None


def test_limite_de_subcategoria_so_conta_o_gasto_dela(db):
    conta = nova_conta(db)
    pai = nova_categoria(db, "Mercado")
    filha = nova_categoria(db, "Feira", pai=pai)
    novo_lancamento(db, conta, date(2026, 10, 2), -10_000, categoria=pai)
    novo_lancamento(db, conta, date(2026, 10, 3), -5_000, categoria=filha)
    definir_limite(db, OUT, filha.id, 20_000)
    (linha,) = painel_orcamento(db, OUT)
    assert (linha.categoria.id, linha.efetivado, linha.limite) == (filha.id, 5_000, 20_000)
