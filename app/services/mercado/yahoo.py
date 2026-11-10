from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.services.mercado import ErroMercado, novo_cliente, obter_json

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}"


def _faixa(desde: date, hoje: date | None) -> str:
    if hoje is None:
        return "max"
    anos = (hoje - desde).days / 365.25
    for limite, nome in ((1, "1y"), (2, "2y"), (5, "5y"), (10, "10y")):
        if anos < limite:
            return nome
    return "max"


def _fechamento_anterior(res: dict) -> Decimal | None:
    """Fechamento do pregão anterior ao do preço atual. O `chartPreviousClose` é o de antes da janela de 5 dias
    (daria a variação de 5 dias), então só vale quando a janela não tem candles."""
    meta = res["meta"]
    deslocamento = timedelta(seconds=int(meta.get("gmtoffset") or 0))
    candles = [((datetime.fromtimestamp(int(ts), UTC) + deslocamento).date(), fech)
               for ts, fech in zip(res.get("timestamp") or [],
                                   (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or [])
               if fech is not None]
    if not candles:
        anterior = meta.get("chartPreviousClose")
        return Decimal(anterior) if anterior else None
    momento = meta.get("regularMarketTime")
    dia_do_preco = (datetime.fromtimestamp(int(momento), UTC) + deslocamento).date() if momento else candles[-1][0]
    antes = [fech for dia, fech in candles if dia < dia_do_preco]
    return Decimal(antes[-1]) if antes else None


class Yahoo:
    def __init__(self, cliente: httpx.Client | None = None):
        self.cliente = cliente or novo_cliente()

    def _resultado(self, simbolo: str, faixa: str, intervalo: str, eventos: str | None = None) -> dict:
        url = URL.format(simbolo=quote(simbolo, safe=""))
        params = {"range": faixa, "interval": intervalo}
        if eventos:
            params["events"] = eventos
        dados = obter_json(self.cliente, url, params)
        try:
            resultado = dados["chart"]["result"][0]
            resultado["meta"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ErroMercado(f"resposta do Yahoo sem dados para {simbolo}") from exc
        if resultado is None:
            raise ErroMercado(f"resposta do Yahoo sem dados para {simbolo}")
        return resultado

    def cotacao_atual(self, simbolo: str) -> tuple[Decimal, str, Decimal | None]:
        """(preço, moeda, variação do dia em %)."""
        res = self._resultado(simbolo, "5d", "1d")
        meta = res["meta"]
        preco = meta.get("regularMarketPrice")
        if preco is None:
            raise ErroMercado(f"Yahoo sem preço para {simbolo}")
        preco = Decimal(preco)
        anterior = _fechamento_anterior(res)
        variacao = None
        if anterior:
            variacao = (preco - Decimal(anterior)) / Decimal(anterior) * 100
        return preco, meta.get("currency") or "BRL", variacao

    def historico_mensal(self, simbolo: str, desde: date, hoje: date | None = None) -> list[tuple[date, Decimal]]:
        """Fechamento do último pregão de cada mês (a partir de `desde`), em ordem cronológica."""
        res = self._resultado(simbolo, _faixa(desde, hoje), "1d")
        deslocamento = timedelta(seconds=int(res["meta"].get("gmtoffset") or 0))
        fechamentos = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        ultimo_do_mes: dict[tuple[int, int], tuple[date, Decimal]] = {}
        for ts, fech in zip(res.get("timestamp") or [], fechamentos):
            if fech is None:
                continue
            dia = (datetime.fromtimestamp(int(ts), UTC) + deslocamento).date()
            if dia < desde:
                continue
            chave = (dia.year, dia.month)
            if chave not in ultimo_do_mes or dia >= ultimo_do_mes[chave][0]:
                ultimo_do_mes[chave] = (dia, Decimal(fech))
        return [ultimo_do_mes[k] for k in sorted(ultimo_do_mes)]

    def proventos(self, simbolo: str, desde: date, hoje: date | None = None) -> list[tuple[date, Decimal]]:
        """Proventos por cota (data ex, valor na moeda da cotação) a partir de `desde`, em ordem cronológica."""
        res = self._resultado(simbolo, _faixa(desde, hoje), "1mo", eventos="div")
        deslocamento = timedelta(seconds=int(res["meta"].get("gmtoffset") or 0))
        eventos = (res.get("events") or {}).get("dividends") or {}
        pontos = []
        for ev in eventos.values():
            dia = (datetime.fromtimestamp(int(ev["date"]), UTC) + deslocamento).date()
            if dia >= desde and ev.get("amount"):
                pontos.append((dia, Decimal(ev["amount"])))
        return sorted(pontos)
