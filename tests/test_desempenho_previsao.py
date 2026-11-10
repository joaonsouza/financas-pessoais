from datetime import date
from app.models import Categoria, Conta, Lancamento, Recorrencia
from app.services.desempenho.previsao import (
    calcular_previsao,
    media_despesas_avulsas,
)
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 15)


def test_media_despesas_avulsas_ignora_recorrencias_e_parcelas(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 8, 1))
    cat_var = nova_categoria(db, "Mercado", grupo="variavel")
    cat_fixo = nova_categoria(db, "Aluguel", grupo="fixo")

    # Mês 09: 1 avulsa de 100, 1 fixa de 200, 1 parcelada de 50
    novo_lancamento(db, conta, date(2026, 9, 2), -10000, categoria=cat_var, descricao="Feira avulsa")
    novo_lancamento(db, conta, date(2026, 9, 3), -20000, categoria=cat_fixo, descricao="Aluguel")
    l_parc = novo_lancamento(db, conta, date(2026, 9, 4), -5000, categoria=cat_var, descricao="Geladeira 1/10")
    l_parc.grupo_parcela_id = "geladeira"
    l_parc.parcela_num = 1
    l_parc.parcela_total = 10
    db.flush()

    media, base = media_despesas_avulsas(db, "2026-10", n=3)
    # Apenas a feira avulsa (10000) deve entrar na média
    assert media == 10000


def test_calcular_previsao_agendado_vs_estimado(db):
    conta = nova_conta(db, "Conta", saldo=1000000, inicio=date(2026, 9, 1))
    cat_var = nova_categoria(db, "Mercado", grupo="variavel")
    # No mês 09 teve 20000 de despesa avulsa
    novo_lancamento(db, conta, date(2026, 9, 10), -20000, categoria=cat_var)

    # No mês 10 temos despesa agendada de 5000
    novo_lancamento(db, conta, date(2026, 10, 20), -5000, status="pendente")

    prev = calcular_previsao(db, HOJE, "2026-10", horizonte_meses=3)
    assert prev.horizonte_meses == 3
    assert prev.media_variaveis_avulsas == 20000

    # Pontos mensais: deve ter o passado (2026-09) e os futuros (2026-10, 2026-11, 2026-12)
    pontos_por_mes = {p.competencia: p for p in prev.pontos_mensais}
    assert "2026-09" in pontos_por_mes
    assert "2026-10" in pontos_por_mes
    assert "2026-11" in pontos_por_mes
    assert "2026-12" in pontos_por_mes

    # Mês 10: agendado tem 1000000 - 20000 (do mês 9) - 5000 (do mês 10) = 975000
    # Mês 10 estimado desconta a média (20000) menos o que já foi lançado
    pt_10 = pontos_por_mes["2026-10"]
    assert pt_10.saldo_agendado == 975000
    assert pt_10.saldo_estimado <= pt_10.saldo_agendado

    # Curva diária de 30 dias
    assert len(prev.curva_30_dias) == 31
    assert prev.menor_saldo_30_dias is not None


def test_compromissos_terminando(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 10, 1))
    # Parcela terminando em 2026-11 (parcela 3 de 3)
    l_fim = novo_lancamento(db, conta, date(2026, 11, 15), -15000, descricao="Celular 3/3")
    l_fim.grupo_parcela_id = "celular"
    l_fim.parcela_num = 3
    l_fim.parcela_total = 3

    # Recorrência com término em 2026-12
    rec = Recorrencia(
        descricao="Academia",
        tipo="despesa",
        conta_id=conta.id,
        valor=10000,
        dia=10,
        inicio="2026-01",
        fim="2026-12",
        ativa=True,
    )
    db.add(rec)
    db.flush()

    prev = calcular_previsao(db, HOJE, "2026-10", horizonte_meses=6)
    terminos_desc = {t.descricao: t for t in prev.compromissos_terminando}
    assert "Celular 3/3 (3/3)" in terminos_desc
    assert "Recorrência: Academia" in terminos_desc
    assert prev.liberacao_por_mes.get("2026-11") == 15000
    assert prev.liberacao_por_mes.get("2026-12") == 10000
