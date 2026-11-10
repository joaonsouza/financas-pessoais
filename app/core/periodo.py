import calendar
import re
from datetime import date

_RE_COMPETENCIA = re.compile(r"\d{4}-(0[1-9]|1[0-2])")

NOMES_MESES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

INTERVALOS_MESES = {
    1: "Todo mês", 2: "A cada 2 meses", 3: "Trimestral", 6: "Semestral", 12: "Anual",
}


def validar_intervalo(n: int) -> int:
    if not isinstance(n, int) or not 1 <= n <= 120:
        raise ValueError("intervalo inválido")
    return n


def _partes(comp: str) -> tuple[int, int]:
    ano, mes = comp.split("-")
    return int(ano), int(mes)


def competencia_de(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def validar_competencia(texto: str) -> str:
    if not _RE_COMPETENCIA.fullmatch(texto or ""):
        raise ValueError(f"competência inválida: {texto!r}")
    return texto


def somar_meses(comp: str, n: int) -> str:
    ano, mes = _partes(comp)
    total = ano * 12 + (mes - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def primeiro_dia(comp: str) -> date:
    ano, mes = _partes(comp)
    return date(ano, mes, 1)


def ultimo_dia(comp: str) -> date:
    ano, mes = _partes(comp)
    return date(ano, mes, calendar.monthrange(ano, mes)[1])


def dia_no_mes(comp: str, dia: int) -> date:
    ano, mes = _partes(comp)
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


def somar_meses_data(d: date, n: int) -> date:
    return dia_no_mes(somar_meses(competencia_de(d), n), d.day)


def nome_competencia(comp: str) -> str:
    ano, mes = _partes(comp)
    return f"{NOMES_MESES[mes - 1]} {ano}"


def meses_entre(inicio: str, competencia: str) -> int:
    ano_i, mes_i = _partes(inicio)
    ano_c, mes_c = _partes(competencia)
    return (ano_c - ano_i) * 12 + (mes_c - mes_i)
