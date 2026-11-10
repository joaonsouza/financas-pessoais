from datetime import date

import pytest

from app.core.periodo import (
    competencia_de,
    dia_no_mes,
    nome_competencia,
    primeiro_dia,
    somar_meses,
    somar_meses_data,
    ultimo_dia,
    validar_competencia,
)


def test_competencia_de():
    assert competencia_de(date(2026, 3, 9)) == "2026-03"


@pytest.mark.parametrize(
    "comp, n, esperado",
    [("2026-10", 1, "2026-11"), ("2026-12", 1, "2027-01"), ("2026-01", -1, "2025-12"), ("2026-10", 14, "2027-12")],
)
def test_somar_meses(comp, n, esperado):
    assert somar_meses(comp, n) == esperado


def test_primeiro_e_ultimo_dia():
    assert primeiro_dia("2028-02") == date(2028, 2, 1)
    assert ultimo_dia("2028-02") == date(2028, 2, 29)
    assert ultimo_dia("2026-02") == date(2026, 2, 28)


def test_dia_no_mes_limita_ao_fim_do_mes():
    assert dia_no_mes("2026-02", 31) == date(2026, 2, 28)
    assert dia_no_mes("2026-10", 5) == date(2026, 10, 5)


def test_somar_meses_data():
    assert somar_meses_data(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert somar_meses_data(date(2026, 10, 15), 3) == date(2027, 1, 15)


def test_validar_competencia():
    assert validar_competencia("2026-10") == "2026-10"
    for invalido in ["2026-13", "2026-1", "out/2026", ""]:
        with pytest.raises(ValueError):
            validar_competencia(invalido)


def test_nome_competencia():
    assert nome_competencia("2026-10") == "Outubro 2026"
    assert nome_competencia("2027-03") == "Março 2027"
