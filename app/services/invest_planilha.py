"""Pré-preenchimento da carteira a partir da aba INVESTIMENTOS da planilha de controle.

Layout lido (valores em cache, `data_only`): cada bloco começa numa linha com o título na coluna B e "% Ideal" na
coluna I; abaixo vêm os ativos até a primeira linha em branco: ticker na coluna C (renda fixa: nome na coluna B),
quantidade na coluna F (renda fixa: valor em R$) e % ideal dentro da classe na coluna I (fração: 0,25 = 25%).
O arquivo só é aberto em tempo de execução; o caminho é um atributo do módulo para os testes trocarem.
"""
import unicodedata
import warnings
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import openpyxl
from sqlalchemy.orm import Session

from app.models import Ativo
from app.services.invest_operacoes import DadosAtivo, DadosOperacao, adicionar_ativo, salvar_operacao

CAMINHO_PLANILHA = Path(r"Z:\Meu Drive\Finanças\Planilhas\Controle Orçamento 2025.xlsx")
ABA = "INVESTIMENTOS"
COL_TITULO, COL_TICKER, COL_QTD, COL_IDEAL = 2, 3, 6, 9  # B, C, F, I
BLOCOS = (  # (trecho do título sem acento/minúsculo, classe)
    ("acoes", "acao"), ("fundos imobiliarios", "fii"), ("internacional", "exterior"), ("cripto", "cripto"),
    ("renda fixa", "renda_fixa"),
)
IGNORADOS = {"exterior": {"USD"}}  # "USD" é o caixa em dólar, não um ativo cotável
CEM = Decimal(100)


class PlanilhaIndisponivel(Exception):
    """Arquivo, aba ou layout da planilha não pôde ser lido."""


@dataclass(frozen=True)
class PosicaoPlanilha:
    classe: str
    codigo: str
    quantidade: Decimal  # renda fixa: valor em R$
    meta_pct: Decimal  # % dentro da classe (0–100)


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return sem_acento.strip().lower()


def _classe_do_titulo(valor) -> str | None:
    if not isinstance(valor, str):
        return None
    titulo = _normalizar(valor)
    return next((classe for trecho, classe in BLOCOS if trecho in titulo), None)


def _numero(valor) -> Decimal | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return Decimal(str(valor))


def ler_planilha(caminho: Path | None = None) -> list[PosicaoPlanilha]:
    caminho = Path(caminho or CAMINHO_PLANILHA)
    if not caminho.exists():
        raise PlanilhaIndisponivel(f"Planilha não encontrada: {caminho}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(caminho, data_only=True)
    except Exception as exc:  # noqa: BLE001 — arquivo aberto/corrompido/formato inesperado
        raise PlanilhaIndisponivel(f"Não foi possível abrir a planilha: {exc}") from exc
    if ABA not in wb.sheetnames:
        raise PlanilhaIndisponivel(f"A planilha não tem a aba {ABA}.")
    ws = wb[ABA]

    posicoes: list[PosicaoPlanilha] = []
    classe: str | None = None
    for r in range(1, ws.max_row + 1):
        titulo, ticker = ws.cell(r, COL_TITULO).value, ws.cell(r, COL_TICKER).value
        if _classe_do_titulo(titulo) and _normalizar(str(ws.cell(r, COL_IDEAL).value or "")) == "% ideal":
            classe = _classe_do_titulo(titulo)
            continue
        if classe is None:
            continue
        if titulo in (None, "") and ticker in (None, ""):  # linha em branco encerra o bloco
            classe = None
            continue
        qtd = _numero(ws.cell(r, COL_QTD).value)
        if qtd is None:  # cabeçalho da tabela ("Ticker", "Qtd") ou linha sem quantidade
            continue
        nome = ticker if classe != "renda_fixa" else titulo
        if not isinstance(nome, str) or not nome.strip() or nome.strip().startswith("#"):
            continue
        codigo = nome.strip() if classe == "renda_fixa" else nome.strip().upper()
        if codigo in IGNORADOS.get(classe, ()):
            continue
        ideal = _numero(ws.cell(r, COL_IDEAL).value) or Decimal(0)
        posicoes.append(PosicaoPlanilha(
            classe=classe, codigo=codigo, quantidade=qtd.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP).normalize(),
            meta_pct=(ideal * CEM).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize(),
        ))
    if not posicoes:
        raise PlanilhaIndisponivel(f"Nenhuma posição encontrada na aba {ABA}.")
    return posicoes


@dataclass(frozen=True)
class ItemImportacao:
    classe: str
    codigo: str
    quantidade: Decimal
    meta_pct: Decimal
    preco_medio: int  # centavos na moeda do ativo (renda fixa: valor total)
    cambio: Decimal | None = None
    indexador: str | None = None
    taxa: Decimal | None = None
    vencimento: date | None = None


def importar_posicoes(s: Session, itens: list[ItemImportacao], data: date) -> list[Ativo]:
    """Cria `Ativo` + `ajuste_posicao` de cada item. Levanta ValueError (sem commit) no primeiro item inválido."""
    criados = []
    for item in itens:
        try:
            ativo = adicionar_ativo(s, DadosAtivo(
                classe=item.classe, codigo=item.codigo, indexador=item.indexador, taxa=item.taxa,
                vencimento=item.vencimento))
            ativo.meta_pct = item.meta_pct
            if item.quantidade > 0:
                if item.preco_medio <= 0:
                    raise ValueError("informe o preço médio (ou o valor, na renda fixa)")
                rf = item.classe == "renda_fixa"
                salvar_operacao(s, ativo, DadosOperacao(
                    tipo="ajuste_posicao", data=data, quantidade=Decimal(1) if rf else item.quantidade,
                    preco_unitario=item.preco_medio, cambio=item.cambio, observacao="Pré-preenchido pela planilha"))
        except ValueError as exc:
            raise ValueError(f"{item.codigo}: {exc}") from exc
        criados.append(ativo)
    return criados
