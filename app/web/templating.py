from datetime import date
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2.utils import htmlsafe_json_dumps

from decimal import ROUND_HALF_UP, Decimal

from app.core.demo import modo_demo
from app.core.dinheiro import formatar_dolares, formatar_reais
from app.core.quantidade import fmt_qtd
from app.web.marcas import marca
from app.core import relogio
from app.core.periodo import competencia_de, nome_competencia, somar_meses
from app.services.categorias_seletor import opcoes_seletor
from app.services.recorrencia import resumo_regra

PASTA_TEMPLATES = Path(__file__).parent / "templates"

MENU: list[tuple[str, str]] = [
    ("Dashboard", "/"),
    ("Lançamentos", "/lancamentos"),
    ("Contas", "/contas"),
    ("Cartões", "/cartoes"),
    ("Planejamento", "/planejamento"),
    ("Desempenho", "/desempenho"),
    ("Investimentos", "/investimentos"),
    ("Importar", "/importar"),
    ("Configurações", "/configuracoes"),
]

templates = Jinja2Templates(directory=PASTA_TEMPLATES)


def _data_br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _reais_input(centavos: int) -> str:
    inteiro, frac = divmod(abs(centavos), 100)
    return f"{inteiro},{frac:02d}"


def _reais_dec(centavos) -> str:
    """Centavos fracionários (Decimal) em reais; None vira travessão."""
    if centavos is None:
        return "—"
    return formatar_reais(int(Decimal(centavos).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _pct(valor, casas: int = 2) -> str:
    if valor is None:
        return "—"
    texto = f"{Decimal(valor).quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP):,.{casas}f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".") + "%"


def _num_input(valor) -> str:
    """Decimal para campo de formulário: vírgula decimal, sem milhar."""
    return "" if valor is None else fmt_qtd(Decimal(valor)).replace(".", "")


templates.env.filters["reais"] = formatar_reais
templates.env.filters["dolares"] = formatar_dolares
templates.env.filters["reais_dec"] = _reais_dec
templates.env.filters["pct"] = _pct
templates.env.filters["qtd"] = fmt_qtd
templates.env.filters["num_input"] = _num_input
templates.env.filters["data_br"] = _data_br
templates.env.filters["nome_mes"] = nome_competencia
templates.env.filters["regra"] = resumo_regra
# fatura leva o nome do mês do vencimento, como nos bancos
templates.env.filters["mes_de"] = lambda d: nome_competencia(competencia_de(d))
_MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
          "novembro", "dezembro")
templates.env.filters["data_extenso"] = lambda d: f"{d.day} de {_MESES[d.month - 1]} de {d.year}"
templates.env.filters["reais_input"] = _reais_input


def _dados_seletor_categoria(request: Request, tipo: str) -> dict:
    """Categorias do seletor, uma vez por requisição e tipo; `primeira` = a página ainda não tem o JSON."""
    cache = getattr(request.state, "seletor_categoria", None)
    if cache is None:
        cache = request.state.seletor_categoria = {}
    primeira = tipo not in cache
    if primeira:
        with request.app.state.fabrica_sessao() as s:
            dados = opcoes_seletor(s, tipo, relogio.hoje())
        cache[tipo] = {"por_id": dados["por_id"], "json": htmlsafe_json_dumps(dados)}
    return {**cache[tipo], "primeira": primeira}


templates.env.globals["dados_seletor_categoria"] = _dados_seletor_categoria
templates.env.globals["somar_meses"] = somar_meses
templates.env.globals["modo_demo"] = modo_demo
templates.env.globals["mes_hoje"] = lambda: competencia_de(relogio.hoje())
templates.env.globals["marca"] = marca
templates.env.globals["menu"] = MENU
# Open Finance (Pluggy) desativado a pedido do usuário em 2026-10-06; o código continua no projeto.
# Para reativar as telas, troque para True.
OPEN_FINANCE_ATIVO = False
templates.env.globals["open_finance_ativo"] = OPEN_FINANCE_ATIVO
IMPORTAR_PLANILHA_ATIVO = False
templates.env.globals["importar_planilha_ativo"] = IMPORTAR_PLANILHA_ATIVO


def render(request: Request, nome: str, contexto: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, nome, contexto or {}, status_code=status_code)


def versao_css() -> int:
    """Muda quando o app.css é regerado; vai na URL do CSS para o navegador não usar a cópia antiga do cache."""
    try:
        return int((Path(__file__).parent / "static" / "app.css").stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["versao_css"] = versao_css
