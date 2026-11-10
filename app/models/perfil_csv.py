from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PerfilCsv(Base):
    """Mapeamento de colunas de um CSV genérico, guardado por conta."""

    __tablename__ = "perfil_csv"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id"), unique=True)
    mapeamento: Mapped[dict] = mapped_column(JSON)
