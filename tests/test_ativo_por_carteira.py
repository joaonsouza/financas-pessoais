"""Detalhe do ativo aberto de uma carteira: só as operações (e a posição) dela."""
from datetime import date
from decimal import Decimal as D

from app.models import Ativo, Carteira, OperacaoInvest
from app.services.invest_carteira import garantir_carteira_padrao
from tests.factories import nova_conta
from tests.test_rotas_investimentos import mercado  # noqa: F401


def _cenario(db):
    nova_conta(db)
    joao = garantir_carteira_padrao(db)
    ana = Carteira(nome="Carteira da Ana")
    gbtc = Ativo(classe="etf", codigo="GBTC11", simbolo_cotacao="GBTC11.SA", alocacao="cripto")
    db.add_all([ana, gbtc])
    db.flush()
    db.add(OperacaoInvest(ativo_id=gbtc.id, tipo="compra", data=date(2026, 9, 1), quantidade=D(7), preco_unitario=2900,
                          carteira_id=ana.id))
    db.commit()
    return joao, ana, gbtc


def test_da_outra_carteira_nao_mostra_a_operacao(client, db, mercado):  # noqa: F811
    joao, ana, gbtc = _cenario(db)
    texto = client.get(f"/investimentos/ativos/{gbtc.id}?carteira={joao.id}").text
    assert "01/09/2026" not in texto and "Minha carteira" in texto
    assert f'href="/investimentos/ativos/{gbtc.id}"' in texto                       # ver de todas as carteiras
    assert f'href="/investimentos?carteira={joao.id}"' in texto                     # volta para a carteira
    assert "01/09/2026" in client.get(f"/investimentos/ativos/{gbtc.id}?carteira={ana.id}").text
    assert "01/09/2026" in client.get(f"/investimentos/ativos/{gbtc.id}").text     # sem carteira: todas


def test_links_da_carteira_levam_a_carteira(client, db, mercado):  # noqa: F811
    joao, ana, gbtc = _cenario(db)
    texto = client.get(f"/investimentos?carteira={ana.id}").text
    assert f'href="/investimentos/ativos/{gbtc.id}?carteira={ana.id}"' in texto
