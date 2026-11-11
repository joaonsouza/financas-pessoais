from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.core.periodo import somar_meses, validar_competencia
from app.models import (
    HORIZONTES, AjusteMes, Categoria, Meta, Orcamento, dispensar_alerta, get_metas_503020, set_metas_503020,
)
from app.services.indicadores_curso import painel_503020
from app.services.metas import agendado_da_meta, aporte_mensal, economia_do_mes, progresso_meta, reserva
from app.services.orcamento import LinhaOrcamento, copiar_orcamento, definir_limite, painel_orcamento, teto_mes
from app.services.parcelas import parcelas_do_mes, parcelas_no_gasto_do_mes, visao_parcelas
from app.services.relatorios import NoCategoria, despesas_hierarquia
from app.services.reserva_plano import plano_reserva, projecao_reserva
from app.services.resumo_mes import totais_mes
from app.web.routes.cartoes import _rotulo_mes
from app.web.routes.lancamentos import _categorias_ordenadas
from app.web.routes.metas import ORDENS, _contexto_form, _link, _ordenar, expira_em_breve
from app.web.deps import get_hoje, get_session, mes_selecionado
from app.web.templating import render

router = APIRouter(prefix="/planejamento")


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _int_ou_none(texto) -> int | None:
    texto = str(texto or "").strip()
    return int(texto) if texto.isdigit() else None


def _mes_do_form(form, padrao: str) -> str:
    try:
        return validar_competencia(_campo(form, "mes"))
    except ValueError:
        return padrao


def _voltar(mes: str) -> RedirectResponse:
    return RedirectResponse(f"/planejamento?mes={mes}", status_code=303)


def _dinheiro(texto: str) -> int:
    return parse_reais(texto) if texto.strip() else 0


MAX_CATEGORIAS = 7  # as demais somam numa linha "Outras"


def onde_foi(fatias: list[NoCategoria], linhas: list[LinhaOrcamento],
             ids_filhas: dict[tuple[int, str], int] | None = None) -> list[dict]:
    """Despesas do mês por categoria-raiz, maiores primeiro, com o limite do orçamento quando há e as
    subcategorias (o que foi lançado direto na raiz vira "Direto em ..."). As larguras (0-100) usam uma régua
    só, a do maior valor entre gastos e limites, para a marca do limite e as subcategorias serem comparáveis."""
    limites = {l.categoria.id: l.limite for l in linhas if l.categoria is not None}  # raízes e subcategorias
    ids_filhas = ids_filhas or {}
    itens = []
    for f in fatias[:MAX_CATEGORIAS]:
        # (nome, valor, categoria para a lista de lançamentos); o direto na raiz abre a lista da raiz
        filhas = [(nome, valor, ids_filhas.get((f.categoria_id, nome), f.categoria_id)) for nome, valor in f.filhas]
        direto = f.total - sum(v for _, v, _ in filhas)
        if filhas and direto > 0:
            filhas.append((f"Direto em {f.nome}", direto, f.categoria_id))
        itens.append({"categoria_id": f.categoria_id, "nome": f.nome, "cor": f.cor, "total": f.total,
                      "limite": limites.get(f.categoria_id), "filhas": filhas, "raiz": f.categoria_id})
    resto = fatias[MAX_CATEGORIAS:]
    if resto:
        itens.append({"categoria_id": None, "nome": f"Outras ({len(resto)})", "cor": None,
                      "total": sum(f.total for f in resto), "limite": None,
                      "filhas": [(f.nome, f.total, f.categoria_id) for f in resto], "raiz": None})
    # só a subcategoria de verdade tem limite próprio: o "Direto em ..." e as raízes de "Outras" não herdam
    for i in itens:
        i["filhas"] = [(nome, valor, cid, limites.get(cid) if cid != i["raiz"] and i["raiz"] is not None else None)
                       for nome, valor, cid in i["filhas"]]
    escala = max([i["total"] for i in itens] + [i["limite"] or 0 for i in itens]
                 + [fl[3] or 0 for i in itens for fl in i["filhas"]], default=0)

    def marca(limite):
        return limite * 100 / escala if limite and escala else None

    for i in itens:
        i["largura"] = i["total"] * 100 / escala if escala else 0
        i["marca"] = marca(i["limite"])
        i["filhas"] = [(nome, valor, valor * 100 / escala if escala else 0, cid, marca(lim), lim)
                       for nome, valor, cid, lim in i["filhas"]]
        del i["raiz"]
    return itens


def _grafico_parcelas(s: Session, hoje: date, mes: str) -> dict | None:
    """Série do gráfico do card "Parcelas do mês"; None quando não há parcela nenhuma."""
    visao = visao_parcelas(s, hoje, mes_ref=mes)
    if not visao.meses:
        return None
    return {
        "rotulos": [_rotulo_mes(m.competencia) for m in visao.meses],
        "valores": [m.total / 100 for m in visao.meses],
        "acima": [m.acima for m in visao.meses],
        "passado": [m.passado for m in visao.meses],
        "selecionado": [m.competencia == mes for m in visao.meses],
        "teto": visao.teto / 100,
    }


def tela_planejamento(request: Request, s: Session, mes: str, hoje: date, erro: str | None = None,
                      status_code: int = 200, metas_form: list[str] | None = None, aba: str = "andamento",
                      ordem: str = "prazo", horizonte: str = "", abrir_criar: bool = False):
    ativas = list(s.scalars(select(Categoria).where(Categoria.tipo == "despesa", Categoria.ativa)
                            .order_by(Categoria.nome)))
    todas = {c.id: c for c in s.scalars(select(Categoria))}
    rotulos = {c.id: f"{todas[c.pai_id].nome} › {c.nome}" if c.pai_id else c.nome for c in todas.values()}
    com_limite = {o.categoria_id for o in s.scalars(select(Orcamento).where(Orcamento.competencia == mes))}
    metas = list(s.scalars(select(Meta).order_by(Meta.ativa.desc(), Meta.nome)))
    objetivos = [(m, progresso_meta(s, m, hoje)) for m in metas if m.tipo == "objetivo"]
    # meta que repete todo ano não se conclui: batido o alvo, fica pronta para o vencimento
    andamento = [x for x in objetivos if not x[1].concluido or x[0].repete_anual]
    concluidos = [x for x in objetivos if x[1].concluido and not x[0].repete_anual]
    lista = andamento if aba == "andamento" else concluidos
    if horizonte:
        lista = [x for x in lista if x[0].horizonte == horizonte]
    return render(request, "planejamento/index.html", {
        "mes": mes, "hoje": hoje, "erro": erro, "abrir_criar": abrir_criar,
        "teto": teto_mes(s, mes),
        "linhas": (linhas := painel_orcamento(s, mes)),
        "onde_foi": onde_foi(despesas_hierarquia(s, mes), linhas, {
            (c.pai_id, c.nome): c.id for c in s.scalars(select(Categoria).where(Categoria.pai_id.is_not(None)))}),
        # o mesmo total do teto: inclui os créditos de categorias que ficaram negativas no mês
        "gasto_mes": totais_mes(s, mes).despesas_projetado,
        # raízes com as subcategorias logo abaixo; "— " no prefixo, como no formulário de lançamento
        "sem_limite": [c for c, _ in _categorias_ordenadas(ativas) if c.id not in com_limite],
        "rotulos": rotulos,
        "teto_auto_pct": (teto_pct := 100 - get_metas_503020(s)[2]),
        # o que o 50/30/20 recomenda (o mesmo valor do teto automático), para comparar com um teto salvo
        "teto_recomendado": totais_mes(s, mes).receitas_projetado * teto_pct // 100,
        "painel": painel_503020(s, mes),
        "parcelas_mes": parcelas_do_mes(s, hoje, mes),
        "parcelas_gasto": parcelas_no_gasto_do_mes(s, mes),
        "parcelas_grafico": _grafico_parcelas(s, hoje, mes),
        "metas_pct": metas_form or [str(v) for v in get_metas_503020(s)],
        "ajuste": s.scalar(select(AjusteMes).where(AjusteMes.competencia == mes)),
        **_contexto_form(s, hoje),
        "reservas": [(m, reserva(s, m, hoje), aporte_mensal(s, m, hoje), projecao_reserva(s, m, hoje),
                      plano_reserva(s, m, hoje)) for m in metas
                     if m.tipo == "reserva_emergencia"],
        "economias": [(m, economia_do_mes(s, mes)) for m in metas if m.tipo == "economia_mensal"],
        "itens": [(m, p, agendado_da_meta(s, m), aporte_mensal(s, m, hoje)) for m, p in _ordenar(lista, ordem)],
        "aba": aba, "ordem": ordem, "horizonte": horizonte, "ordens": ORDENS,
        "links": {
            "abas": {a: _link(a, ordem, horizonte, mes) for a in ("andamento", "concluidos")},
            "ordens": {o: _link(aba, o, horizonte, mes) for o in ORDENS},
            "horizontes": {h: _link(aba, ordem, h, mes) for h in ("", *HORIZONTES)},
        },
        "n_andamento": len(andamento), "n_concluidos": len(concluidos),
        "expira": expira_em_breve([x for x in andamento if not x[0].repete_anual], hoje),
        "tem_anterior": bool(s.scalar(select(Orcamento.id).where(Orcamento.competencia == somar_meses(mes, -1)).limit(1))),
    }, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def planejamento(request: Request, aba: str = "andamento", ordem: str = "prazo", horizonte: str = "",
                 mes: str = Depends(mes_selecionado), s: Session = Depends(get_session),
                 hoje: date = Depends(get_hoje)):
    aba = aba if aba in ("andamento", "concluidos") else "andamento"
    ordem = ordem if ordem in ORDENS else "prazo"
    horizonte = horizonte if horizonte in HORIZONTES else ""
    return tela_planejamento(request, s, mes, hoje, aba=aba, ordem=ordem, horizonte=horizonte)


@router.post("/limite")
async def salvar_limite(request: Request, mes_padrao: str = Depends(mes_selecionado),
                        s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    mes = _mes_do_form(form, mes_padrao)
    try:
        definir_limite(s, mes, _int_ou_none(form.get("categoria_id")), _dinheiro(_campo(form, "limite")))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return tela_planejamento(request, s, mes, hoje, erro=str(exc), status_code=422)
    return _voltar(mes)


@router.post("/copiar")
async def copiar(request: Request, mes_padrao: str = Depends(mes_selecionado), s: Session = Depends(get_session)):
    mes = _mes_do_form(await request.form(), mes_padrao)
    copiar_orcamento(s, somar_meses(mes, -1), mes)
    s.commit()
    return _voltar(mes)


@router.post("/metas-503020")
async def salvar_metas(request: Request, mes_padrao: str = Depends(mes_selecionado),
                       s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    mes = _mes_do_form(form, mes_padrao)
    textos = [_campo(form, c) for c in ("necessidades", "desejos", "terceira")]
    try:
        if not all(t.isdigit() for t in textos):
            raise ValueError("informe as três metas em números inteiros")
        set_metas_503020(s, [int(t) for t in textos])
        s.commit()
    except ValueError as exc:
        s.rollback()
        return tela_planejamento(request, s, mes, hoje, erro=str(exc), status_code=422, metas_form=textos)
    return _voltar(mes)


@router.post("/ajuste")
async def salvar_ajuste(request: Request, mes_padrao: str = Depends(mes_selecionado),
                        s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    mes = _mes_do_form(form, mes_padrao)
    ajuste = s.scalar(select(AjusteMes).where(AjusteMes.competencia == mes))
    descricao = _campo(form, "descricao")
    try:
        valor = _dinheiro(_campo(form, "valor_liberado"))
    except ValueError as exc:
        return tela_planejamento(request, s, mes, hoje, erro=str(exc), status_code=422)
    if not descricao:
        if ajuste:
            s.delete(ajuste)
    elif ajuste:
        ajuste.descricao, ajuste.valor_liberado, ajuste.destino = descricao, valor, _campo(form, "destino") or None
    else:
        s.add(AjusteMes(competencia=mes, descricao=descricao, valor_liberado=valor,
                        destino=_campo(form, "destino") or None))
    s.commit()
    return _voltar(mes)


@router.post("/alertas/dispensar")
async def dispensar(request: Request, mes_padrao: str = Depends(mes_selecionado), s: Session = Depends(get_session)):
    form = await request.form()
    mes = _mes_do_form(form, mes_padrao)
    chave = _campo(form, "chave")
    voltar = _campo(form, "voltar")
    # Só caminho deste app: "//site" e "/\site" o navegador lê como outro domínio.
    if not voltar or not voltar.startswith("/") or voltar[1:2] in ("/", "\\"):
        voltar = f"/?mes={mes}"
    if chave:
        dispensar_alerta(s, mes, chave)
        s.commit()
    return RedirectResponse(voltar, status_code=303)
