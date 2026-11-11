from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Meta, get_config
from app.services.faturas import cartoes_ativos, fatura_do_mes, melhor_cartao_hoje, resumo_cartao
from app.services.indicadores_curso import alertas
from app.models.investimento import ALOCACOES
from app.services.invest_carteira import resumo_carteira
from app.services.invest_historico import evolucao_mensal
from app.services.invest_resumo import CORES_CLASSE, cards_resumo
from app.services.metas import economia_do_mes, progresso_meta, reserva
from app.services.orcamento import teto_mes
from app.services.resumo_mes import a_pagar, despesas_por_categoria, totais_mes
from app.services.saldos import saldo_liquido, saldos_contas
from app.services.tela_inicial import COLUNAS, blocos_tela_inicial, salvar_tela_inicial
from app.services.openfinance.sincronizar import itens_configurados, ultima_sincronizacao
from app.web.deps import get_hoje, get_session, mes_selecionado
from app.web.templating import render

router = APIRouter()


def _selo_sincronizacao(s: Session, hoje: date) -> str | None:
    """"há X" desde a última sincronização do Open Finance; só aparece quando há itens configurados."""
    ultima = ultima_sincronizacao(s)
    if ultima is None:
        return "ainda não sincronizado" if itens_configurados(s) else None
    dias = (hoje - ultima).days
    return "hoje" if dias <= 0 else "há 1 dia" if dias == 1 else f"há {dias} dias"


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, mes: str = Depends(mes_selecionado), s: Session = Depends(get_session),
              hoje: date = Depends(get_hoje)):
    saldos = saldos_contas(s, hoje, mes, tipos=("corrente", "investimento"))
    fatias = despesas_por_categoria(s, mes)
    cartoes = cartoes_ativos(s)
    totais = totais_mes(s, mes)
    # objetivos em andamento, os de prazo mais próximo primeiro (sem prazo no fim)
    objetivos = [(m, p) for m in s.scalars(select(Meta).where(Meta.tipo == "objetivo", Meta.ativa))
                 if not (p := progresso_meta(s, m, hoje)).concluido]
    objetivos.sort(key=lambda x: (x[0].prazo is None, x[0].prazo or date.max, x[0].nome.casefold()))
    resumos_cartao = [resumo_cartao(s, c, hoje) for c in cartoes]
    faturas_mes = [fatura_do_mes(s, c, mes, hoje) for c in cartoes]
    blocos = blocos_tela_inicial(s)
    invest = _resumo_investimentos(s, hoje) if any(b.id == "investimentos" and b.visivel for b in blocos) else None
    meta_reserva = s.scalar(select(Meta).where(Meta.tipo == "reserva_emergencia", Meta.ativa).order_by(Meta.id).limit(1))
    return render(request, "dashboard/index.html", {
        "mes": mes,
        "saldos": saldos,
        "liquido_atual": saldo_liquido(saldos),
        "liquido_mes": saldo_liquido(saldos, "previsto_mes"),
        "totais": totais,
        "pendente_despesas": totais.despesas_projetado - totais.despesas_efetivado,
        "pendente_receitas": totais.receitas_projetado - totais.receitas_efetivado,
        "alertas": alertas(s, mes, hoje),
        "teto": teto_mes(s, mes),
        "economia": economia_do_mes(s, mes),
        "reserva": reserva(s, meta_reserva, hoje) if meta_reserva else None,
        "objetivos": objetivos[:4], "n_objetivos": len(objetivos),
        "fatias": fatias,
        "grafico": {
            "rotulos": [f.nome for f in fatias],
            "valores": [f.total / 100 for f in fatias],
            "cores": [f.cor for f in fatias],
        },
        "cartoes": resumos_cartao,
        "faturas_mes": faturas_mes,
        "melhor": melhor_cartao_hoje(s, hoje),
        # faturas que vencem no mês selecionado, como no card de cartões
        "total_cartoes": sum(f["total"] for f in faturas_mes),
        "disponivel_cartoes": sum(r["disponivel"] for r in resumos_cartao if r["limite"]),
        "blocos": blocos,
        "blocos_json": [asdict(b) for b in blocos],
        "colunas": COLUNAS,
        "hoje": hoje,
        "a_pagar": a_pagar(s, hoje),
        "erro_backup": get_config(s, "erro_backup"),
        "erro_migracao_metas": get_config(s, "erro_migracao_metas"),
        "sync_openfinance": _selo_sincronizacao(s, hoje),
        "invest": invest,
    })


def _resumo_investimentos(s: Session, hoje: date) -> dict | None:
    """Cartão da tela inicial: patrimônio, ganho, rentabilidade e alocação atual contra a meta de cada classe."""
    carteira = resumo_carteira(s, hoje)
    if not carteira.ativos:
        return None
    return {
        "cards": cards_resumo(carteira, evolucao_mensal(s, hoje)),
        "classes": [{"rotulo": ALOCACOES[c.classe], "pct": c.pct_atual, "meta": c.meta_pct, "saldo": c.saldo,
                     "cor": CORES_CLASSE[c.classe]} for c in carteira.classes],
    }


@router.post("/tela-inicial")
async def gerenciar_tela_inicial(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("restaurar"):
        salvar_tela_inicial(s, None, None)
    else:
        salvar_tela_inicial(s, form.getlist("ordem"), set(form.getlist("visivel")),
                            {k.removeprefix("coluna_"): str(v) for k, v in form.items() if k.startswith("coluna_")})
    s.commit()
    if request.headers.get("X-Organizar"):  # arrastar na própria tela: salva sem recarregar
        return Response(status_code=204)
    return RedirectResponse("/", status_code=303)
