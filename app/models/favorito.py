from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Favorito(Base):
    """Descrição favorita para lançamento rápido (preenche descrição, categoria e conta)."""

    __tablename__ = "favorito"
    __table_args__ = (UniqueConstraint("tipo", "descricao", name="uq_favorito_tipo_descricao"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tipo: Mapped[str] = mapped_column(String(20))  # receita | despesa | transferencia | cartao
    descricao: Mapped[str] = mapped_column(String(200))
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id", ondelete="SET NULL"))
