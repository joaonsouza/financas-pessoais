from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import openpyxl
from openpyxl.utils import range_boundaries

PLANILHA_PADRAO = Path(r"Z:\Meu Drive\Finanças\Planilhas\Controle Orçamento 2025.xlsx")


@dataclass
class ContaSugerida:
    nome: str
    tipo: str
    entra_saldo_liquido: bool
    saldo: int


@dataclass
class AssinaturaSugerida:
    nome: str
    ativa: bool
    valor: int


def _centavos(valor) -> int:
    if valor is None or valor == "":
        return 0
    return int((Decimal(str(valor)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _linhas(ws, tabela) -> list[dict]:
    col1, lin1, col2, lin2 = range_boundaries(tabela.ref)
    cabecalho = [ws.cell(lin1, c).value for c in range(col1, col2 + 1)]
    return [
        {cabecalho[i]: ws.cell(lin, col1 + i).value for i in range(len(cabecalho))}
        for lin in range(lin1 + 1, lin2 + 1)
    ]


def _valida(descricao) -> bool:
    return bool(descricao) and str(descricao).strip() != "Total"


def ler_config_inicial(caminho: Path) -> tuple[list[ContaSugerida], list[AssinaturaSugerida]]:
    wb = openpyxl.load_workbook(caminho, data_only=True)
    ws = wb["CONFIG_INICIAL"]
    contas: list[ContaSugerida] = []
    assinaturas: list[AssinaturaSugerida] = []
    for tabela in ws.tables.values():
        linhas = _linhas(ws, tabela)
        if tabela.name == "CONFIG":
            for linha in linhas:
                if not _valida(linha.get("Desc")):
                    continue
                tipo = "investimento" if str(linha.get("Tipo") or "").lower().startswith("invest") else "corrente"
                contas.append(ContaSugerida(str(linha["Desc"]).strip(), tipo, bool(linha.get("SL")),
                                            _centavos(linha.get("Inicial"))))
        elif tabela.name.startswith("Assinaturas"):
            for linha in linhas:
                if not _valida(linha.get("Desc")):
                    continue
                assinaturas.append(AssinaturaSugerida(str(linha["Desc"]).strip(), bool(linha.get("Ativa")),
                                                      _centavos(linha.get("Valor"))))
    return contas, assinaturas
