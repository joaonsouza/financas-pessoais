"""Títulos do Tesouro Direto: taxa e preço de compra por dia (dados públicos do Tesouro Transparente).

O arquivo traz o histórico inteiro (todos os títulos, todos os dias). O app guarda numa tabela local e só
baixa de novo uma vez por dia, quando alguém precisa (lista de títulos no formulário ou "Atualizar cotações").
"""
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import TaxaTesouro, get_config, set_config
from app.services.mercado import ErroMercado, novo_cliente

URL = ("https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/"
       "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv")
CHAVE_ATUALIZADO = "tesouro_atualizado_em"
CHAVE_FORMATO, FORMATO = "tesouro_formato", 2  # 2: com o preço de venda
# títulos que o app sabe avaliar (sem cupom): tipo do arquivo -> indexador do app
INDEXADOR_DO_TITULO = {
    "Tesouro IPCA+": "ipca", "Tesouro Prefixado": "pre", "Tesouro Selic": "cdi",
    "Tesouro Educa+": "ipca", "Tesouro Renda+ Aposentadoria Extra": "ipca",
}


@dataclass(frozen=True)
class PontoTesouro:
    titulo: str
    vencimento: date
    data: date
    taxa_compra: Decimal
    pu_compra: int
    pu_venda: int | None = None


def _data(texto: str) -> date:
    return datetime.strptime(texto.strip(), "%d/%m/%Y").date()


def _decimal(texto: str) -> Decimal:
    return Decimal(texto.strip().replace(".", "").replace(",", "."))


def _centavos(texto: str) -> int:
    return int((_decimal(texto) * 100).quantize(Decimal(1)))


def ler_csv(texto: str) -> list[PontoTesouro]:
    """Linhas do arquivo do Tesouro Transparente (separador ";", vírgula decimal). Linhas ruins são puladas."""
    leitor = csv.DictReader(io.StringIO(texto), delimiter=";")
    pontos = []
    for linha in leitor:
        try:
            pontos.append(PontoTesouro(
                titulo=linha["Tipo Titulo"].strip(), vencimento=_data(linha["Data Vencimento"]),
                data=_data(linha["Data Base"]), taxa_compra=_decimal(linha["Taxa Compra Manha"]),
                pu_compra=_centavos(linha["PU Compra Manha"]), pu_venda=_centavos(linha.get("PU Venda Manha") or "0") or None))
        except (KeyError, ValueError, InvalidOperation, AttributeError):
            continue
    return pontos


class Tesouro:
    def __init__(self, cliente: httpx.Client | None = None):
        self.cliente = cliente or novo_cliente()

    def historico(self) -> list[PontoTesouro]:
        try:
            resp = self.cliente.get(URL, timeout=120)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ErroMercado(f"falha ao baixar os títulos do Tesouro: {exc}") from exc
        pontos = ler_csv(resp.content.decode("latin-1"))
        if not pontos:
            raise ErroMercado("arquivo do Tesouro sem linhas reconhecíveis")
        return pontos


def atualizar_tesouro(s: Session, hoje: date, *, tesouro: Tesouro | None = None, forcar: bool = False) -> bool:
    """Grava os dias novos do arquivo (no máximo uma vez por dia). False se o download falhou."""
    if not forcar and get_config(s, CHAVE_ATUALIZADO, None) == hoje.isoformat():
        return True
    try:
        pontos = (tesouro or Tesouro()).historico()
    except ErroMercado:
        return False
    ultimo = s.scalar(select(func.max(TaxaTesouro.data)))
    if get_config(s, CHAVE_FORMATO, None) != FORMATO:
        s.query(TaxaTesouro).delete()  # tabela gravada num formato antigo (sem o preço de venda): recarrega inteira
        ultimo = None
    novos = [p for p in pontos if ultimo is None or p.data > ultimo]
    s.bulk_insert_mappings(TaxaTesouro, [p.__dict__ for p in novos])
    set_config(s, CHAVE_ATUALIZADO, hoje.isoformat())
    set_config(s, CHAVE_FORMATO, FORMATO)
    s.commit()
    return True


@dataclass(frozen=True)
class TituloTesouro:
    titulo: str
    vencimento: date
    indexador: str
    taxa_compra: Decimal
    pu_compra: int

    @property
    def nome(self) -> str:
        """Como o ativo fica no app: "Tesouro IPCA+ 2050"."""
        return f"{self.titulo} {self.vencimento.year}"


def titulos_a_venda(s: Session) -> list[TituloTesouro]:
    """Títulos (que o app sabe avaliar) do dia mais recente do arquivo, por tipo e vencimento."""
    ultimo = s.scalar(select(func.max(TaxaTesouro.data)))
    if ultimo is None:
        return []
    linhas = s.scalars(select(TaxaTesouro).where(TaxaTesouro.data == ultimo,
                                                 TaxaTesouro.titulo.in_(list(INDEXADOR_DO_TITULO))))
    return sorted((TituloTesouro(t.titulo, t.vencimento, INDEXADOR_DO_TITULO[t.titulo], t.taxa_compra, t.pu_compra)
                   for t in linhas), key=lambda t: (t.titulo, t.vencimento))


def taxa_na_data(s: Session, titulo: str, vencimento: date, dia: date) -> TaxaTesouro | None:
    """Taxa e preço de compra do título no dia (ou no último dia útil antes dele)."""
    return s.scalar(select(TaxaTesouro).where(
        TaxaTesouro.titulo == titulo, TaxaTesouro.vencimento == vencimento, TaxaTesouro.data <= dia)
        .order_by(TaxaTesouro.data.desc()).limit(1))


def titulo_do_codigo(codigo: str) -> str | None:
    """"Tesouro IPCA+ 2050" -> "Tesouro IPCA+" (sem diferenciar maiúsculas), se for um título conhecido."""
    base = codigo.rsplit(" ", 1)[0].strip().lower()
    return next((t for t in INDEXADOR_DO_TITULO if t.lower() == base), None)


def eh_tesouro(ativo) -> bool:
    return ativo.classe == "renda_fixa" and ativo.vencimento is not None and titulo_do_codigo(ativo.codigo) is not None


def preco_de_mercado(s: Session, ativo, dia: date) -> TaxaTesouro | None:
    """Linha do Tesouro com o preço de venda do título no dia (ou no último dia antes dele com preço)."""
    titulo = titulo_do_codigo(ativo.codigo)
    if titulo is None or ativo.vencimento is None:
        return None
    return s.scalar(select(TaxaTesouro).where(
        TaxaTesouro.titulo == titulo, TaxaTesouro.vencimento == ativo.vencimento, TaxaTesouro.data <= dia,
        TaxaTesouro.pu_venda.is_not(None)).order_by(TaxaTesouro.data.desc()).limit(1))
