"""Normalização de descrições, parcelas e valores vindos de arquivos bancários."""
from __future__ import annotations

import re
import unicodedata

from app.core.dinheiro import parse_reais

# "Parcela 1/2", "- 1/4", "Parcela 16 de 21", "LOJAX03/04" (grudada), sempre no fim da descrição.
_PARCELA = re.compile(
    r"[\s\-–]*(?:parcela\s*)?(?P<n>\d{1,2})\s*(?:/|\s+de\s+)\s*(?P<t>\d{1,2})\s*$", re.IGNORECASE
)
_DATA_GRUDADA = re.compile(r"\s*\d{1,2}/\d{1,2}\s*$")


def _sem_acento(texto: str) -> str:
    base = unicodedata.normalize("NFD", texto)
    return "".join(c for c in base if not unicodedata.combining(c))


def _colapsar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


def extrair_parcela(texto: str) -> tuple[str, int | None, int | None]:
    """Separa o sufixo de parcela. Devolve (descrição limpa, número, total) ou (texto, None, None)."""
    m = _PARCELA.search(texto)
    if m:
        num, total = int(m["n"]), int(m["t"])
        if 1 <= num <= total and total >= 2:
            return _colapsar(texto[: m.start()]).rstrip(" -–"), num, total
    return _colapsar(texto), None, None


def normalizar_descricao(texto: str) -> str:
    """Maiúsculas, sem acento, espaços colapsados, sem sufixo de parcela nem data grudada."""
    limpa, _, _ = extrair_parcela(texto)
    limpa = _DATA_GRUDADA.sub("", limpa)
    return _colapsar(_sem_acento(limpa).upper())


def valor_br(texto: str) -> int:
    """Valor no formato BR ("1.234,56", "-0,01", "R$ 5,00") em centavos."""
    return parse_reais(texto)
