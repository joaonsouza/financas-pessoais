from datetime import date
from types import SimpleNamespace
import pytest

from app.models import Categoria, Conta, Lancamento
from app.services.desempenho.analise_ia import (
    ItemSugestaoIA,
    SaidaAnaliseIA,
    obter_analise_ia,
)
from app.services.desempenho.insights import gerar_insights
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 15)


def test_insights_categoria_acima_da_media(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 6, 1))
    cat = nova_categoria(db, "Lazer", grupo="variavel")

    # Média anterior: 10000 nos meses 07, 08, 09
    for m in ("2026-07", "2026-08", "2026-09"):
        novo_lancamento(db, conta, date(int(m[:4]), int(m[5:]), 5), -10000, categoria=cat)

    # Mês 10: 25000 (+150% e +15000)
    novo_lancamento(db, conta, date(2026, 10, 5), -25000, categoria=cat)

    ins = gerar_insights(db, "2026-10", HOJE)
    chaves = [i.chave for i in ins]
    assert f"cat_acima:{cat.id}" in chaves


def test_insights_gastos_sem_categoria(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 10, 1))
    # Despesa categorizada de 20000 e sem categoria de 15000 (>= 5% e >= 5000)
    cat = nova_categoria(db, "Alimentação", grupo="variavel")
    novo_lancamento(db, conta, date(2026, 10, 2), -20000, categoria=cat)
    novo_lancamento(db, conta, date(2026, 10, 3), -15000)

    ins = gerar_insights(db, "2026-10", HOJE)
    chaves = [i.chave for i in ins]
    assert "gastos_sem_categoria" in chaves


class ClienteIAFalso:
    def __init__(self, resposta):
        self._resposta = resposta
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        return self._resposta


def test_analise_ia_geracao_e_cache(db, monkeypatch):
    conta = nova_conta(db, "Conta", inicio=date(2026, 10, 1))
    novo_lancamento(db, conta, date(2026, 10, 2), 500000, tipo="receita")
    novo_lancamento(db, conta, date(2026, 10, 5), -200000, tipo="despesa")

    saida_mock = SaidaAnaliseIA(
        resumo="Mês excelente com boa sobra financeira.",
        pontos_fortes=["Sobra consistente", "Dentro do planejado"],
        pontos_de_atencao=["Acompanhar variáveis"],
        sugestoes=[ItemSugestaoIA(acao="Aportar na reserva", impacto_mensal_estimado=30000)],
    )
    cliente = ClienteIAFalso(SimpleNamespace(stop_reason="end_turn", parsed_output=saida_mock))

    # 1. Primeira chamada gera e salva em cache
    res = obter_analise_ia(db, "2026-10", HOJE, forcar=True, cliente=cliente, modelo="modelo-teste")
    assert res is not None
    assert res.resumo == "Mês excelente com boa sobra financeira."
    assert res.desatualizado is False

    # 2. Segunda chamada (sem cliente injetado) deve vir do cache
    res_cache = obter_analise_ia(db, "2026-10", HOJE, forcar=False)
    assert res_cache is not None
    assert res_cache.resumo == res.resumo
    assert res_cache.desatualizado is False

    # 3. Adiciona um lançamento: o hash muda, o cache continua mas indica desatualizado
    novo_lancamento(db, conta, date(2026, 10, 8), -50000, tipo="despesa")
    res_desatualizado = obter_analise_ia(db, "2026-10", HOJE, forcar=False)
    assert res_desatualizado is not None
    assert res_desatualizado.desatualizado is True
