"""Mascaramento de dados pessoais no texto de um PDF antes de enviá-lo à IA.

Cobre CPF (com e sem pontos), números de cartão (mantém os 4 finais), agência e conta, e-mails e telefones.
Valores em reais, datas e descrições de lançamentos não são tocados.
"""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CPF = re.compile(r"(?<![\d.])\d{3}\.\d{3}\.\d{3}-\d{2}(?![\d])|(?<![\d.,/-])\d{11}(?![\d,/-]|\.\d)")
# 13 a 19 dígitos, com espaços ou hífens de separação (cartão completo)
_CARTAO_COMPLETO = re.compile(r"(?<![\d.,/])(?:\d[ -]?){12,18}\d(?![\d.,/])")
# cartão já parcialmente mascarado pelo banco: 1234.XXXX.XXXX.5678, **** **** **** 5678, ************5678
_CARTAO_PARCIAL = re.compile(r"(?:\d{4}[ .-]?)?[Xx*]{4}(?:[ .-]?[Xx*]{4}){0,2}[ .-]?(\d{4})(?!\d)")
_AGENCIA_CONTA = re.compile(
    r"(?i)\b(ag(?:ência|encia|\.)?|conta(?:\s+corrente)?|c/c|cc)\b(\s*[:.]?\s*)(\d[\d.\-/ ]{2,}\d)(?![\d,])"
)
_AGENCIA_CONTA_BARRA = re.compile(r"(?<![\d.,/-])\d{4}\s*/\s*\d{5,}-?\d?(?![\d.,/-])")
_TELEFONE = re.compile(
    r"(?<![\d.,/-])(?:\+55\s*)?(?:\(\d{2}\)\s*|\d{2}\s)9?\d{4}-?\d{4}(?![\d.,/-])|\+55\s*\d{2}\s*9?\d{4}-?\d{4}"
)


def _mascarar_cartao(m: re.Match) -> str:
    digitos = re.sub(r"\D", "", m.group())
    return f"**** **** **** {digitos[-4:]}"


def _mascarar_agencia_conta(m: re.Match) -> str:
    return f"{m.group(1)}{m.group(2)}****"


def mascarar(texto: str) -> str:
    """Devolve o texto com os dados pessoais substituídos por marcadores."""
    texto = _EMAIL.sub("[e-mail]", texto)
    texto = _CPF.sub("***.***.***-**", texto)
    texto = _CARTAO_PARCIAL.sub(lambda m: f"**** **** **** {m.group(1)}", texto)
    texto = _CARTAO_COMPLETO.sub(_mascarar_cartao, texto)
    texto = _AGENCIA_CONTA.sub(_mascarar_agencia_conta, texto)
    texto = _AGENCIA_CONTA_BARRA.sub("****/****", texto)
    texto = _TELEFONE.sub("[telefone]", texto)
    return texto
