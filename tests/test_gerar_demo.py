"""Gerador do banco de demonstração: sem rede, num arquivo temporário. Dados fictícios."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.periodo import competencia_de
from app.main import criar_app
from app.models import Fatura, Lancamento, Meta
from app.services.saldos import saldo_liquido, saldos_contas
from scripts.gerar_demo import gerar

HOJE = date(2026, 10, 30)


@pytest.fixture
def demo_app(tmp_path):
    caminho = tmp_path / "demo.db"
    gerar(caminho, semente=7, hoje=HOJE, yahoo=None)
    aplicacao = criar_app(f"sqlite:///{caminho}", rotinas=False)
    yield aplicacao
    aplicacao.state.fabrica_sessao.kw["bind"].dispose()


def test_dados_consistentes(demo_app):
    with demo_app.state.fabrica_sessao() as s:
        saldos = saldos_contas(s, HOJE, competencia_de(HOJE))
        assert saldo_liquido(saldos) > 0
        assert all(x.atual >= 0 for x in saldos if x.tipo != "cartao")
        status = dict(s.execute(select(Fatura.status, func.count()).group_by(Fatura.status)).all())
        assert status.get("paga") and status.get("aberta")
        assert s.scalar(select(func.count(func.distinct(Lancamento.grupo_parcela_id)))) >= 3
        assert s.scalar(select(func.count(Meta.id))) == 2


def test_mesma_semente_mesmos_dados(tmp_path):
    for nome in ("a.db", "b.db"):
        gerar(tmp_path / nome, semente=3, hoje=HOJE, yahoo=None)
    assert (tmp_path / "a.db").stat().st_size == (tmp_path / "b.db").stat().st_size


def test_telas_principais_abrem(demo_app):
    with TestClient(demo_app, base_url="http://127.0.0.1") as client:
        for url in ("/", "/contas", "/cartoes", "/lancamentos", "/investimentos", "/objetivos", "/planejamento", "/desempenho",
                    "/cartoes/parcelas", "/lancamentos/assinaturas"):
            assert client.get(url).status_code == 200, url
