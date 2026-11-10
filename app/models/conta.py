from datetime import date

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

TIPOS_CONTA = ("corrente", "investimento", "cartao")


class Conta(Base):
    __tablename__ = "conta"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80), unique=True)
    tipo: Mapped[str] = mapped_column(String(20))
    ativa: Mapped[bool] = mapped_column(default=True)
    cor: Mapped[str] = mapped_column(String(9), default="#6366f1")
    icone: Mapped[str] = mapped_column(String(40), default="wallet")
    entra_saldo_liquido: Mapped[bool] = mapped_column(default=True)
    # moeda dos valores da conta (centavos dela): "BRL" ou "USD"; só se escolhe ao criar
    moeda: Mapped[str] = mapped_column(String(3), default="BRL")
    saldo_inicial: Mapped[int] = mapped_column(default=0)
    data_saldo_inicial: Mapped[date]
    # só cartão
    limite: Mapped[int | None]
    dia_fechamento: Mapped[int | None]
    dia_vencimento: Mapped[int | None]
    conta_pagamento_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    # entra na sugestão "o melhor cartão para comprar hoje"
    sugerir_compra: Mapped[bool] = mapped_column(default=True)
    # sala VIP (só cartão): vouchers por ano (None = sem o benefício), usados no período e próxima renovação
    vip_vouchers: Mapped[int | None]
    vip_usados: Mapped[int] = mapped_column(default=0)
    vip_renova_em: Mapped[date | None]
    # posição do cartão nas telas (escolhida arrastando em Cartões); None = depois dos ordenados, por nome
    ordem: Mapped[int | None]
    # mapeamento aprendido na importação (Plano 2)
    identificador_externo: Mapped[str | None] = mapped_column(String(120))
