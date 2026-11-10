from datetime import date, datetime

from sqlalchemy import Boolean, Column, ForeignKey, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.relogio import agora
from app.db import Base

lancamento_tag = Table(
    "lancamento_tag",
    Base.metadata,
    Column("lancamento_id", ForeignKey("lancamento.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(40), unique=True)


def _agora_sem_fuso() -> datetime:
    return agora().replace(tzinfo=None)


class Lancamento(Base):
    __tablename__ = "lancamento"
    __table_args__ = (
        UniqueConstraint("recorrencia_id", "competencia_recorrencia", "conta_id", name="uq_recorrencia_mes_conta"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id"), index=True)
    data: Mapped[date] = mapped_column(index=True)
    descricao: Mapped[str] = mapped_column(String(200))
    descricao_original: Mapped[str | None] = mapped_column(String(300))
    valor: Mapped[int]  # centavos; negativo = saída
    tipo: Mapped[str] = mapped_column(String(20))  # receita | despesa | transferencia | pagamento_fatura | ajuste
    status: Mapped[str] = mapped_column(String(10), default="pendente")  # pendente | efetivado | cancelado
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id"))
    observacao: Mapped[str | None] = mapped_column(Text)
    fatura_id: Mapped[int | None] = mapped_column(ForeignKey("fatura.id"), index=True)
    recorrencia_id: Mapped[int | None] = mapped_column(ForeignKey("recorrencia.id"))
    competencia_recorrencia: Mapped[str | None] = mapped_column(String(7))
    transferencia_par_id: Mapped[int | None] = mapped_column(ForeignKey("lancamento.id"))
    grupo_parcela_id: Mapped[str | None] = mapped_column(String(32), index=True)
    parcela_num: Mapped[int | None]
    parcela_total: Mapped[int | None]
    origem: Mapped[str] = mapped_column(String(12), default="manual")  # manual | ofx | csv | pdf | recorrencia
    id_externo: Mapped[str | None] = mapped_column(String(120))
    hash_dedup: Mapped[str | None] = mapped_column(String(64), index=True)
    importacao_id: Mapped[int | None] = mapped_column(index=True)
    ignorar: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    meta_id: Mapped[int | None] = mapped_column(ForeignKey("meta.id"), index=True)  # dinheiro guardado/retirado de um objetivo
    caixinha_id: Mapped[int | None] = mapped_column(ForeignKey("caixinha.id"), index=True)  # entra/sai desta caixinha da conta
    criado_em: Mapped[datetime] = mapped_column(default=_agora_sem_fuso)

    tags: Mapped[list[Tag]] = relationship(secondary=lancamento_tag, lazy="selectin")
    caixinha: Mapped["Caixinha | None"] = relationship(lazy="selectin")  # noqa: F821
