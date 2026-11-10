import re
from decimal import Decimal
from fractions import Fraction
from math import floor

_MILHAR = re.compile(r"\d{1,3}(\.\d{3})+")
_INTEIRO = re.compile(r"\d+")
_DECIMAL_PONTO = re.compile(r"\d+\.\d+")
_TOKEN = re.compile(r"\s*(?:(?P<num>[\d.,]+)|(?P<op>[-+*/()]))")


def _erro(texto: str) -> ValueError:
    return ValueError(f"valor inválido: {texto!r}")


def _numero(t: str, original: str) -> Decimal:
    """Converte um número sem sinal (formato BR ou com ponto decimal) em Decimal; regex estrita."""
    if "," in t:
        inteiro, _, frac = t.partition(",")
        if not (_INTEIRO.fullmatch(inteiro) or _MILHAR.fullmatch(inteiro)) or not _INTEIRO.fullmatch(frac):
            raise _erro(original)
        return Decimal(f"{inteiro.replace('.', '')}.{frac}")
    if _MILHAR.fullmatch(t):
        return Decimal(t.replace(".", ""))
    if _INTEIRO.fullmatch(t) or _DECIMAL_PONTO.fullmatch(t):
        return Decimal(t)
    raise _erro(original)


def _centavos(valor: Decimal | Fraction) -> int:
    """Reais -> centavos, arredondando meio para cima."""
    return floor(Fraction(valor) * 100 + Fraction(1, 2))


def parse_reais(texto: str) -> int:
    """Converte texto em reais (formato brasileiro ou com ponto decimal) para centavos."""
    t = texto.strip().replace("R$", "").replace(" ", "")
    negativo = t.startswith("-")
    if t[:1] in ("+", "-"):
        t = t[1:]
    if not t:
        raise _erro(texto)
    centavos = _centavos(_numero(t, texto))
    return -centavos if negativo else centavos


def _tokens(texto: str) -> list[tuple[str, str]]:
    limpo = texto.replace("R$", " ")
    tokens, pos = [], 0
    while pos < len(limpo):
        if not limpo[pos:].strip():
            break
        m = _TOKEN.match(limpo, pos)
        if not m:
            raise _erro(texto)
        tokens.append(("num", m["num"]) if m["num"] else ("op", m["op"]))
        pos = m.end()
    return tokens


def avaliar_valor(texto: str) -> int:
    """Avalia uma conta simples em reais (+ - * / e parênteses) e devolve centavos > 0. Sem eval."""
    tokens = _tokens(texto)
    if not tokens:
        raise _erro(texto)
    pos = 0

    def olhar() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def expressao() -> Fraction:
        nonlocal pos
        valor = termo()
        while olhar() in (("op", "+"), ("op", "-")):
            op = tokens[pos][1]
            pos += 1
            direita = termo()
            valor = valor + direita if op == "+" else valor - direita
        return valor

    def termo() -> Fraction:
        nonlocal pos
        valor = fator()
        while olhar() in (("op", "*"), ("op", "/")):
            op = tokens[pos][1]
            pos += 1
            direita = fator()
            if op == "*":
                valor *= direita
            elif direita == 0:
                raise _erro(texto)
            else:
                valor /= direita
        return valor

    def fator() -> Fraction:
        nonlocal pos
        tok = olhar()
        if tok is None:
            raise _erro(texto)
        pos += 1
        if tok == ("op", "-"):
            return -fator()
        if tok == ("op", "("):
            valor = expressao()
            if olhar() != ("op", ")"):
                raise _erro(texto)
            pos += 1
            return valor
        if tok[0] == "num":
            return Fraction(_numero(tok[1], texto))
        raise _erro(texto)

    resultado = expressao()
    if pos != len(tokens):
        raise _erro(texto)
    centavos = _centavos(resultado)
    if centavos <= 0:
        raise _erro(texto)
    return centavos


def formatar_reais(centavos: int) -> str:
    sinal = "-" if centavos < 0 else ""
    inteiro, frac = divmod(abs(centavos), 100)
    milhar = f"{inteiro:,}".replace(",", ".")
    return f"{sinal}R$ {milhar},{frac:02d}"


def formatar_dolares(centavos: int) -> str:
    return formatar_reais(centavos).replace("R$", "US$")
