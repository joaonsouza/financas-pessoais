from sqlalchemy import text

from app.db import Base, criar_engine, migrar


def test_migrar_adiciona_coluna_faltante_com_default():
    engine = criar_engine("sqlite://")
    with engine.begin() as con:
        con.execute(text(
            "CREATE TABLE lancamento (id INTEGER PRIMARY KEY, conta_id INTEGER, data DATE, descricao VARCHAR(200),"
            " valor INTEGER, tipo VARCHAR(20), status VARCHAR(10), criado_em DATETIME)"
        ))
        con.execute(text(
            "INSERT INTO lancamento (id, conta_id, data, descricao, valor, tipo, status)"
            " VALUES (1, 1, '2026-10-01', 'Antigo', -100, 'despesa', 'efetivado')"
        ))
    migrar(engine)
    with engine.connect() as con:
        colunas = {linha[1] for linha in con.execute(text("PRAGMA table_info(lancamento)"))}
        assert "ignorar" in colunas
        assert con.execute(text("SELECT ignorar FROM lancamento WHERE id = 1")).scalar() == 0
    engine.dispose()


def test_migrar_e_idempotente_e_nao_mexe_em_banco_novo():
    engine = criar_engine("sqlite://")
    Base.metadata.create_all(engine)
    migrar(engine)
    migrar(engine)
    engine.dispose()
