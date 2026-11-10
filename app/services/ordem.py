"""Ordem de contas e cartões escolhida arrastando (`Conta.ordem`); sem posição, ficam depois, no desempate."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Conta


def criterio_ordem(*desempate):
    """`order_by` das contas: as com posição primeiro, na posição; as outras depois, pelo desempate."""
    return (Conta.ordem.is_(None), Conta.ordem, *desempate)


def reordenar(atuais: list[Conta], ids: list[int]) -> None:
    """Grava a ordem de `ids`; as de `atuais` que não vieram ficam depois, na ordem em que estavam."""
    por_id = {c.id: c for c in atuais}
    na_ordem = [por_id[i] for i in dict.fromkeys(ids) if i in por_id]
    na_ordem += [c for c in atuais if c not in na_ordem]
    for posicao, conta in enumerate(na_ordem):
        conta.ordem = posicao


def proxima_ordem(s: Session, *, cartao: bool) -> int | None:
    """Posição de uma conta/cartão novo: no fim, se o João já ordenou; senão None (segue o desempate)."""
    tipo = Conta.tipo == "cartao" if cartao else Conta.tipo != "cartao"
    maior = s.scalar(select(func.max(Conta.ordem)).where(tipo))
    return None if maior is None else maior + 1
