from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx

from app.services.mercado import ErroMercado, novo_cliente, obter_json

URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{n}/dados"
SERIE_CDI = 12  # % ao dia, só dias úteis
SERIE_IPCA = 433  # % ao mês
JANELA_ANOS = 10
UM_DIA = timedelta(days=1)


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _somar_anos(d: date, anos: int) -> date:
    try:
        return d.replace(year=d.year + anos)
    except ValueError:  # 29/02
        return d.replace(year=d.year + anos, day=28)


class Bcb:
    def __init__(self, cliente: httpx.Client | None = None):
        self.cliente = cliente or novo_cliente()

    def serie(self, numero: int, inicio: date, fim: date) -> list[tuple[date, Decimal]]:
        """Pontos (data, valor em %) da série SGS entre `inicio` e `fim`, em janelas de até 10 anos."""
        pontos: list[tuple[date, Decimal]] = []
        ini = inicio
        while ini <= fim:
            limite = _somar_anos(ini, JANELA_ANOS)
            fim_janela = fim if limite > fim else limite - UM_DIA
            dados = obter_json(
                self.cliente, URL.format(n=numero),
                {"formato": "json", "dataInicial": _br(ini), "dataFinal": _br(fim_janela)},
            )
            if not isinstance(dados, list):
                raise ErroMercado(f"resposta inesperada do BCB (série {numero})")
            try:
                for item in dados:
                    pontos.append((datetime.strptime(item["data"], "%d/%m/%Y").date(), Decimal(item["valor"])))
            except (KeyError, ValueError, TypeError) as exc:
                raise ErroMercado(f"item inválido na série {numero} do BCB") from exc
            ini = fim_janela + UM_DIA
        return sorted(pontos)

    def cdi(self, inicio: date, fim: date) -> list[tuple[date, Decimal]]:
        return self.serie(SERIE_CDI, inicio, fim)

    def ipca(self, inicio: date, fim: date) -> list[tuple[date, Decimal]]:
        return self.serie(SERIE_IPCA, inicio, fim)
