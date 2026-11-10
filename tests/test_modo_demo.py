"""Modo demonstração: faixa, sem backup no destino real e sem IA. Dados fictícios."""
from pathlib import Path

import pytest

from app.importers.base import LeitorErro
from app.main import executar_backup_se_preciso
from app.services import ia
from tests.factories import nova_conta


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("FINANCAS_DEMO", "1")


def test_faixa_so_no_modo_demo(client, db, monkeypatch):
    nova_conta(db)
    db.commit()
    assert "dados fictícios" not in client.get("/").text
    monkeypatch.setenv("FINANCAS_DEMO", "1")
    assert "Demonstração · dados fictícios" in client.get("/").text


def test_sem_backup_no_modo_demo(demo, app_teste, tmp_path, monkeypatch):
    chamadas = []
    monkeypatch.setattr("app.main.fazer_backup", lambda *a: chamadas.append(a))
    monkeypatch.setattr("app.main.precisa_backup", lambda *a: True)
    executar_backup_se_preciso(app_teste.state.fabrica_sessao, Path(tmp_path / "demo.db"))
    assert chamadas == []


def test_sem_ia_no_modo_demo(demo, db):
    with pytest.raises(LeitorErro, match="demonstração"):
        ia.criar_cliente()
    with pytest.raises(LeitorErro, match="demonstração"):
        ia.cliente_e_modelo_de_texto(db)
