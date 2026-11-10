from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.quantidade import DecimalTexto
from app.db import Base


class ProventoCache(Base):
    """Provento por cota de um símbolo (dividendo, JCP, rendimento de FII), na data ex, como o provedor informa."""

    __tablename__ = "provento_cache"
    __table_args__ = (UniqueConstraint("simbolo", "data_ex", name="uq_provento_simbolo_data"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    simbolo: Mapped[str] = mapped_column(String(40))
    data_ex: Mapped[date]  # quem tinha a cota no pregão anterior recebe
    valor: Mapped[Decimal] = mapped_column(DecimalTexto)  # por cota, na moeda da cotação (unidades, não centavos)
    atualizado_em: Mapped[datetime]
