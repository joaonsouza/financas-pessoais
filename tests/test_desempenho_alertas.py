from datetime import date
from app.models import Conta, Fatura, Lancamento, dispensar_alerta
from app.services.desempenho.alertas_previstos import (
    alertas_previstos,
    central_alertas,
)
from app.services.faturas import obter_ou_criar_fatura
from app.services.orcamento import definir_limite
from tests.factories import nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 10, 15)


def test_alerta_saldo_negativo_agendado(db):
    conta = nova_conta(db, "Conta", saldo=50000, inicio=date(2026, 10, 1))
    # Compromisso agendado de 80000 em 2026-10-25 -> saldo cai para -30000
    novo_lancamento(db, conta, date(2026, 10, 25), -80000, status="pendente")

    alts = alertas_previstos(db, "2026-10", HOJE)
    chaves = [a.chave for a in alts]
    assert any(k.startswith("saldo_negativo_agendado:") for k in chaves)


def test_alerta_ritmo_teto(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 10, 1))
    # Teto de R$ 1.000,00 (100000 centavos)
    definir_limite(db, "2026-10", None, 100000)

    # No dia 15 de 31, já gastou 80000 -> ritmo linear = (80000/15)*31 = ~165.333 > 100.000
    novo_lancamento(db, conta, date(2026, 10, 10), -80000, tipo="despesa")

    alts = alertas_previstos(db, "2026-10", HOJE)
    chaves = [a.chave for a in alts]
    assert "ritmo_teto" in chaves


def test_alerta_fatura_alta(db):
    pagadora = nova_conta(db, "Corrente", saldo=100000, inicio=date(2026, 6, 1))
    cartao = novo_cartao(db, "Cartão", pagadora=pagadora, fechamento=3, vencimento=10)

    # 3 faturas anteriores com média R$ 200,00 (20000 centavos)
    for comp in ("2026-07", "2026-08", "2026-09"):
        fat = obter_ou_criar_fatura(db, cartao, comp)
        novo_lancamento(db, cartao, date(int(comp[:4]), int(comp[5:]), 1), -20000, fatura_id=fat.id)

    # Próxima fatura (2026-10) com R$ 350,00 (+75% e +R$ 150)
    fat_prox = obter_ou_criar_fatura(db, cartao, "2026-10")
    novo_lancamento(db, cartao, date(2026, 10, 1), -35000, fatura_id=fat_prox.id)

    alts = alertas_previstos(db, "2026-10", HOJE)
    chave_esperada = f"fatura_alta:{cartao.id}:2026-10"
    assert any(a.chave == chave_esperada for a in alts)


def test_central_alertas_dispensar(db):
    conta = nova_conta(db, "Conta", saldo=50000, inicio=date(2026, 10, 1))
    novo_lancamento(db, conta, date(2026, 10, 25), -80000, status="pendente")

    alts = central_alertas(db, "2026-10", HOJE)
    chave_alerta = next(a.chave for a in alts if a.chave.startswith("saldo_negativo_agendado"))

    # Dispensa o alerta
    dispensar_alerta(db, "2026-10", chave_alerta)
    db.commit()

    alts_depois = central_alertas(db, "2026-10", HOJE)
    assert not any(a.chave == chave_alerta for a in alts_depois)
