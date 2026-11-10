import json
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class Config(Base):
    __tablename__ = "config"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(Text)


def get_config(s: Session, chave: str, padrao: Any = None) -> Any:
    item = s.get(Config, chave)
    return json.loads(item.valor) if item else padrao


def set_config(s: Session, chave: str, valor: Any) -> None:
    texto = json.dumps(valor, ensure_ascii=False)
    item = s.get(Config, chave)
    if item:
        item.valor = texto
    else:
        s.add(Config(chave=chave, valor=texto))
    s.flush()
