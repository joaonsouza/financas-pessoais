from dataclasses import replace
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import avaliar_valor
from app.core.periodo import INTERVALOS_MESES, competencia_de, nome_competencia, somar_meses
from app.models import Caixinha, Categoria, Conta, Fatura, Lancamento, Meta
from app.services.assinaturas import resumo_assinaturas
from app.services.caixinhas import marcar_caixinha
from app.services.consulta_lancamentos import (
    FiltrosLancamento,
    COLUNAS_LISTA,
    agrupar_por_dia,
    linhas_da_lista,
    ordenar_linhas,
    resumo_vencidos,
    totais_da_lista,
)
from app.services.entrada_rapida import NovaEntrada, descricoes_recentes, registrar_entrada, sugestao_por_descricao
from app.services.categorizacao import aprender_categoria
from app.services.faturas import datas_fatura, em_fatura_paga
from app.services.favoritos import alternar_favorito, eh_favorito, listar_favoritos
from app.services.lancamentos import (
    alternar_efetivado,
    atualizar_lancamento,
    pode_trocar_conta,
    converter_em_transferencia,
    editar_lote,
    efetivar_lote,
    excluir_lote,
)
from app.services.recorrencia import custo_mensal_recorrencias
from app.services.saldos import saldo_liquido, saldos_contas
from app.services.series import ESCOPOS, excluir_serie, propagar_edicao, retrato, tipo_serie
from app.web.deps import get_hoje, get_session, mes_selecionado
from app.web.templating import render

router = APIRouter(prefix="/lancamentos")

TITULOS = {
    "despesa": "Nova despesa",
    "receita": "Nova receita",
    "transferencia": "Nova transferência",
    "cartao": "Nova despesa no cartão",
}


def _int_ou_none(valor) -> int | None:
    texto = str(valor or "").strip()
    return int(texto) if texto.isdigit() else None


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _categorias_ordenadas(categorias: list[Categoria]) -> list[tuple[Categoria, int]]:
    resultado = []
    for pai in (c for c in categorias if c.pai_id is None):
        resultado.append((pai, 0))
        resultado.extend((filha, 1) for filha in categorias if filha.pai_id == pai.id)
    return resultado


def _mapas(s: Session) -> dict:
    return {
        "contas_por_id": {c.id: c for c in s.scalars(select(Conta))},
        "categorias_por_id": {c.id: c for c in s.scalars(select(Categoria))},
    }


FATURA_PAGA = "a fatura desta compra já foi paga: valor, data e fatura não mudam (categoria, descrição e tags sim)"


def _recarregar() -> Response:
    return Response(status_code=200, headers={"HX-Refresh": "true"})


def _obter(s: Session, lanc_id: int) -> Lancamento:
    lanc = s.get(Lancamento, lanc_id)
    if lanc is None:
        raise HTTPException(status_code=404, detail="lançamento não encontrado")
    return lanc


POR_PAGINA = (25, 50, 100, 200)
POR_PAGINA_PADRAO = 50


def _link(mes: str, filtros: FiltrosLancamento, ordem: str, direcao: str, por: int = POR_PAGINA_PADRAO,
          pagina: int = 1) -> str:
    """Link da própria tela com filtros, ordem e paginação; os valores padrão não vão na URL."""
    params = {
        "mes": mes, "conta": filtros.conta_id, "categoria": "sem" if filtros.sem_categoria else filtros.categoria_id,
        "status": filtros.status,
        "tag": filtros.tag, "q": filtros.busca, "tipo": filtros.tipo, "ajustes": 1 if filtros.ajustes else None,
    }
    if (ordem, direcao) != ("data", "asc"):
        params |= {"ordem": ordem, "dir": direcao}
    if por != POR_PAGINA_PADRAO:
        params["por"] = por
    if pagina > 1:
        params["pagina"] = pagina
    return "?" + urlencode({k: v for k, v in params.items() if v})


# seletor de tipo: valor do filtro -> (rótulo, cor, ícone); None = todas
TIPOS_SELETOR = {
    None: ("Todas", "#7C3AED", "layers"),
    "receita": ("Receitas", "var(--c-income)", "arrow-down-left"),
    "despesa": ("Despesas", "var(--c-expense)", "arrow-up-right"),
    "transferencia": ("Transferências", "var(--c-primary)", "arrow-left-right"),
    "pagamento_fatura": ("Pagamento de fatura", "var(--c-ink-muted)", "credit-card"),
    "ajuste": ("Ajuste", "var(--c-ink-muted)", "sliders-horizontal"),
}
NOVO_POR_TIPO = {"receita": "Nova receita", "despesa": "Nova despesa", "transferencia": "Nova transferência"}


def _cards(s: Session, mes: str, filtros: FiltrosLancamento, hoje: date, ordem: str, direcao: str,
           por: int) -> list[dict]:
    """Cards de resumo do tipo escolhido. Somam o mês sem o filtro de situação (e, em Todas, sem o de tipo)
    para servirem de atalho: clicar liga o filtro, clicar no ativo desliga."""
    link = lambda **mudar: _link(mes, replace(filtros, **mudar), ordem, direcao, por)
    if filtros.tipo in ("receita", "despesa"):
        linhas = [l for l in linhas_da_lista(s, mes, replace(filtros, status=None)) if not l.ignorar]
        sinal = 1 if filtros.tipo == "receita" else -1
        pendentes = sinal * sum(l.valor for l in linhas if l.status == "pendente")
        feitas = sinal * sum(l.valor for l in linhas if l.status == "efetivado")
        nome, cor = ("Receitas", "income") if filtros.tipo == "receita" else ("Despesas", "expense")
        feitas_rotulo = "recebidas" if filtros.tipo == "receita" else "pagas"
        ativo = filtros.status
        return [
            {"rotulo": f"{nome} pendentes", "valor": pendentes, "cor": "warning",
             "icone": "alert-circle", "ativo": ativo == "pendente",
             "link": link(status=None if ativo == "pendente" else "pendente")},
            {"rotulo": f"{nome} {feitas_rotulo}", "valor": feitas, "cor": "income", "icone": "check",
             "ativo": ativo == "efetivado", "link": link(status=None if ativo == "efetivado" else "efetivado")},
            {"rotulo": "Total", "valor": pendentes + feitas, "cor": cor,
             "icone": "arrow-down-left" if filtros.tipo == "receita" else "arrow-up-right",
             "ativo": False, "link": link(status=None)},
        ]
    receitas, despesas = totais_da_lista(linhas_da_lista(s, mes, replace(filtros, tipo=None)))
    return [
        {"rotulo": "Saldo atual", "valor": saldo_liquido(saldos_contas(s, hoje, mes)), "cor": "primary",
         "icone": "landmark", "ativo": False, "link": f"/contas?mes={mes}"},
        {"rotulo": "Receitas", "valor": receitas, "cor": "income", "icone": "arrow-down-left",
         "ativo": False, "link": link(tipo="receita")},
        {"rotulo": "Despesas", "valor": despesas, "cor": "expense", "icone": "arrow-up-right",
         "ativo": False, "link": link(tipo="despesa")},
        {"rotulo": "Balanço mensal", "valor": receitas - despesas, "cor": "ink", "icone": "scale",
         "ativo": False, "link": None},
    ]


def _colunas(mes: str, filtros: FiltrosLancamento, ordem: str, direcao: str, por: int) -> dict:
    """Link e aria-sort de cada cabeçalho: a coluna ativa inverte, as outras começam na direção delas."""
    inverter = "asc" if direcao == "desc" else "desc"
    return {
        col: {
            "link": _link(mes, filtros, col, inverter if col == ordem else inicial, por),
            "aria": ("descending" if direcao == "desc" else "ascending") if col == ordem else None,
        }
        for col, inicial in COLUNAS_LISTA.items()
    }


def _paginacao(linhas: list, pagina: int, por: int, link) -> tuple[list, dict]:
    """Recorta a página pedida (fora do intervalo vai para a primeira ou a última) e monta o rodapé."""
    total = len(linhas)
    paginas = max(1, -(-total // por))
    pagina = min(max(pagina, 1), paginas)
    inicio = (pagina - 1) * por
    fim = min(inicio + por, total)
    return linhas[inicio:fim], {
        "inicio": inicio + 1 if total else 0, "fim": fim, "total": total, "por": por,
        "anterior": link(por=por, pagina=pagina - 1) if pagina > 1 else None,
        "proxima": link(por=por, pagina=pagina + 1) if pagina < paginas else None,
        "opcoes": [(n, link(por=n)) for n in POR_PAGINA],
    }


@router.get("", response_class=HTMLResponse)
def listar(request: Request, mes: str = Depends(mes_selecionado), s: Session = Depends(get_session),
           hoje: date = Depends(get_hoje),
           conta: str = "", categoria: str = "", tipo: str = "", status: str = "", tag: str = "", q: str = "",
           ajustes: str = "",
           ordem: str = "data", dir: str = "asc", pagina: int = 1, por: int = POR_PAGINA_PADRAO):
    filtros = FiltrosLancamento(
        conta_id=_int_ou_none(conta), categoria_id=_int_ou_none(categoria), sem_categoria=categoria == "sem",
        tipo=tipo if tipo in TIPOS_SELETOR else None,
        status=status if status in ("pendente", "efetivado") else None, tag=tag or None, busca=q or None,
        ajustes=bool(ajustes),
    )
    if ordem not in COLUNAS_LISTA or dir not in ("asc", "desc"):
        ordem, dir = "data", "asc"
    if por not in POR_PAGINA:
        por = POR_PAGINA_PADRAO
    lancamentos = linhas_da_lista(s, mes, filtros)
    categorias = list(s.scalars(select(Categoria).where(Categoria.ativa).order_by(Categoria.nome)))
    mapas = _mapas(s)
    rotulo, cor, icone = TIPOS_SELETOR[filtros.tipo]
    # PC: duas tabelas na ordem escolhida (pendentes inteiras; efetivados paginados); o celular, por dia
    ordenadas = ordenar_linhas(lancamentos, ordem, dir,
                               categorias={i: c.nome for i, c in mapas["categorias_por_id"].items()},
                               contas={i: c.nome for i, c in mapas["contas_por_id"].items()})
    pendentes = [l for l in ordenadas if l.status == "pendente"]
    linhas_efetivadas, paginacao = _paginacao(
        [l for l in ordenadas if l.status != "pendente"],
        pagina, por, lambda **pag: _link(mes, filtros, ordem, dir, **pag))
    por_data = lambda l: (l.data, getattr(l, "id", 0))
    cronologico = sorted(lancamentos, key=por_data)
    grupos_pendentes = agrupar_por_dia([l for l in cronologico if l.status == "pendente"])
    grupos_efetivados = agrupar_por_dia([l for l in cronologico if l.status != "pendente"])
    return render(request, "lancamentos/index.html", {
        "mes": mes,
        "esconder_mes_topo": True,  # a lista tem o próprio seletor de mês, que mantém os filtros
        "link_mes": {d: _link(somar_meses(mes, d), filtros, ordem, dir, por) for d in (-1, 1)},
        "link_mes_hoje": _link(competencia_de(hoje), filtros, ordem, dir, por),
        "tipo_atual": {"rotulo": rotulo, "cor": cor, "icone": icone},
        "tipos_menu": [
            {"valor": t, "rotulo": r, "cor": c, "icone": i, "ativo": t == filtros.tipo,
             "link": _link(mes, replace(filtros, tipo=t), ordem, dir, por)}
            for t, (r, c, i) in TIPOS_SELETOR.items()
        ],
        "novo_rotulo": NOVO_POR_TIPO.get(filtros.tipo),
        "cards": _cards(s, mes, filtros, hoje, ordem, dir, por),
        "atalhos": {"recorrencias": custo_mensal_recorrencias(s), "assinaturas": resumo_assinaturas(s)},
        "abrir_filtros": bool(filtros.conta_id or filtros.categoria_id or filtros.sem_categoria or filtros.status or filtros.tag
                              or filtros.ajustes),
        "link_limpar": _link(mes, FiltrosLancamento(tipo=filtros.tipo), ordem, dir, por),
        "ordem": ordem, "direcao": dir,
        # celular: lista por dia; PC: tabela na ordem escolhida
        "hoje": hoje, "vencidos": resumo_vencidos(s, hoje),
        "grupos_pendentes": grupos_pendentes, "grupos_efetivados": grupos_efetivados,
        "total_pendentes": len(pendentes), "total_efetivados": paginacao["total"],
        "linhas_pendentes": pendentes, "linhas_efetivadas": linhas_efetivadas,
        "paginacao": paginacao,
        "colunas": _colunas(mes, filtros, ordem, dir, por),
        "filtros": filtros,
        "contas": list(s.scalars(select(Conta).where(Conta.ativa).order_by(Conta.nome))),
        "categorias": _categorias_ordenadas(categorias),
        **mapas,
    })


def _opcoes_fatura(hoje: date, atual: str = "") -> list[tuple[str, str]]:
    base = competencia_de(hoje)
    comps = [somar_meses(base, i) for i in range(4)]
    if atual and atual not in comps:
        comps.append(atual)
    return [(c, nome_competencia(c)) for c in sorted(comps)]


def _competencia_fatura(s: Session, lanc: Lancamento) -> str:
    fatura = s.get(Fatura, lanc.fatura_id) if lanc.fatura_id else None
    return fatura.competencia if fatura else ""


def _form_padrao(hoje: date) -> dict:
    return {
        "valor": "", "descricao": "", "data": hoje.isoformat(), "conta_id": "", "conta_destino_id": "",
        "categoria_id": "", "tags": "", "efetivado": True, "repetir": "nao", "parcelas": "2", "observacao": "",
        "intervalo": "1", "fim": "", "regra": "mensal", "unidade": "mes", "fim_tipo": "nunca", "fim_data": "",
        "fim_vezes": "", "dias": [],
        "ignorar": False, "fatura": "", "meta_id": "", "caixinha_id": "",
    }


def _modal(request: Request, s: Session, *, tipo: str, form: dict, hoje: date, acao: str, titulo: str,
           edicao: bool = False, recorrente: bool = False, serie: str | None = None,
           erro: str | None = None, status_code: int = 200,
           sem_fatura: bool = False, salvo: bool = False, recarregar: bool = False, fatura_paga: bool = False,
           troca_conta: Conta | None = None):
    """`troca_conta`: na edição, a conta atual quando o lançamento pode mudar de conta (só para as da mesma moeda)."""
    contas = list(s.scalars(select(Conta).where(Conta.ativa).order_by(Conta.nome)))
    if tipo == "cartao":
        contas_opcoes = [c for c in contas if c.tipo == "cartao"]
    else:
        contas_opcoes = [c for c in contas if c.tipo != "cartao"]
    if edicao and troca_conta is not None:
        contas_opcoes = [c for c in contas_opcoes if c.moeda == troca_conta.moeda]
    tipo_categoria = "receita" if tipo == "receita" else "despesa"
    categorias = list(s.scalars(
        select(Categoria).where(Categoria.ativa, Categoria.tipo == tipo_categoria).order_by(Categoria.nome)
    ))
    return render(request, "lancamentos/_form.html", {
        "salvo": salvo, "recarregar": recarregar,
        "favorito": eh_favorito(s, tipo, str(form.get("descricao", ""))),
        "favoritos": [] if edicao else listar_favoritos(s, tipo),
        "faturas": _opcoes_fatura(hoje, form.get("fatura", "")) if tipo == "cartao" and not sem_fatura else None,
        # nome de cada fatura pelo vencimento do cartão escolhido (como nos bancos)
        "faturas_cartao": {
            str(c.id): [(comp, f"Vence {datas_fatura(c, comp)[1]:%d/%m/%Y}") for comp, _ in
                        _opcoes_fatura(hoje, form.get("fatura", ""))]
            for c in contas_opcoes if c.dia_fechamento and c.dia_vencimento
        } if tipo == "cartao" and not sem_fatura else {},
        "tipo": tipo, "form": form, "hoje": hoje, "acao": acao, "titulo": titulo, "edicao": edicao,
        "conta_fixa": edicao and troca_conta is None,
        "recorrente": recorrente, "serie": serie, "erro": erro, "contas": contas_opcoes, "fatura_paga": fatura_paga,
        "intervalos": INTERVALOS_MESES,
        "contas_reserva": [m.conta_id for m in s.scalars(select(Meta).where(
            Meta.tipo == "reserva_emergencia", Meta.ativa, Meta.conta_id.is_not(None)))],
        "destinos": [c for c in contas if c.tipo != "cartao"],
        "caixinhas_opcoes": [
            {"id": c.id, "nome": c.nome, "conta": c.conta_id, "conta_nome": nome}
            for c, nome in s.execute(select(Caixinha, Conta.nome).join(Conta, Conta.id == Caixinha.conta_id)
                                     .where(Caixinha.ativa).order_by(Conta.nome, Caixinha.nome))],
        "objetivos_opcoes": list(s.scalars(select(Meta).where(Meta.ativa, Meta.tipo != "economia_mensal").order_by(Meta.nome))),
        "categorias": _categorias_ordenadas(categorias),
        "descricoes": descricoes_recentes(s),
    }, status_code=status_code)


@router.get("/novo", response_class=HTMLResponse)
def novo(request: Request, tipo: str = "despesa", conta: str = "", s: Session = Depends(get_session),
         hoje: date = Depends(get_hoje)):
    if tipo not in TITULOS:
        tipo = "despesa"
    form = _form_padrao(hoje)
    if conta.isdigit():
        form["conta_id"] = conta  # atalho "+" de um cartão ou conta: já vem escolhido
    return _modal(request, s, tipo=tipo, form=form, hoje=hoje, acao="/lancamentos", titulo=TITULOS[tipo])


@router.get("/sugestao")
def sugestao(descricao: str = "", s: Session = Depends(get_session)):
    return JSONResponse(sugestao_por_descricao(s, descricao) if descricao.strip() else {})


def _data(form) -> date:
    try:
        return date.fromisoformat(_campo(form, "data"))
    except ValueError as exc:
        raise ValueError("data inválida") from exc


def _tags(form) -> list[str]:
    return [t for t in _campo(form, "tags").split(",") if t.strip()]


def _data_opcional(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError as exc:
        raise ValueError("informe a data de término") from exc


def _vezes_opcional(texto: str) -> int:
    if not texto.isdigit():
        raise ValueError("informe quantas ocorrências")
    return int(texto)


def _entrada_do_form(form) -> NovaEntrada:
    parcelas = _campo(form, "parcelas") or "1"
    if not parcelas.isdigit():
        raise ValueError("número de parcelas inválido")
    intervalo = _campo(form, "intervalo") or "1"
    if not intervalo.isdigit():
        raise ValueError("intervalo inválido")
    return NovaEntrada(
        tipo=_campo(form, "tipo"), valor=avaliar_valor(_campo(form, "valor")), descricao=_campo(form, "descricao"),
        data=_data(form), conta_id=_int_ou_none(form.get("conta_id")),
        conta_destino_id=_int_ou_none(form.get("conta_destino_id")),
        categoria_id=_int_ou_none(form.get("categoria_id")), tags=_tags(form),
        efetivado=bool(form.get("efetivado")), repetir=_campo(form, "repetir") or "nao",
        parcelas=int(parcelas), observacao=_campo(form, "observacao") or None,
        intervalo=int(intervalo), fim=_campo(form, "fim") or None,
        unidade=_campo(form, "unidade") or "mes",
        dias_semana=[int(d) for d in form.getlist("dias") if str(d).isdigit()],
        fim_data=_data_opcional(_campo(form, "fim_data")) if _campo(form, "fim_tipo") == "data" else None,
        fim_vezes=_vezes_opcional(_campo(form, "fim_vezes")) if _campo(form, "fim_tipo") == "vezes" else None,
        ignorar=bool(form.get("ignorar")), fatura_competencia=_campo(form, "fatura") or None,
        checklist_reserva=[str(v) for v in form.getlist("checklist_reserva")] or None,
    )


def _meta_do_form(s: Session, form) -> int | None:
    meta_id = _int_ou_none(form.get("meta_id"))
    if meta_id is None:
        return None
    meta = s.get(Meta, meta_id)
    if meta is None or meta.tipo == "economia_mensal":
        raise ValueError("objetivo inválido")
    return meta.id


def _form_reexibir(form, hoje: date) -> dict:
    dados = _form_padrao(hoje)
    dados.update({chave: str(valor) for chave, valor in form.items()})
    dados["efetivado"] = bool(form.get("efetivado"))
    dados["ignorar"] = bool(form.get("ignorar"))
    dados["dias"] = [str(d) for d in form.getlist("dias")]
    predefinidas = {("mes", "1"): "mensal", ("semana", "1"): "semanal", ("ano", "1"): "anual"}
    dados["regra"] = predefinidas.get((dados["unidade"], dados["intervalo"]), "custom")
    return dados


@router.post("", response_class=HTMLResponse)
async def criar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        meta_id = _meta_do_form(s, form)
        entrada = _entrada_do_form(form)
        entrada.meta_id, entrada.caixinha_id = meta_id, _int_ou_none(form.get("caixinha_id"))
        criados = registrar_entrada(s, entrada, hoje)
        for lanc in criados:
            lanc.meta_id = meta_id
        if _int_ou_none(form.get("caixinha_id")):
            # parcelas/repetições ficam todas na caixinha; transferência: a perna da conta dela
            marcar_caixinha(s, criados, _int_ou_none(form.get("caixinha_id")))
        s.commit()
    except ValueError as exc:
        s.rollback()
        tipo = _campo(form, "tipo") if _campo(form, "tipo") in TITULOS else "despesa"
        return _modal(request, s, tipo=tipo, form=_form_reexibir(form, hoje), hoje=hoje, acao="/lancamentos",
                      titulo=TITULOS[tipo], erro=str(exc), status_code=422,
                      recarregar=bool(form.get("recarregar")))
    if form.get("continuar"):
        tipo = _campo(form, "tipo") if _campo(form, "tipo") in TITULOS else "despesa"
        novo_form = _form_padrao(hoje)
        novo_form.update({"data": _campo(form, "data"), "conta_id": _campo(form, "conta_id")})
        return _modal(request, s, tipo=tipo, form=novo_form, hoje=hoje, acao="/lancamentos",
                      titulo=TITULOS[tipo], salvo=True, recarregar=True)
    return _recarregar()


@router.post("/favoritos", response_class=HTMLResponse)
async def favoritar(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    tipo = _campo(form, "tipo") if _campo(form, "tipo") in TITULOS else "despesa"
    try:
        favorito = alternar_favorito(s, tipo, _campo(form, "descricao"),
                                     _int_ou_none(form.get("categoria_id")), _int_ou_none(form.get("conta_id")))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return HTMLResponse(str(exc), status_code=422)
    return render(request, "lancamentos/_favorito.html", {"favorito": favorito})


def _tipo_do_lancamento(lanc: Lancamento, conta: Conta) -> str:
    if lanc.tipo in ("transferencia", "pagamento_fatura"):
        return "transferencia"
    if conta.tipo == "cartao":
        return "cartao"
    if lanc.tipo in ("receita", "despesa"):
        return lanc.tipo
    return "receita" if lanc.valor > 0 else "despesa"


@router.post("/efetivar-lote")
async def efetivar_em_lote(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    efetivar_lote(s, [int(i) for i in form.getlist("ids") if str(i).isdigit()])
    s.commit()
    return _recarregar()


def _ids(valores) -> list[int]:
    return [int(i) for i in valores if str(i).isdigit()]


def _modal_editar_lote(request: Request, s: Session, ids: list[int], erro: str | None = None,
                       status_code: int = 200):
    categorias = list(s.scalars(select(Categoria).where(Categoria.ativa).order_by(Categoria.nome)))
    return render(request, "lancamentos/_editar_lote.html", {
        "ids": ids, "erro": erro,
        "contas": [c for c in s.scalars(select(Conta).where(Conta.ativa).order_by(Conta.nome))
                   if c.tipo != "cartao"],
        "categorias": _categorias_ordenadas(categorias),
    }, status_code=status_code)


@router.get("/editar-lote", response_class=HTMLResponse)
def editar_lote_modal(request: Request, ids: list[str] = Query(default=[]), s: Session = Depends(get_session)):
    return _modal_editar_lote(request, s, _ids(ids))


@router.post("/editar-lote", response_class=HTMLResponse)
async def editar_em_lote(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    ids = _ids(form.getlist("ids"))
    ignorar = {"sim": True, "nao": False}.get(_campo(form, "ignorar"))
    try:
        editar_lote(s, ids, categoria_id=_int_ou_none(form.get("categoria_id")),
                    conta_id=_int_ou_none(form.get("conta_id")), status=_campo(form, "status") or None,
                    tags=_tags(form), ignorar=ignorar)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _modal_editar_lote(request, s, ids, erro=str(exc), status_code=422)
    return _recarregar()


@router.post("/excluir-lote")
async def excluir_em_lote(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    excluir_lote(s, _ids(form.getlist("ids")))
    s.commit()
    return _recarregar()


def _modal_converter(request: Request, s: Session, lanc: Lancamento, erro: str | None = None,
                     status_code: int = 200):
    contas = [c for c in s.scalars(select(Conta).where(Conta.ativa).order_by(Conta.nome))
              if c.tipo != "cartao" and c.id != lanc.conta_id]
    return render(request, "lancamentos/_converter.html", {
        "l": lanc, "conta": s.get(Conta, lanc.conta_id), "contas": contas, "erro": erro,
    }, status_code=status_code)


@router.get("/{lanc_id}/converter", response_class=HTMLResponse)
def converter_modal(request: Request, lanc_id: int, s: Session = Depends(get_session)):
    return _modal_converter(request, s, _obter(s, lanc_id))


@router.post("/{lanc_id}/converter", response_class=HTMLResponse)
async def converter(request: Request, lanc_id: int, s: Session = Depends(get_session)):
    lanc = _obter(s, lanc_id)
    form = await request.form()
    destino_id = _int_ou_none(form.get("conta_destino_id"))
    try:
        destino = s.get(Conta, destino_id) if destino_id else None
        if destino is None:
            raise ValueError("escolha a outra conta")
        converter_em_transferencia(s, lanc, destino)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _modal_converter(request, s, lanc, erro=str(exc), status_code=422)
    return _recarregar()


@router.get("/{lanc_id}/editar", response_class=HTMLResponse)
def editar(request: Request, lanc_id: int, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    lanc = _obter(s, lanc_id)
    par = s.get(Lancamento, lanc.transferencia_par_id) if lanc.transferencia_par_id else None
    inteiro, frac = divmod(abs(lanc.valor), 100)
    form = {
        **_form_padrao(hoje),
        "valor": f"{inteiro},{frac:02d}", "descricao": lanc.descricao, "data": lanc.data.isoformat(),
        "conta_id": str(lanc.conta_id), "conta_destino_id": "", "categoria_id": str(lanc.categoria_id or ""),
        "tags": ", ".join(t.nome for t in lanc.tags), "efetivado": lanc.status == "efetivado",
        "repetir": "nao", "parcelas": "2", "observacao": lanc.observacao or "",
        "ignorar": lanc.ignorar, "fatura": _competencia_fatura(s, lanc), "meta_id": str(lanc.meta_id or ""),
        "caixinha_id": str(lanc.caixinha_id or (par.caixinha_id if par else "") or ""),
    }
    if par is not None:
        form["conta_destino_id"] = str(par.conta_id)  # só para oferecer as caixinhas das duas contas
    return _modal(request, s, tipo=_tipo_do_lancamento(lanc, s.get(Conta, lanc.conta_id)), form=form, hoje=hoje,
                  acao=f"/lancamentos/{lanc.id}", titulo="Editar lançamento", edicao=True,
                  recorrente=lanc.recorrencia_id is not None, serie=tipo_serie(s, lanc),
                  sem_fatura=lanc.grupo_parcela_id is not None, fatura_paga=em_fatura_paga(s, lanc),
                  troca_conta=_troca_conta(s, lanc))


def _troca_conta(s: Session, lanc: Lancamento) -> Conta | None:
    return s.get(Conta, lanc.conta_id) if pode_trocar_conta(s, lanc) else None


@router.post("/{lanc_id}", response_class=HTMLResponse)
async def salvar_edicao(request: Request, lanc_id: int, s: Session = Depends(get_session),
                        hoje: date = Depends(get_hoje)):
    lanc = _obter(s, lanc_id)
    form = await request.form()
    categoria_antes = lanc.categoria_id
    antes = retrato(lanc)
    travada = em_fatura_paga(s, lanc)
    no_total = (lanc.valor, lanc.data, lanc.fatura_id, lanc.ignorar)
    try:
        atualizar_lancamento(
            s, lanc, data=_data(form), descricao=_campo(form, "descricao"),
            valor_abs=avaliar_valor(_campo(form, "valor")), categoria_id=_int_ou_none(form.get("categoria_id")),
            tags=_tags(form), observacao=_campo(form, "observacao") or None,
            ignorar=bool(form.get("ignorar")), fatura_competencia=_campo(form, "fatura") or None,
            conta_id=_int_ou_none(form.get("conta_id")),
        )
        if travada and (lanc.valor, lanc.data, lanc.fatura_id, lanc.ignorar) != no_total:
            raise ValueError(FATURA_PAGA)
        # o form do cartão não tem "efetivado": a edição não mexe no status da compra
        compra_no_cartao = _tipo_do_lancamento(lanc, s.get(Conta, lanc.conta_id)) == "cartao"
        if not compra_no_cartao and bool(form.get("efetivado")) != (lanc.status == "efetivado"):
            alternar_efetivado(s, lanc)
        lanc.meta_id = _meta_do_form(s, form)
        pernas = [lanc]
        if lanc.transferencia_par_id:
            pernas.append(s.get(Lancamento, lanc.transferencia_par_id))
            pernas[1].meta_id = lanc.meta_id
        marcar_caixinha(s, pernas, _int_ou_none(form.get("caixinha_id")))
        propagar_edicao(s, lanc, antes, _campo(form, "escopo"))  # só este, este e os próximos, ou todos
        if lanc.categoria_id and lanc.categoria_id != categoria_antes:
            aprender_categoria(s, lanc)  # aplica nos iguais sem categoria e cria regra para as próximas
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _modal(request, s, tipo=_tipo_do_lancamento(lanc, s.get(Conta, lanc.conta_id)),
                      form=_form_reexibir(form, hoje), hoje=hoje, acao=f"/lancamentos/{lanc.id}",
                      titulo="Editar lançamento", edicao=True, recorrente=lanc.recorrencia_id is not None,
                      serie=tipo_serie(s, lanc), sem_fatura=lanc.grupo_parcela_id is not None,
                      fatura_paga=em_fatura_paga(s, lanc), erro=str(exc), status_code=422,
                      troca_conta=_troca_conta(s, lanc))
    return _recarregar()


@router.post("/{lanc_id}/alternar", response_class=HTMLResponse)
def alternar(request: Request, lanc_id: int, formato: str = "", s: Session = Depends(get_session),
             hoje: date = Depends(get_hoje)):
    lanc = _obter(s, lanc_id)
    tipo_antes = lanc.tipo
    try:
        lanc = alternar_efetivado(s, lanc)
        s.commit()
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=422)
    if lanc.tipo != tipo_antes:
        return _recarregar()  # a parcela do aporte da meta trocou de despesa para transferência (ou voltou)
    modelo = "lancamentos/_linha_tabela.html" if formato == "tabela" else "lancamentos/_linha.html"
    return render(request, modelo, {"l": lanc, "hoje": hoje, **_mapas(s)})


@router.get("/{lanc_id}/excluir", response_class=HTMLResponse)
def excluir_modal(request: Request, lanc_id: int, s: Session = Depends(get_session)):
    """Pergunta o alcance da exclusão de lançamento recorrente ou parcelado."""
    lanc = _obter(s, lanc_id)
    return render(request, "lancamentos/_excluir_serie.html", {"l": lanc, "serie": tipo_serie(s, lanc)})


@router.post("/{lanc_id}/excluir")
async def excluir(request: Request, lanc_id: int, s: Session = Depends(get_session)):
    form = await request.form()
    escopo = _campo(form, "escopo")
    try:
        excluir_serie(s, _obter(s, lanc_id), escopo if escopo in ESCOPOS else "este")
        s.commit()
    except ValueError as exc:
        s.rollback()
        return HTMLResponse(str(exc), status_code=422)
    return _recarregar()
