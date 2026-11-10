import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from app.services.mercado import ErroMercado
from app.services.mercado.bcb import Bcb
from app.services.mercado.brapi import KEYRING_SERVICO, KEYRING_USUARIO, Brapi, token_brapi
from app.services.mercado.yahoo import Yahoo

FIXTURES = Path(__file__).parent / "fixtures" / "mercado"


def carregar(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding="utf-8")


class HttpFalso:
    """Transporte httpx falso: roteia por trecho da URL para fixtures JSON e registra as requisições."""

    def __init__(self, rotas: dict[str, str | Exception]):
        self.rotas = rotas
        self.requisicoes: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requisicoes.append(req)
        for trecho, resposta in self.rotas.items():
            if trecho in str(req.url):
                if isinstance(resposta, Exception):
                    raise resposta
                return httpx.Response(200, text=carregar(resposta) if resposta.endswith(".json") else resposta)
        return httpx.Response(404, text="{}")

    def cliente(self) -> httpx.Client:
        from app.services.mercado import USER_AGENT
        return httpx.Client(transport=httpx.MockTransport(self), headers={"User-Agent": USER_AGENT})


@pytest.fixture(autouse=True)
def sem_rede_real(monkeypatch):
    def proibido(self, request):
        raise AssertionError(f"chamada de rede real: {request.url}")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", proibido)


# ---------- Yahoo ----------

def test_yahoo_cotacao_atual():
    http = HttpFalso({"PETR4.SA": "yahoo_petr4.json"})
    preco, moeda, variacao = Yahoo(http.cliente()).cotacao_atual("PETR4.SA")
    assert preco == D("38.12") and isinstance(preco, D)
    assert moeda == "BRL"
    assert variacao == (D("38.12") - D("38.6")) / D("38.6") * 100   # contra o pregão anterior, não o de antes da janela
    req = http.requisicoes[0]
    assert str(req.url).startswith("https://query1.finance.yahoo.com/v8/finance/chart/PETR4.SA?")
    assert req.url.params["interval"] == "1d" and req.url.params["range"]
    assert "Mozilla" in req.headers["User-Agent"]


def test_yahoo_variacao_do_dia_e_contra_o_pregao_anterior_ao_do_preco():
    """A janela é de 5 dias: o `chartPreviousClose` é o fechamento de antes dela (variação de 5 dias, não do dia)."""
    dia = 86400
    base = 1791205200  # um pregão às 10h de Brasília (gmtoffset -3h)
    resposta = {"chart": {"result": [{
        "meta": {"currency": "BRL", "gmtoffset": -10800, "regularMarketPrice": 103.0, "chartPreviousClose": 80.0,
                 "regularMarketTime": base + 4 * dia + 6 * 3600},
        "timestamp": [base + i * dia for i in range(5)],
        "indicators": {"quote": [{"close": [90.0, 95.0, 98.0, 100.0, 103.0]}]}}], "error": None}}
    http = HttpFalso({"X.SA": json.dumps(resposta)})
    assert Yahoo(http.cliente()).cotacao_atual("X.SA")[2] == (D("103.0") - D("100.0")) / D("100.0") * 100


def test_yahoo_simbolos_especiais_sao_codificados():
    http = HttpFalso({"USDBRL%3DX": "yahoo_usdbrl.json", "%5EBVSP": "yahoo_bvsp.json"})
    y = Yahoo(http.cliente())
    assert y.cotacao_atual("USDBRL=X")[0] == D("5.4321")
    assert y.cotacao_atual("^BVSP")[0] == D("130000.5")


def test_yahoo_exterior_em_dolar():
    http = HttpFalso({"AAPL": "yahoo_aapl.json"})
    assert Yahoo(http.cliente()).cotacao_atual("AAPL")[:2] == (D("227.5"), "USD")


def test_yahoo_historico_mensal_pega_ultimo_pregao_de_cada_mes():
    http = HttpFalso({"PETR4.SA": "yahoo_petr4.json"})
    pontos = Yahoo(http.cliente()).historico_mensal("PETR4.SA", date(2026, 7, 1))
    # 02/10 tem close nulo e é ignorado; 05/10 é o último pregão de outubro nos dados
    assert pontos == [
        (date(2026, 7, 31), D("36.8")), (date(2026, 8, 31), D("37.55")),
        (date(2026, 9, 30), D("38.4")), (date(2026, 10, 5), D("38.12")),
    ]
    assert all(isinstance(v, D) for _, v in pontos)
    assert http.requisicoes[0].url.params["range"] == "max"


def test_yahoo_historico_respeita_desde_e_escolhe_faixa_pela_idade():
    http = HttpFalso({"PETR4.SA": "yahoo_petr4.json"})
    pontos = Yahoo(http.cliente()).historico_mensal("PETR4.SA", date(2026, 9, 1), hoje=date(2026, 10, 5))
    assert [p[0] for p in pontos] == [date(2026, 9, 30), date(2026, 10, 5)]
    assert http.requisicoes[0].url.params["range"] == "1y"


def test_yahoo_erros_viram_erro_mercado():
    with pytest.raises(ErroMercado):
        Yahoo(HttpFalso({"X": "yahoo_vazio.json"}).cliente()).cotacao_atual("X")
    with pytest.raises(ErroMercado):
        Yahoo(HttpFalso({"X": httpx.ConnectError("sem rede")}).cliente()).cotacao_atual("X")
    with pytest.raises(ErroMercado):
        Yahoo(HttpFalso({}).cliente()).cotacao_atual("X")  # 404
    with pytest.raises(ErroMercado):
        Yahoo(HttpFalso({"X": "isto não é json"}).cliente()).cotacao_atual("X")


# ---------- brapi ----------

def test_brapi_cotacao_atual_um_ticker_por_requisicao():
    http = HttpFalso({"brapi.dev/api/quote/PETR4": "brapi_petr4.json"})
    preco, moeda, variacao = Brapi("tok123", http.cliente()).cotacao_atual("PETR4.SA")
    assert (preco, moeda, variacao) == (D("38.5"), "BRL", D("1.25"))
    assert http.requisicoes[0].url.params["token"] == "tok123"
    assert len(http.requisicoes) == 1


def test_brapi_erro():
    with pytest.raises(ErroMercado):
        Brapi("t", HttpFalso({"quote": json.dumps({"results": []})}).cliente()).cotacao_atual("PETR4")


def test_token_brapi_vem_do_keyring(keyring_falso):
    assert token_brapi() is None
    keyring_falso[(KEYRING_SERVICO, KEYRING_USUARIO)] = "segredo"
    assert token_brapi() == "segredo"
    assert (KEYRING_SERVICO, KEYRING_USUARIO) == ("app-financas", "brapi")


# ---------- BCB ----------

def test_bcb_cdi_url_e_parse():
    http = HttpFalso({"sgs.12/dados": "bcb_cdi.json"})
    pontos = Bcb(http.cliente()).cdi(date(2026, 10, 1), date(2026, 10, 5))
    assert pontos == [(date(2026, 10, 1), D("0.040168")), (date(2026, 10, 2), D("0.040168")),
                      (date(2026, 10, 5), D("0.0402"))]
    p = http.requisicoes[0].url.params
    assert p["formato"] == "json" and p["dataInicial"] == "01/10/2026" and p["dataFinal"] == "05/10/2026"
    assert str(http.requisicoes[0].url).startswith("https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados?")


def test_bcb_ipca_serie_433():
    http = HttpFalso({"sgs.433/dados": "bcb_ipca.json"})
    assert Bcb(http.cliente()).ipca(date(2026, 8, 1), date(2026, 9, 30))[1] == (date(2026, 9, 1), D("0.44"))


def test_bcb_janelas_de_ate_10_anos():
    http = HttpFalso({"sgs.12/dados": json.dumps([])})
    Bcb(http.cliente()).cdi(date(2005, 3, 15), date(2026, 10, 5))
    janelas = [(r.url.params["dataInicial"], r.url.params["dataFinal"]) for r in http.requisicoes]
    assert janelas == [("15/03/2005", "14/03/2015"), ("15/03/2015", "14/03/2025"), ("15/03/2025", "05/10/2026")]


def test_bcb_erro_de_rede():
    with pytest.raises(ErroMercado):
        Bcb(HttpFalso({"sgs": httpx.ReadTimeout("lento")}).cliente()).cdi(date(2026, 1, 1), date(2026, 1, 2))


def test_yahoo_proventos_por_cota_desde_a_data():
    resposta = {"chart": {"result": [{"meta": {"currency": "BRL", "gmtoffset": -10800}, "events": {"dividends": {
        "1": {"amount": 1.17, "date": 1785589200},   # 01/08/2026 13h UTC
        "2": {"amount": 1.1, "date": 1782910800},    # 01/07/2026
        "3": {"amount": 0.81, "date": 1751374800},   # 01/07/2025: antes de `desde`, fica de fora
    }}}], "error": None}}
    http = HttpFalso({"HGLG11.SA": json.dumps(resposta)})
    pontos = Yahoo(http.cliente()).proventos("HGLG11.SA", date(2025, 7, 2), date(2026, 8, 5))
    assert pontos == [(date(2026, 7, 1), D("1.1")), (date(2026, 8, 1), D("1.17"))]
    assert "events=div" in str(http.requisicoes[0].url)


def test_yahoo_proventos_sem_eventos_e_lista_vazia():
    resposta = {"chart": {"result": [{"meta": {"currency": "BRL"}}], "error": None}}
    http = HttpFalso({"PACB11.SA": json.dumps(resposta)})
    assert Yahoo(http.cliente()).proventos("PACB11.SA", date(2025, 1, 1)) == []
