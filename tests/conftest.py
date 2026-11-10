from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (registra as tabelas no metadata)
from app.db import Base, criar_engine
from app.main import criar_app
from app.web.deps import get_hoje

HOJE_TESTE = date(2026, 10, 5)


@pytest.fixture
def engine():
    eng = criar_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    fabrica = sessionmaker(engine, expire_on_commit=False)
    with fabrica() as s:
        yield s


@pytest.fixture
def app_teste():
    aplicacao = criar_app("sqlite://", rotinas=False)
    aplicacao.dependency_overrides[get_hoje] = lambda: HOJE_TESTE
    yield aplicacao
    aplicacao.state.engine.dispose()


@pytest.fixture
def client(app_teste):
    with TestClient(app_teste, base_url="http://127.0.0.1") as c:
        yield c


@pytest.fixture
def db(app_teste):
    with app_teste.state.fabrica_sessao() as s:
        yield s


@pytest.fixture(autouse=True)
def keyring_falso(monkeypatch):
    """Nenhum teste toca o Credential Manager real: o keyring vira um dicionário em memória."""
    import keyring
    from keyring.errors import PasswordDeleteError

    cofre: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda servico, usuario: cofre.get((servico, usuario)))
    monkeypatch.setattr(keyring, "set_password", lambda servico, usuario, senha: cofre.__setitem__((servico, usuario), senha))

    def apagar(servico, usuario):
        if (servico, usuario) not in cofre:
            raise PasswordDeleteError("não encontrada")
        del cofre[(servico, usuario)]

    monkeypatch.setattr(keyring, "delete_password", apagar)
    return cofre


@pytest.fixture(autouse=True)
def horizonte_curto(monkeypatch):
    """Os testes de recorrência assumem a janela curta (mês atual + 2); o padrão real (36) tem teste próprio."""
    import app.services.recorrencia as recorrencia
    monkeypatch.setattr(recorrencia, "HORIZONTE_PADRAO", 2)
