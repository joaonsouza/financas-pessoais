from datetime import date

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Fatura(Base):
    __tablename__ = "fatura"
    __table_args__ = (UniqueConstraint("cartao_id", "competencia", name="uq_fatura_cartao_mes"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cartao_id: Mapped[int] = mapped_column(ForeignKey("conta.id"), index=True)
    competencia: Mapped[str] = mapped_column(String(7))
    data_fechamento: Mapped[date]
    data_vencimento: Mapped[date]
    status: Mapped[str] = mapped_column(String(12), default="aberta")  # aberta | paga | paga_parcial
    total_informado: Mapped[int | None]  # total lido do PDF (Plano 2)
    # fatura aberta sem arquivo (o C6 só exporta a fechada): o total que o app do banco mostra, digitado à mão.
    # O que ainda não foi lançado conta como "a detalhar" até a importação da fatura fechada trazer as compras.
    total_parcial: Mapped[int | None]
    total_parcial_em: Mapped[date | None]
