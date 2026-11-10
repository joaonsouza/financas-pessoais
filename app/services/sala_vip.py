"""Vouchers de sala VIP por cartão: N por ano, renovação anual numa data. Ler não grava; usar/desfazer gravam os
valores efetivos (período virado zera os usados e leva a renovação para depois de hoje)."""
from dataclasses import dataclass
from datetime import date

from app.models import Conta

SEM_VOUCHER = "Sem vouchers de sala VIP neste cartão até a renovação."


@dataclass(frozen=True)
class SituacaoVip:
    total: int
    usados: int
    restantes: int
    renova_em: date | None


def _mais_um_ano(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29/02 em ano não bissexto
        return d.replace(year=d.year + 1, day=28)


def _efetivos(cartao: Conta, hoje: date) -> tuple[int, date | None]:
    usados, renova = cartao.vip_usados or 0, cartao.vip_renova_em
    if renova is not None and hoje >= renova:
        usados = 0
        while hoje >= renova:
            renova = _mais_um_ano(renova)
    return usados, renova


def situacao_vip(cartao: Conta, hoje: date) -> SituacaoVip | None:
    if cartao.vip_vouchers is None:
        return None
    usados, renova = _efetivos(cartao, hoje)
    return SituacaoVip(cartao.vip_vouchers, usados, max(cartao.vip_vouchers - usados, 0), renova)


def usar_voucher(cartao: Conta, hoje: date) -> None:
    usados, renova = _efetivos(cartao, hoje)
    if cartao.vip_vouchers is None or usados >= cartao.vip_vouchers:
        raise ValueError(SEM_VOUCHER)
    cartao.vip_usados, cartao.vip_renova_em = usados + 1, renova


def desfazer_voucher(cartao: Conta, hoje: date) -> None:
    usados, renova = _efetivos(cartao, hoje)
    cartao.vip_usados, cartao.vip_renova_em = max(usados - 1, 0), renova
