from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RegraCategoria(Base):
    """Regra "descrição contém padrão" -> categoria, aplicada na importação."""

    __tablename__ = "regra_categoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    padrao: Mapped[str] = mapped_column(String(200))  # comparado com a descrição normalizada
    categoria_id: Mapped[int] = mapped_column(ForeignKey("categoria.id", ondelete="CASCADE"))
    prioridade: Mapped[int] = mapped_column(default=0)
    criada_de: Mapped[str | None] = mapped_column(String(300))  # descrição que originou a regra
