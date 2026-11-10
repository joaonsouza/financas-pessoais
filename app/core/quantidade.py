from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


def q(texto) -> Decimal:
    """Converte texto (ponto decimal, ou vírgula pt-BR) em Decimal. Nunca passa por float."""
    if isinstance(texto, Decimal):
        return texto
    if isinstance(texto, float):
        raise TypeError("float não é aceito para quantidades/taxas")
    bruto = str(texto).strip()
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    try:
        return Decimal(bruto)
    except InvalidOperation as exc:
        raise ValueError(f"número inválido: {texto!r}") from exc


def canonico(valor: Decimal) -> str:
    """Texto decimal canônico: ponto decimal, sem expoente e sem zeros à direita."""
    if valor == 0:
        return "0"
    texto = format(valor.normalize(), "f")
    return texto


def fmt_qtd(valor: Decimal) -> str:
    """Formato pt-BR (milhar com ponto, vírgula decimal), até 8 casas, sem zeros à direita."""
    arredondado = valor.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    texto = canonico(arredondado)
    sinal = ""
    if texto.startswith("-"):
        sinal, texto = "-", texto[1:]
    inteiro, _, frac = texto.partition(".")
    inteiro = f"{int(inteiro):,}".replace(",", ".")
    return f"{sinal}{inteiro},{frac}" if frac else f"{sinal}{inteiro}"


class DecimalTexto(TypeDecorator):
    """Decimal persistido como texto decimal canônico (sem perda, sem float)."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("float não é aceito em DecimalTexto")
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        return canonico(value)

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)
