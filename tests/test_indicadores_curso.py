from datetime import date

from sqlalchemy import select

from app.models import Categoria, Fatura, set_config
from app.models import dispensar_alerta
from app.services.categorias_padrao import semear_categorias
from app.services.indicadores_curso import _carregar_mes, _grupo, _soma_por, alertas, get_mapa_indicadores, painel_503020
from app.services.orcamento import definir_limite
from tests.factories import nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 10, 5)
COMP = "2026-10"


def _cat(db, nome):
    return db.scalar(select(Categoria).where(Categoria.nome == nome, Categoria.tipo == "despesa"))


def _gasto(db, conta, nome, valor, dia=3, **kw):
    return novo_lancamento(db, conta, date(2026, 10, dia), -valor, categoria=_cat(db, nome), descricao=nome, **kw)


def _cenario(db):
    """Receita 5.000; fixos 1.300; variáveis 1.500; dívida 500; sobra 1.700 (cálculo manual no teste)."""
    semear_categorias(db)
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 500000, descricao="Salário")
    for nome, valor in (("Aluguel", 100000), ("Condomínio/IPTU", 20000), ("Energia", 10000),
                        ("Mercado", 60000), ("Comer fora/Delivery", 40000), ("Lazer", 50000),
                        ("Parcelas de empréstimo/financiamento", 50000)):
        _gasto(db, conta, nome, valor)
    return conta


def _grupos(db, comp=COMP):
    return _soma_por(_carregar_mes(db, comp), _grupo)


def _por_mapa(db, chave, comp=COMP):
    return _carregar_mes(db, comp).total_nomes(get_mapa_indicadores(db)[chave])


def test_mes_base_bate_com_calculo_manual(db):
    _cenario(db)
    mes = _carregar_mes(db, COMP)
    g = _grupos(db)
    assert (mes.receitas, g["fixo"], g["variavel"], g["divida"], mes.sobra) == (500000, 130000, 150000, 50000, 170000)
    assert (_por_mapa(db, "moradia"), _por_mapa(db, "alimentacao")) == (120000, 100000)


def test_mes_base_respeita_ignorar_pendente_e_sem_receita(db):
    conta = _cenario(db)
    _gasto(db, conta, "Mercado", 99999, ignorar=True)
    _gasto(db, conta, "Energia", 5000, dia=25, status="pendente")  # projetado inclui pendente
    assert _grupos(db)["fixo"] == 135000
    vazio = _carregar_mes(db, "2026-11")
    assert (vazio.receitas, vazio.sobra) == (0, 0)


def test_mapa_indicadores_editavel(db):
    _cenario(db)
    set_config(db, "mapa_indicadores", {"moradia": ["Energia"], "alimentacao": ["Mercado"]})
    assert (_por_mapa(db, "moradia"), _por_mapa(db, "alimentacao")) == (10000, 60000)


def test_503020_necessidade_desejo_e_terceira_fatia(db):
    _cenario(db)
    p = painel_503020(db, COMP)
    nec, des, ter = p.fatias
    assert (nec.real, nec.pct_real, nec.pct_meta, nec.diferenca) == (190000, 38.0, 50, -60000)
    assert (des.real, des.pct_real, des.pct_meta, des.diferenca) == (90000, 18.0, 30, -60000)
    assert (ter.real, ter.pct_real, ter.pct_meta, ter.diferenca) == (220000, 44.0, 20, 120000)


def test_503020_usa_metas_configuradas_e_sobra_negativa_nao_entra(db):
    from app.models import set_metas_503020

    semear_categorias(db)
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 100000)
    _gasto(db, conta, "Aluguel", 80000)
    _gasto(db, conta, "Lazer", 50000)
    set_metas_503020(db, [60, 20, 20])
    nec, des, ter = painel_503020(db, COMP).fatias
    assert (nec.pct_meta, nec.real, des.real, ter.real) == (60, 80000, 50000, 0)
    assert nec.diferenca == 80000 - 60000


def _chaves(db, comp=COMP):
    return {a.chave for a in alertas(db, comp, HOJE)}


def test_alertas_cenario_sem_aperto_so_avisos_leves(db):
    _cenario(db)
    assert _chaves(db) == {"variaveis_maior_fixos", "moradia_maior_fatia"}  # sem sobra negativa nem saída > receita


def test_alertas_sobra_negativa_saida_maior_e_moradia(db):
    semear_categorias(db)
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 100000, descricao="Salário")
    _gasto(db, conta, "Aluguel", 90000)
    _gasto(db, conta, "Mercado", 30000)
    chaves = _chaves(db)
    assert {"sobra_negativa", "saida_maior_receita", "moradia_maior_fatia"} <= chaves
    assert "variaveis_maior_fixos" not in chaves
    assert _chaves(db, "2026-11") == set()  # mês sem lançamentos não alerta


def test_alerta_tudo_necessidade(db):
    semear_categorias(db)
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 500000)
    _gasto(db, conta, "Aluguel", 100000)
    assert "tudo_necessidade" not in _chaves(db)  # uma categoria só: sem alerta
    _gasto(db, conta, "Energia", 10000)
    assert "tudo_necessidade" in _chaves(db)
    _gasto(db, conta, "Lazer", 1000)
    assert "tudo_necessidade" not in _chaves(db)


def test_alerta_fatura_parcial(db):
    conta = nova_conta(db)
    cartao = novo_cartao(db, pagadora=conta)
    fatura = Fatura(cartao_id=cartao.id, competencia="2026-09", data_fechamento=date(2026, 9, 3),
                    data_vencimento=date(2026, 9, 10), status="paga_parcial")
    db.add(fatura)
    db.flush()
    (a,) = [x for x in alertas(db, COMP, HOJE) if x.chave.startswith("fatura_parcial")]
    assert a.nivel == "perigo" and "rotativo" in a.texto and a.acao_url == f"/cartoes/{cartao.id}"
    fatura.status = "paga"
    assert not [x for x in alertas(db, COMP, HOJE) if x.chave.startswith("fatura_parcial")]


def test_alerta_juros_de_cheque_especial(db):
    conta = nova_conta(db)
    cartao = novo_cartao(db, pagadora=conta)
    novo_lancamento(db, conta, date(2026, 10, 2), -1234, descricao="JUROS LIMITE CONTA", tipo="despesa")
    novo_lancamento(db, conta, date(2026, 10, 2), -56, descricao="IOF", tipo="despesa")
    novo_lancamento(db, cartao, date(2026, 10, 2), -999, descricao="Repasse de IOF", tipo="despesa")
    novo_lancamento(db, conta, date(2026, 10, 2), -777, descricao="Mercado do Zé", tipo="despesa")
    (a,) = [x for x in alertas(db, COMP, HOJE) if x.chave == "juros_cheque_especial"]
    assert "R$ 12,90" in a.texto or "R$ 12,90" in a.texto.replace("\xa0", " ")
    assert "juros_cheque_especial" not in _chaves(db, "2026-11")


def test_alertas_de_orcamento_e_teto(db):
    semear_categorias(db)
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 1000000)
    _gasto(db, conta, "Mercado", 60000)
    definir_limite(db, COMP, _cat(db, "Mercado").id, 50000)
    definir_limite(db, COMP, _cat(db, "Lazer").id, 50000)
    definir_limite(db, COMP, None, 60000)
    chaves = _chaves(db)
    assert f"orcamento:{_cat(db, 'Mercado').id}" in chaves
    assert f"orcamento:{_cat(db, 'Lazer').id}" not in chaves
    assert "teto_estourado" in chaves


def test_alerta_assinatura_sugerida(db):
    conta = novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))
    for mes in (7, 8, 9):
        novo_lancamento(db, conta, date(2026, mes, 10), -3990, descricao="Streaming Plus")
    (a,) = [x for x in alertas(db, COMP, HOJE) if x.chave.startswith("assinatura:")]
    assert a.nivel == "info" and "Streaming Plus" in a.titulo


def test_alertas_dispensados_somem_so_naquele_mes(db):
    _cenario(db)
    assert "variaveis_maior_fixos" in _chaves(db)
    dispensar_alerta(db, COMP, "variaveis_maior_fixos")
    assert "variaveis_maior_fixos" not in _chaves(db)
    # em outro mês o mesmo alerta continua valendo
    conta = nova_conta(db, "Outra")
    novo_lancamento(db, conta, date(2026, 11, 3), -9000, categoria=_cat(db, "Lazer"))
    assert "variaveis_maior_fixos" in _chaves(db, "2026-11")


def test_503020_sem_categoria_fica_fora_dos_desejos(db):
    semear_categorias(db)
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 100000)
    _gasto(db, conta, "Lazer", 10000)
    novo_lancamento(db, conta, date(2026, 10, 3), -30000, descricao="Sem categoria")
    p = painel_503020(db, COMP)
    assert p.fatias[1].real == 10000  # só o Lazer
    assert (p.sem_categoria, p.pct_sem_categoria) == (30000, 75.0)


def test_moradia_soma_as_subcategorias_do_pai(db):
    from app.models import Categoria
    semear_categorias(db)
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 100000)
    moradia = Categoria(nome="Moradia", tipo="despesa")
    db.add(moradia)
    db.flush()
    gas = db.scalar(select(Categoria).where(Categoria.nome == "Gás"))
    gas.pai_id = moradia.id
    novo_lancamento(db, conta, date(2026, 10, 3), -8000, categoria=gas)
    assert _por_mapa(db, "moradia") == 8000
