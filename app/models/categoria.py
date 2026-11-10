from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Categoria(Base):
    __tablename__ = "categoria"
    __table_args__ = (
        UniqueConstraint("nome", "tipo", "pai_id", name="uq_categoria_nome"),
        Index("uq_categoria_raiz", "nome", "tipo", unique=True, sqlite_where=text("pai_id IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(60))
    pai_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id"))
    tipo: Mapped[str] = mapped_column(String(10))  # receita | despesa
    grupo: Mapped[str | None] = mapped_column(String(10))  # fixo | variavel | divida (só despesa)
    natureza: Mapped[str | None] = mapped_column(String(12))  # necessidade | desejo (só despesa)
    essencial: Mapped[bool] = mapped_column(default=False)
    inflacao: Mapped[str | None] = mapped_column(String(8))  # fixo | consumo: entra na inflação pessoal (só despesa)
    icone: Mapped[str] = mapped_column(String(40), default="tag")
    cor: Mapped[str] = mapped_column(String(9), default="#64748b")
    ativa: Mapped[bool] = mapped_column(default=True)
