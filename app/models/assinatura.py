from datetime import date

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Assinatura(Base):
    """Assinatura (streaming, nuvem, apps...): só controle. Não gera lançamentos; as cobranças reais vêm do cartão."""

    __tablename__ = "assinatura"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    padrao: Mapped[str] = mapped_column(String(120))  # texto normalizado que reconhece a cobrança no extrato
    valor: Mapped[int]  # centavos, > 0: valor esperado de cada cobrança
    intervalo_meses: Mapped[int] = mapped_column(default=1)  # 1 = mensal, 12 = anual
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id"), index=True)
    dia: Mapped[int | None]
    ativa: Mapped[bool] = mapped_column(default=True)
    inicio: Mapped[date | None]
    cancelada_em: Mapped[date | None]
    observacao: Mapped[str | None] = mapped_column(Text)
