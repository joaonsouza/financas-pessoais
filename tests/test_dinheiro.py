import pytest

from app.core.dinheiro import avaliar_valor, formatar_reais, parse_reais


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("1.234,56", 123456),
        ("R$ 10,5", 1050),
        ("-3,00", -300),
        ("0,01", 1),
        ("1.234", 123400),
        ("12.5", 1250),
        ("100", 10000),
        ("  R$ 2.000.000,00 ", 200000000),
    ],
)
def test_parse_reais(texto, esperado):
    assert parse_reais(texto) == esperado


@pytest.mark.parametrize("texto", ["", "abc", "1,2,3", "R$"])
def test_parse_reais_invalido(texto):
    with pytest.raises(ValueError):
        parse_reais(texto)


@pytest.mark.parametrize(
    "centavos, esperado",
    [
        (123456, "R$ 1.234,56"),
        (-500, "-R$ 5,00"),
        (0, "R$ 0,00"),
        (7, "R$ 0,07"),
        (100000000, "R$ 1.000.000,00"),
    ],
)
def test_formatar_reais(centavos, esperado):
    assert formatar_reais(centavos) == esperado


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("12,50+8", 2050),
        ("3*19,90", 5970),
        ("(100-20)/2", 4000),
        ("10/3", 333),
        ("1/8", 13),  # 0,125 -> meio para cima
        ("1.234,56", 123456),
        (" R$ 2 * 3 ", 600),
        ("2+3*4", 1400),
        ("-(1-3)", 200),
        ("((5))", 500),
        ("45,90+12", 5790),
    ],
)
def test_avaliar_valor(texto, esperado):
    assert avaliar_valor(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    ["", "abc", "1+", "(1+2", "1+2)", "5-5", "1-9", "0", "1/0", "2**3", "1e3", "inf", "nan", "__import__('os')",
     "1,2,3", "5 5", "1+*2", "0,001"],
)
def test_avaliar_valor_invalido(texto):
    with pytest.raises(ValueError, match="valor inválido"):
        avaliar_valor(texto)


@pytest.mark.parametrize("texto", ["inf", "nan", "1e3", "1E3", "--3", "+-3", "Infinity", "1.2.3,4", "12.", "."])
def test_parse_reais_endurecido(texto):
    with pytest.raises(ValueError):
        parse_reais(texto)
