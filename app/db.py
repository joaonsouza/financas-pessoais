import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def caminho_db_padrao() -> Path:
    if os.environ.get("FINANCAS_DB"):
        return Path(os.environ["FINANCAS_DB"])
    # data/ fica na raiz do repositório e é ignorada pelo git
    pasta = Path(__file__).resolve().parents[1] / "data"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta / "financas.db"


def criar_engine(url: str | None = None) -> Engine:
    url = url or f"sqlite:///{caminho_db_padrao()}"
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if url == "sqlite://":
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _ativar_fk(conexao, _registro):
        conexao.execute("PRAGMA foreign_keys=ON")

    return engine


def _literal_sql(valor) -> str:
    if isinstance(valor, bool):
        return "1" if valor else "0"
    if isinstance(valor, (int, float)):
        return str(valor)
    return "'" + str(valor).replace("'", "''") + "'"


def migrar(engine: Engine) -> None:
    """Adiciona colunas declaradas no modelo que ainda não existem nas tabelas (bancos antigos)."""
    with engine.begin() as con:
        for tabela in Base.metadata.sorted_tables:
            existentes = {linha[1] for linha in con.execute(text(f'PRAGMA table_info("{tabela.name}")'))}
            if not existentes:
                continue  # tabela nova: create_all já cuidou
            for coluna in tabela.columns:
                if coluna.name in existentes:
                    continue
                tipo = coluna.type.compile(dialect=engine.dialect)
                if coluna.server_default is not None:
                    padrao = f" DEFAULT {coluna.server_default.arg}"
                elif coluna.default is not None and coluna.default.is_scalar:
                    padrao = f" DEFAULT {_literal_sql(coluna.default.arg)}"
                else:
                    padrao = ""
                con.execute(text(f'ALTER TABLE "{tabela.name}" ADD COLUMN "{coluna.name}" {tipo}{padrao}'))
