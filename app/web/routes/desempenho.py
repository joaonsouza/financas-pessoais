"""Rotas da página e parciais da central de Desempenho."""
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.importers.base import LeitorErro
from app.models import get_config, set_config
from app.services.desempenho.alertas_previstos import central_alertas
from app.services.desempenho.analise_ia import obter_analise_ia
from app.services.desempenho.historico import (
    categorias_desempenho_mes,
    serie_fixos_variaveis_dividas,
    serie_historica_categoria,
    serie_patrimonio,
    serie_receitas_despesas,
)
from app.services.desempenho.inflacao import grafico_inflacao, inflacao_pessoal
from app.services.desempenho.insights import gerar_insights
from app.services.desempenho.kpis import calcular_kpis
from app.services.desempenho.placar import calcular_placar
from app.services.desempenho.previsao import calcular_previsao
from app.web.deps import get_hoje, get_session, mes_selecionado
from app.web.templating import render

router = APIRouter(prefix="/desempenho")


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    mes: str = Depends(mes_selecionado),
    hoje: date = Depends(get_hoje),
    s: Session = Depends(get_session),
):
    # Janela selecionada (3, 6 ou 12 meses)
    janela_param = request.query_params.get("janela")
    janela_salva = get_config(s, "desempenho_janela", 12)
    try:
        janela = int(janela_param) if janela_param else int(janela_salva)
        if janela not in (3, 6, 12):
            janela = 12
    except (ValueError, TypeError):
        janela = 12

    if janela_param and int(janela_param) != int(janela_salva):
        set_config(s, "desempenho_janela", janela)
        s.commit()

    # Dados das seções
    kpis = calcular_kpis(s, mes)
    placar = calcular_placar(s, mes, hoje)
    # a previsão de 12 meses é a parte cara: calculada uma vez e repassada aos alertas e aos insights
    previsao12 = calcular_previsao(s, hoje, mes, horizonte_meses=12)
    alertas = central_alertas(s, mes, hoje, previsao=previsao12)
    insights = gerar_insights(s, mes, hoje, previsao=previsao12)
    analise = obter_analise_ia(s, mes, hoje, forcar=False)
    previsao = previsao12 if janela == 12 else calcular_previsao(s, hoje, mes, horizonte_meses=janela)
    rd = serie_receitas_despesas(s, mes, n=janela)
    patrimonio = serie_patrimonio(s, mes, n=janela)
    fixos_vars = serie_fixos_variaveis_dividas(s, mes, n=janela)
    categorias = categorias_desempenho_mes(s, mes)
    inflacao = inflacao_pessoal(s, mes, hoje)

    return render(request, "desempenho/index.html", {
        "mes": mes,
        "hoje": hoje,
        "janela": janela,
        "kpis": kpis,
        "placar": placar,
        "alertas": alertas,
        "insights": insights,
        "analise_ia": analise,
        "previsao": previsao,
        "inflacao": inflacao,
        "dados_graficos": {
            "rd": {
                "rotulos": rd.rotulos,
                "competencias": rd.competencias,
                "receitas_ef": rd.receitas_efetivado,
                "receitas_pend": rd.receitas_pendente,
                "despesas_ef": rd.despesas_efetivado,
                "despesas_pend": rd.despesas_pendente,
                "taxas_poupanca": rd.taxas_poupanca,
            },
            "patrimonio": {
                "rotulos": patrimonio.rotulos,
                "competencias": patrimonio.competencias,
                "saldo_liquido": patrimonio.saldo_liquido,
                "investimentos": patrimonio.investimentos,
                "total": patrimonio.patrimonio_total,
            },
            "fixos_vars": {
                "rotulos": fixos_vars.rotulos,
                "competencias": fixos_vars.competencias,
                "fixos": fixos_vars.fixos,
                "variaveis": fixos_vars.variaveis,
                "dividas": fixos_vars.dividas,
                "media": fixos_vars.media_despesas,
            },
            "previsao_saldo": {
                "rotulos": [p.rotulo for p in previsao.pontos_mensais],
                "competencias": [p.competencia for p in previsao.pontos_mensais],
                "realizado": [p.saldo_realizado / 100 if p.saldo_realizado is not None else None for p in previsao.pontos_mensais],
                "agendado": [p.saldo_agendado / 100 if p.saldo_agendado is not None else None for p in previsao.pontos_mensais],
                "estimado": [p.saldo_estimado / 100 if p.saldo_estimado is not None else None for p in previsao.pontos_mensais],
            },
            "curva_30": {
                "rotulos": [p.rotulo for p in previsao.curva_30_dias],
                "valores": [round(p.saldo / 100, 2) for p in previsao.curva_30_dias],
            },
            "inflacao": grafico_inflacao(inflacao),
        },
        "categorias": categorias,
    })


@router.post("/analise", response_class=HTMLResponse)
async def gerar_analise(
    request: Request,
    mes: str = Depends(mes_selecionado),
    hoje: date = Depends(get_hoje),
    s: Session = Depends(get_session),
):
    form = await request.form()
    forcar = form.get("forcar") == "1"
    erro = None
    analise = None
    try:
        analise = obter_analise_ia(s, mes, hoje, gerar_nova=True, forcar=forcar)
    except LeitorErro as exc:
        erro = str(exc)

    return render(request, "desempenho/_analise_ia.html", {
        "mes": mes,
        "analise_ia": analise,
        "erro_ia": erro,
    })


@router.get("/categoria/{categoria_id}", response_class=HTMLResponse)
def detalhe_categoria(
    categoria_id: int,
    request: Request,
    mes: str = Depends(mes_selecionado),
    s: Session = Depends(get_session),
):
    serie = serie_historica_categoria(s, categoria_id, mes, n=12)
    return render(request, "desempenho/_categoria_modal.html", {
        "mes": mes,
        "serie": serie,
    })
