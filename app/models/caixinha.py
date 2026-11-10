from datetime import date

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Caixinha(Base):
    """Divisão do dinheiro de uma conta por finalidade (IPTU, reserva, viagem...).

    O banco pode guardar tudo junto; o app separa. O que não está em nenhuma caixinha é o "Livre" da conta
    (saldo da conta − soma das caixinhas), calculado, nunca gravado.
    """

    __tablename__ = "caixinha"

    id: Mapped[int] = mapped_column(primary_key=True)
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id"), index=True)
    nome: Mapped[str] = mapped_column(String(80))
    icone: Mapped[str] = mapped_column(String(30), default="piggy-bank")
    cor: Mapped[str] = mapped_column(String(9), default="#10b981")
    meta_id: Mapped[int | None] = mapped_column(ForeignKey("meta.id"), index=True)  # objetivo que ela compõe
    ativa: Mapped[bool] = mapped_column(default=True)
    # reserva de oportunidade (conta de investimento): o que estiver guardado aqui fica fora da alocação da
    # carteira e entra como saldo disponível no rebalanceamento
    oportunidade: Mapped[bool] = mapped_column(default=False, server_default="0")


class MovimentoCaixinha(Base):
    """Mudança só na divisão (valor inicial, remanejamento entre caixinhas): o dinheiro da conta não muda."""

    __tablename__ = "movimento_caixinha"

    id: Mapped[int] = mapped_column(primary_key=True)
    caixinha_id: Mapped[int] = mapped_column(ForeignKey("caixinha.id"), index=True)
    data: Mapped[date]
    valor: Mapped[int]  # centavos; positivo = entra na caixinha (sai do Livre ou de outra caixinha)
    descricao: Mapped[str] = mapped_column(String(120))
    grupo: Mapped[str | None] = mapped_column(String(32))  # as duas pontas de um remanejamento
