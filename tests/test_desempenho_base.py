from datetime import date
import pytest
from app.models import Categoria, Conta, Fatura, Lancamento, Meta
from app.services.desempenho.base import (
    carregar_totais_periodo,
    janela_historico,
    meses_media_referencia,
    totais_do_mes,
)
from app.services.desempenho.kpis import calcular_kpis
from app.services.desempenho.placar import calcular_placar
from app.services.orcamento import definir_limite
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 15)


def test_janela_historico_sem_meses_vazios_anteriores(db):
    assert janela_historico(db, "2026-10", 12) == ["2026-10"]

    conta = nova_conta(db, "Conta A", inicio=date(2026, 8, 1))
    novo_lancamento(db, conta, date(2026, 8, 10), 10000)

    # Com lançamento mais antigo em 2026-08, janela de 12 meses terminando em 2026-10 deve ir de 08 a 10
    j = janela_historico(db, "2026-10", 12)
    assert j == ["2026-08", "2026-09", "2026-10"]


def test_meses_media_referencia(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 1, 1))
    novo_lancamento(db, conta, date(2026, 6, 1), 10000)
    novo_lancamento(db, conta, date(2026, 7, 1), 10000)
    # 2026-08 vazio
    novo_lancamento(db, conta, date(2026, 9, 1), 10000)

    # Referência para 2026-10 deve ser [2026-06, 2026-07, 2026-09]
    media_meses = meses_media_referencia(db, "2026-10", n=3)
    assert media_meses == ["2026-06", "2026-07", "2026-09"]


def test_totais_periodo_e_meta_id_nao_entra_em_despesa(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 10, 1))
    cat_fixo = nova_categoria(db, "Aluguel", grupo="fixo")
    cat_var = nova_categoria(db, "Mercado", grupo="variavel")
    meta = Meta(nome="Viagem", tipo="objetivo", valor_alvo=100000)
    db.add(meta)
    db.flush()

    # Receita normal
    novo_lancamento(db, conta, date(2026, 10, 2), 500000, tipo="receita")
    # Despesa fixa
    novo_lancamento(db, conta, date(2026, 10, 5), -150000, categoria=cat_fixo)
    # Despesa variável
    novo_lancamento(db, conta, date(2026, 10, 10), -50000, categoria=cat_var)
    # Despesa com meta_id (dinheiro guardado)
    lanc_meta = novo_lancamento(db, conta, date(2026, 10, 12), -40000, descricao="Aporte meta")
    lanc_meta.meta_id = meta.id
    db.flush()

    totais = totais_do_mes(db, "2026-10")
    assert totais.receitas_projetado == 500000
    # Despesas devem somar apenas 1500 + 500 = 200000 (meta_id NÃO conta como despesa)
    assert totais.despesas_projetado == 200000
    assert totais.guardado_projetado == 40000
    assert totais.fixos_projetado == 150000
    assert totais.variaveis_projetado == 50000
    assert totais.sobra_projetado == 300000
    # Taxa de poupança = (sobra + guardado) / receitas = (3000 + 400) / 5000 = 68.0%
    assert totais.taxa_poupanca_projetado == 68.0


def test_kpis_comparacoes(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 8, 1))
    # Mês 09
    novo_lancamento(db, conta, date(2026, 9, 5), 400000, tipo="receita")
    novo_lancamento(db, conta, date(2026, 9, 6), -200000, tipo="despesa")
    # Mês 10
    novo_lancamento(db, conta, date(2026, 10, 5), 600000, tipo="receita")
    novo_lancamento(db, conta, date(2026, 10, 6), -250000, tipo="despesa")

    kpis = calcular_kpis(db, "2026-10")
    assert kpis.tem_media is True
    assert kpis.meses_media == ["2026-09"]

    # Receitas subiram de 4000 para 6000 (+2000, +50%)
    rec = kpis.receitas
    assert rec.valor_projetado == 600000
    assert rec.delta_anterior.diferenca == 200000
    assert rec.delta_anterior.pct == 50.0
    assert rec.delta_anterior.positivo is True

    # Despesas subiram de 2000 para 2500 (+500, +25%), o que é negativo para o usuário
    desp = kpis.despesas
    assert desp.valor_projetado == 250000
    assert desp.delta_anterior.diferenca == 50000
    assert desp.delta_anterior.pct == 25.0
    assert desp.delta_anterior.positivo is False


def test_placar_do_mes(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 10, 1))
    novo_lancamento(db, conta, date(2026, 10, 2), 500000, tipo="receita")
    novo_lancamento(db, conta, date(2026, 10, 5), -200000, tipo="despesa")
    definir_limite(db, "2026-10", None, 300000)

    placar = calcular_placar(db, "2026-10", HOJE)
    # Sobra: 300000 >= 0 -> atingido
    # Teto: 200000 <= 300000 -> atingido
    # Sem encargos: sem faturas parciais nem juros -> atingido
    # Economia, 503020, Reserva: testam comportamento
    assert placar.competencia == "2026-10"
    itens_por_id = {i.id: i for i in placar.itens}
    assert itens_por_id["sobra"].status == "atingido"
    assert itens_por_id["teto"].status == "atingido"
    assert itens_por_id["sem_encargos"].status == "atingido"
