from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core import relogio
from app.core.periodo import competencia_de, validar_competencia


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.fabrica_sessao() as s:
        yield s


def get_hoje() -> date:
    return relogio.hoje()


def get_agora() -> datetime:
    return relogio.agora().replace(tzinfo=None)


def mes_selecionado(request: Request, hoje: date = Depends(get_hoje)) -> str:
    mes = request.query_params.get("mes")
    if mes:
        try:
            return validar_competencia(mes)
        except ValueError:
            pass
    return competencia_de(hoje)


@dataclass
class Provedores:
    """Provedores de mercado injetáveis (`None` = padrão de rede real). Os testes sobrescrevem `get_provedores`."""

    yahoo: object | None = None
    tesouro: object | None = None
    brapi: object | None = None
    bcb: object | None = None


def get_provedores() -> Provedores:
    return Provedores()
