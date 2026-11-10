from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _agora_sem_fuso() -> datetime:
    return datetime.now()


class Importacao(Base):
    """Uma importação de arquivo (histórico, com snapshot para desfazer)."""

    __tablename__ = "importacao"

    id: Mapped[int] = mapped_column(primary_key=True)
    arquivo_nome: Mapped[str] = mapped_column(String(300))
    hash_arquivo: Mapped[str] = mapped_column(String(64), index=True)
    tipo: Mapped[str] = mapped_column(String(12))  # ofx | csv | pdf | pdf_ia
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id"), index=True)
    criada_em: Mapped[datetime] = mapped_column(default=_agora_sem_fuso)
    qtd_novas: Mapped[int] = mapped_column(default=0)
    qtd_conciliadas: Mapped[int] = mapped_column(default=0)
    qtd_duplicadas: Mapped[int] = mapped_column(default=0)
    desfeita: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    snapshot_conciliacoes: Mapped[list | dict | None] = mapped_column(JSON)
