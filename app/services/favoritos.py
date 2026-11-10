from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Favorito

LIMITE_CHIPS = 8


def _buscar(s: Session, tipo: str, descricao: str) -> Favorito | None:
    return s.scalar(select(Favorito).where(Favorito.tipo == tipo, Favorito.descricao == descricao))


def eh_favorito(s: Session, tipo: str, descricao: str) -> bool:
    return _buscar(s, tipo, descricao.strip()) is not None


def alternar_favorito(s: Session, tipo: str, descricao: str, categoria_id: int | None,
                      conta_id: int | None) -> bool:
    """Cria ou remove o favorito. Devolve True se, ao final, a descrição é favorita."""
    descricao = descricao.strip()
    if not descricao:
        raise ValueError("informe a descrição para favoritar")
    existente = _buscar(s, tipo, descricao)
    if existente:
        s.delete(existente)
        s.flush()
        return False
    s.add(Favorito(tipo=tipo, descricao=descricao, categoria_id=categoria_id, conta_id=conta_id))
    s.flush()
    return True


def listar_favoritos(s: Session, tipo: str, limite: int = LIMITE_CHIPS) -> list[Favorito]:
    consulta = select(Favorito).where(Favorito.tipo == tipo).order_by(Favorito.id).limit(limite)
    return list(s.scalars(consulta))
