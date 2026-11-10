from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import relogio
from app.core.dinheiro import formatar_reais, parse_reais
from app.models import Categoria, Lancamento, RegraCategoria, get_config, get_metas_503020, set_config, set_metas_503020
from app.importers.base import LeitorErro
from app.services.categorizar_ia import (
    NovaSugestao, Proposta, aplicar_categorizacao, categorias_usaveis, propor_categorizacao, rotulo_categoria,
)
from app.services.gestao_categorias import (
    apagar_regra, arvore, definir_mae, impacto, regras_da_categoria, remover_categoria, trocar_regra, uso_por_categoria,
)
from app.services.organizar_categorias import ItemProposta, aplicar_hierarquia, propor_hierarquia
from app.services.parcelas import definir_teto_parcelas, teto_parcelas
from app.services.visao_configuracoes import visao_geral
from app.services.ia import (
    MODELO_PADRAO, PROVEDORES, cliente_e_modelo_de_texto, obter_modelo, obter_modelo_gemini, obter_provedor,
    remover_chave, salvar_chave, ultimos4_da_chave,
)
from app.services.gemini import (
    MODELO_GEMINI_PADRAO, remover_chave_gemini, salvar_chave_gemini, ultimos4_gemini,
)
from app.services.backup import PASTA_BACKUP_PADRAO, fazer_backup, listar_backups, restaurar_backup
from app.services.mercado.brapi import remover_token, salvar_token, ultimos4_do_token
from app.services.openfinance.pluggy import (
    ClientePluggy, OpenFinanceErro, criar_cliente, remover_chaves, salvar_chaves, ultimos4_do_client_id,
)
from app.services.openfinance.sincronizar import (
    adicionar_item, consultar_itens, itens_configurados, remover_item, status_dos_itens,
)
from app.web.deps import Provedores, get_agora, get_hoje, get_provedores, get_session
from app.web.templating import render

router = APIRouter(prefix="/configuracoes")

ABAS = ("geral", "categorias", "preferencias", "backup", "ia", "investimentos", "openfinance")
GRUPOS = ("fixo", "variavel", "divida")
NATUREZAS = ("necessidade", "desejo")


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _int_ou_none(texto: str) -> int | None:
    return int(texto) if texto.isdigit() else None


def _pasta_backup(s: Session) -> Path:
    return Path(get_config(s, "pasta_backup", str(PASTA_BACKUP_PADRAO)))


def _tela(request: Request, s: Session, hoje: date, aba: str = "categorias", erro: str | None = None,
          mensagem: str | None = None, status_code: int = 200, proposta: list[ItemProposta] | None = None,
          categorizacao: Proposta | None = None, tipo: str = "despesa", cat: str = "",
          prefs_form: dict | None = None):
    if aba not in ABAS:
        aba = "geral"
    sel = s.get(Categoria, int(cat)) if cat.isdigit() else None
    if sel is not None:
        tipo = sel.tipo  # o painel aberto decide a aba Despesas/Receitas
    tipo = tipo if tipo in ("despesa", "receita") else "despesa"
    galhos = arvore(s, tipo) if aba == "categorias" else []
    categorias = list(s.scalars(select(Categoria).order_by(Categoria.tipo, Categoria.nome)))
    pais = [c for c in categorias if c.pai_id is None]
    pasta = _pasta_backup(s)
    return render(request, "configuracoes/index.html", {
        "aba": aba, "erro": erro, "mensagem": mensagem, "hoje": hoje,
        "sem_categoria": s.scalar(select(func.count(Lancamento.id)).where(
            Lancamento.categoria_id.is_(None), Lancamento.tipo.in_(("despesa", "receita")),
            Lancamento.status != "cancelado")),
        "categorias": [(p, [c for c in categorias if c.pai_id == p.id]) for p in pais],
        "pais": pais, "proposta": proposta, "categorizacao": categorizacao,
        # árvore e painel da aba Categorias
        "tipo_cat": tipo, "arvore": galhos, "uso": uso_por_categoria(s, hoje) if aba == "categorias" else {},
        "cat_sel": sel, "cat_nova": cat == "nova",
        "impacto_sel": impacto(s, sel) if sel else None,
        "regras_sel": regras_da_categoria(s, sel.id) if sel else [],
        "maes_ids": [m.id for m, _ in galhos if m.ativa and (sel is None or m.id != sel.id)],
        "opcoes_categoria": {
            tipo: [(c.id, rotulo_categoria(c, {x.id: x for x in categorias}))
                   for c in categorias_usaveis(s, tipo)] for tipo in ("despesa", "receita")
        } if categorizacao else {},
        "pais_sugeridos": sorted({p.nome for p in pais if p.tipo == "despesa"}
                                 | {i.pai for i in proposta or [] if i.pai}),
        "pasta_backup": str(pasta), "backups": [p.name for p in listar_backups(pasta)],
        # visão geral (Open Finance desligado também cai nela, pela tela)
        "visao": visao_geral(s, pasta, request.app.state.db_path is not None) if aba in ("geral", "openfinance") else [],
        "prefs": prefs_form or {"teto": formatar_reais(teto_parcelas(s)).removeprefix("R$ "),
                                **dict(zip(("necessidades", "desejos", "terceira"), map(str, get_metas_503020(s))))},
        "backup_disponivel": request.app.state.db_path is not None,
        # a chave nunca volta para a tela: só se existe e os 4 últimos caracteres
        "ia_ultimos4": ultimos4_da_chave() if aba == "ia" else None,
        "ia_modelo": obter_modelo(s), "ia_modelo_padrao": MODELO_PADRAO,
        "ia_provedor": obter_provedor(s) if aba == "ia" else None,
        "gemini_ultimos4": ultimos4_gemini() if aba == "ia" else None,
        "gemini_modelo": obter_modelo_gemini(s), "gemini_modelo_padrao": MODELO_GEMINI_PADRAO,
        "brapi_ultimos4": ultimos4_do_token() if aba == "investimentos" else None,
        # Open Finance: o Client ID só aparece pelos 4 últimos caracteres; o Secret nunca volta para a tela
        "of_ultimos4": ultimos4_do_client_id() if aba == "openfinance" else None,
        "of_itens": itens_configurados(s) if aba == "openfinance" else [],
        "of_status": status_dos_itens(s) if aba == "openfinance" else {},
    }, status_code=status_code)


def _voltar(aba: str) -> RedirectResponse:
    return RedirectResponse(f"/configuracoes?aba={aba}", status_code=303)


def _voltar_categoria(cat: Categoria) -> RedirectResponse:
    return RedirectResponse(f"/configuracoes?aba=categorias&tipo={cat.tipo}&cat={cat.id}", status_code=303)


COOKIE_AVISO = "aviso_configuracoes"


def _voltar_com_aviso(url: str, texto: str) -> RedirectResponse:
    """Depois de um POST que muda dados: redireciona (F5 não reenvia) e a mensagem vai num cookie de uso único."""
    resposta = RedirectResponse(url, status_code=303)
    resposta.set_cookie(COOKIE_AVISO, quote(texto), max_age=60, httponly=True, samesite="lax")
    return resposta


@router.get("", response_class=HTMLResponse)
def configuracoes(request: Request, aba: str = "geral", tipo: str = "despesa", cat: str = "",
                  s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    if aba == "recorrencias":
        return RedirectResponse("/lancamentos/recorrencias", status_code=303)
    aviso = request.cookies.get(COOKIE_AVISO)
    resposta = _tela(request, s, hoje, aba, tipo=tipo, cat=cat, mensagem=unquote(aviso) if aviso else None)
    if aviso:
        resposta.delete_cookie(COOKIE_AVISO)
    return resposta


# ---------- preferências ----------

@router.post("/preferencias", response_class=HTMLResponse)
async def salvar_preferencias(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    """Teto de parcelas e metas do 50/30/20: os mesmos valores de Cartões › Parcelas e de Planejamento."""
    form = await request.form()
    prefs = {c: _campo(form, c) for c in ("teto", "necessidades", "desejos", "terceira")}
    try:
        metas = [prefs[c] for c in ("necessidades", "desejos", "terceira")]
        if not all(m.isdigit() for m in metas):
            raise ValueError("informe as três metas do 50/30/20 em números inteiros")
        definir_teto_parcelas(s, parse_reais(prefs["teto"]))
        set_metas_503020(s, [int(m) for m in metas])
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, "preferencias", erro=str(exc), status_code=422, prefs_form=prefs)
    return _voltar("preferencias")


# ---------- categorias ----------

def _dados_categoria(form, tipo: str) -> dict:
    nome = _campo(form, "nome")
    if not nome:
        raise ValueError("informe o nome da categoria")
    grupo = _campo(form, "grupo") or None
    natureza = _campo(form, "natureza") or None
    if grupo and grupo not in GRUPOS:
        raise ValueError("grupo inválido")
    if natureza and natureza not in NATUREZAS:
        raise ValueError("natureza inválida")
    dados = {"nome": nome, "cor": _campo(form, "cor") or "#64748b", "essencial": bool(form.get("essencial"))}
    if tipo == "receita":
        dados.update(grupo=None, natureza=None, essencial=False, inflacao=None)
    else:
        dados.update(grupo=grupo, natureza=natureza)
        if "inflacao" in form:  # formulário sem o campo não mexe na escolha
            inflacao = _campo(form, "inflacao") or None
            if inflacao not in (None, "fixo", "consumo"):
                raise ValueError("opção de inflação pessoal inválida")
            dados["inflacao"] = inflacao
    return dados


@router.post("/categorizar", response_class=HTMLResponse)
def categorizar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    from app.services.categorizacao import categorizar_pendentes
    n = categorizar_pendentes(s)
    s.commit()
    return _tela(request, s, hoje, "categorias",
                 mensagem=f"{n} lançamento(s) categorizado(s) por regra, histórico ou loja conhecida.")


def _cliente_ia(request: Request, s: Session) -> tuple[object, str]:
    """(cliente, modelo) das funções de texto. Cliente injetável (testes); sem chave, `LeitorErro` avisando."""
    cliente = getattr(request.app.state, "cliente_ia", None)
    if cliente is not None:
        return cliente, obter_modelo(s)
    return cliente_e_modelo_de_texto(s)


@router.post("/categorizar/ia", response_class=HTMLResponse)
def categorizar_com_ia_tela(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    """Métodos locais + IA para os lançamentos sem categoria; a pessoa revisa antes de aplicar."""
    try:
        cliente, modelo = _cliente_ia(request, s)
        proposta = propor_categorizacao(s, cliente, modelo)
    except LeitorErro as exc:
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400)
    if not proposta.linhas:
        return _tela(request, s, hoje, "categorias", mensagem="Não há lançamentos sem categoria.")
    return _tela(request, s, hoje, "categorias", categorizacao=proposta, erro=proposta.aviso)


@router.post("/categorizar/ia/aplicar", response_class=HTMLResponse)
async def aplicar_categorizacao_ia(request: Request, s: Session = Depends(get_session),
                                   hoje: date = Depends(get_hoje)):
    form = await request.form()
    escolhas = []
    for i in form.getlist("ok"):
        escolha = _campo(form, f"cat_{i}")
        tipo, chave = _campo(form, f"tipo_{i}"), _campo(form, f"chave_{i}")
        if escolha == "nova":  # a categoria que a IA propôs criar
            escolhas.append((tipo, chave, NovaSugestao(
                _campo(form, f"nova_nome_{i}"), _campo(form, f"nova_pai_{i}") or None,
                _campo(form, f"nova_grupo_{i}"), _campo(form, f"nova_natureza_{i}"))))
        elif _int_ou_none(escolha):
            escolhas.append((tipo, chave, int(escolha)))
    if not escolhas:
        return _tela(request, s, hoje, "categorias", erro="marque ao menos uma descrição com categoria escolhida",
                     status_code=400)
    try:
        lancamentos, regras, novas = aplicar_categorizacao(s, escolhas)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400)
    return _tela(request, s, hoje, "categorias", mensagem=(
        f"{lancamentos} lançamento(s) categorizado(s); {regras} regra(s) criada(s) para as próximas vezes"
        + (f"; {novas} categoria(s) nova(s) criada(s)." if novas else ".")))


@router.post("/categorias/organizar", response_class=HTMLResponse)
def organizar_categorias(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    """A IA propõe pais e subcategorias; nada muda até a pessoa aplicar."""
    try:
        cliente, modelo = _cliente_ia(request, s)
        itens = propor_hierarquia(s, cliente, modelo)
    except LeitorErro as exc:
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400)
    if not itens:
        return _tela(request, s, hoje, "categorias", mensagem="Não há categorias de despesa soltas para organizar.")
    return _tela(request, s, hoje, "categorias", proposta=itens)


@router.post("/categorias/organizar/aplicar", response_class=HTMLResponse)
async def aplicar_organizacao(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    itens = []
    for texto in form.getlist("aplicar"):
        if not str(texto).isdigit():
            continue
        cid = int(texto)
        categoria = s.get(Categoria, cid)
        if categoria is None:
            continue
        itens.append(ItemProposta(cid, categoria.nome, _campo(form, f"pai_{cid}") or None,
                                  _campo(form, f"grupo_{cid}"), _campo(form, f"natureza_{cid}")))
    if not itens:
        return _tela(request, s, hoje, "categorias", erro="marque ao menos uma categoria para aplicar", status_code=400)
    db_path = request.app.state.db_path
    try:
        if db_path is not None:  # cópia de segurança antes de mexer na estrutura
            fazer_backup(db_path, _pasta_backup(s), relogio.agora().replace(tzinfo=None))
        r = aplicar_hierarquia(s, itens)
        s.commit()
    except (ValueError, OSError) as exc:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400)
    return _tela(request, s, hoje, "categorias", mensagem=(
        f"{r.movidas} categoria(s) movida(s) para {r.pais_criados} pai(s) novo(s); "
        f"tipo e natureza atualizados em {r.atributos}."))


@router.post("/categorias", response_class=HTMLResponse)
async def nova_categoria(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        tipo = _campo(form, "tipo")
        if tipo not in ("receita", "despesa"):
            raise ValueError("tipo de categoria inválido")
        nova = Categoria(tipo=tipo, pai_id=_int_ou_none(_campo(form, "pai_id")), **_dados_categoria(form, tipo))
        s.add(nova)
        s.commit()
    except IntegrityError:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro="já existe uma categoria com esse nome", status_code=400,
                     tipo=_campo(form, "tipo"), cat="nova")
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400, tipo=_campo(form, "tipo"),
                     cat="nova")
    return _voltar_categoria(nova)


@router.post("/categorias/{categoria_id}", response_class=HTMLResponse)
async def editar_categoria(request: Request, categoria_id: int, s: Session = Depends(get_session),
                           hoje: date = Depends(get_hoje)):
    categoria = s.get(Categoria, categoria_id)
    if categoria is None:
        raise HTTPException(status_code=404, detail="categoria não encontrada")
    form = await request.form()
    try:
        for campo, valor in _dados_categoria(form, categoria.tipo).items():
            setattr(categoria, campo, valor)
        categoria.ativa = bool(form.get("ativa"))
        if "pai_id" in form:
            definir_mae(s, categoria, _int_ou_none(_campo(form, "pai_id")))
        s.commit()
    except IntegrityError:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro="já existe uma categoria com esse nome", status_code=400,
                     cat=str(categoria_id))
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400, cat=str(categoria_id))
    return _voltar_categoria(categoria)


@router.post("/categorias/{categoria_id}/mover")
async def mover_categoria(request: Request, categoria_id: int, s: Session = Depends(get_session)):
    """Arrastar na árvore: nova mãe (ou nenhuma, vazio)."""
    categoria = s.get(Categoria, categoria_id)
    if categoria is None:
        raise HTTPException(status_code=404, detail="categoria não encontrada")
    try:
        definir_mae(s, categoria, _int_ou_none(_campo(await request.form(), "pai_id")))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return JSONResponse({"erro": str(exc)}, status_code=400)
    return {"ok": True}


@router.post("/categorias/{categoria_id}/remover", response_class=HTMLResponse)
async def remover(request: Request, categoria_id: int, s: Session = Depends(get_session),
                  hoje: date = Depends(get_hoje)):
    """Juntar com `destino_id` (tudo passa para ela) ou, vazio, excluir deixando tudo sem categoria."""
    categoria = s.get(Categoria, categoria_id)
    if categoria is None:
        raise HTTPException(status_code=404, detail="categoria não encontrada")
    destino_id = _int_ou_none(_campo(await request.form(), "destino_id"))
    destino = s.get(Categoria, destino_id) if destino_id else None
    nome, tipo, db_path = categoria.nome, categoria.tipo, request.app.state.db_path
    try:
        if destino_id and destino is None:
            raise ValueError("categoria de destino não encontrada")
        if db_path is not None:  # cópia de segurança antes de mexer em tudo que aponta para a categoria
            fazer_backup(db_path, _pasta_backup(s), relogio.agora().replace(tzinfo=None))
        efeito = remover_categoria(s, categoria, destino)
        s.commit()
    except (ValueError, OSError) as exc:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400, cat=str(categoria_id))
    if destino is not None:
        texto = f"“{nome}” juntada em “{destino.nome}”: {efeito.texto()} passaram para ela."
        return _voltar_com_aviso(f"/configuracoes?aba=categorias&tipo={tipo}&cat={destino.id}", texto)
    texto = f"“{nome}” excluída" + (f"; {efeito.texto()} ficaram sem categoria." if efeito.total else ".")
    return _voltar_com_aviso(f"/configuracoes?aba=categorias&tipo={tipo}", texto)


@router.post("/regras/{regra_id}", response_class=HTMLResponse)
async def trocar_regra_de_categoria(request: Request, regra_id: int, s: Session = Depends(get_session),
                                    hoje: date = Depends(get_hoje)):
    regra = s.get(RegraCategoria, regra_id)
    if regra is None:
        raise HTTPException(status_code=404, detail="regra não encontrada")
    origem = s.get(Categoria, regra.categoria_id)
    try:
        trocar_regra(s, regra, _int_ou_none(_campo(await request.form(), "categoria_id")) or 0)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, "categorias", erro=str(exc), status_code=400, cat=str(origem.id))
    return _voltar_categoria(origem)


@router.post("/regras/{regra_id}/apagar", response_class=HTMLResponse)
def apagar_regra_de_categoria(regra_id: int, s: Session = Depends(get_session)):
    regra = s.get(RegraCategoria, regra_id)
    if regra is None:
        raise HTTPException(status_code=404, detail="regra não encontrada")
    origem = s.get(Categoria, regra.categoria_id)
    apagar_regra(s, regra)
    s.commit()
    return _voltar_categoria(origem)


# ---------- backup ----------

@router.post("/backup/pasta", response_class=HTMLResponse)
async def definir_pasta(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    pasta = _campo(form, "pasta")
    if not pasta:
        return _tela(request, s, hoje, "backup", erro="informe a pasta de backup", status_code=400)
    set_config(s, "pasta_backup", pasta)
    s.commit()
    return _voltar("backup")


@router.post("/backup", response_class=HTMLResponse)
def backup_agora(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    db_path = request.app.state.db_path
    if db_path is None:
        return _tela(request, s, hoje, "backup", erro="backup indisponível para banco em memória", status_code=400)
    try:
        fazer_backup(db_path, _pasta_backup(s), relogio.agora().replace(tzinfo=None))
        set_config(s, "erro_backup", None)
        s.commit()
    except OSError as exc:
        return _tela(request, s, hoje, "backup", erro=f"falha no backup: {exc}", status_code=400)
    return _voltar("backup")


@router.post("/backup/restaurar", response_class=HTMLResponse)
async def restaurar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    nome = _campo(form, "arquivo")
    pasta = _pasta_backup(s)
    disponiveis = {p.name: p for p in listar_backups(pasta)}
    db_path = request.app.state.db_path
    if db_path is None or nome not in disponiveis:
        return _tela(request, s, hoje, "backup", erro="arquivo de backup inválido", status_code=400)
    s.close()
    restaurar_backup(disponiveis[nome], db_path)
    request.app.state.engine.dispose()
    return _voltar("backup")


# ---------- IA (Claude API) ----------

@router.post("/ia/chave", response_class=HTMLResponse)
async def guardar_chave(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        salvar_chave(_campo(form, "chave"))
    except ValueError as exc:
        return _tela(request, s, hoje, "ia", erro=str(exc), status_code=400)
    return _voltar("ia")


@router.post("/ia/chave/remover", response_class=HTMLResponse)
def apagar_chave(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    try:
        remover_chave()
    except ValueError as exc:
        return _tela(request, s, hoje, "ia", erro=str(exc), status_code=400)
    return _voltar("ia")


@router.post("/ia/provedor", response_class=HTMLResponse)
async def definir_provedor(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    provedor = _campo(form, "provedor")
    if provedor not in PROVEDORES:
        return _tela(request, s, hoje, "ia", erro="provedor de IA inválido", status_code=400)
    set_config(s, "provedor_ia", provedor)
    s.commit()
    return _voltar("ia")


@router.post("/ia/gemini/chave", response_class=HTMLResponse)
async def guardar_chave_gemini(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        salvar_chave_gemini(_campo(form, "chave"))
    except ValueError as exc:
        return _tela(request, s, hoje, "ia", erro=str(exc), status_code=400)
    return _voltar("ia")


@router.post("/ia/gemini/chave/remover", response_class=HTMLResponse)
def apagar_chave_gemini(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    try:
        remover_chave_gemini()
    except ValueError as exc:
        return _tela(request, s, hoje, "ia", erro=str(exc), status_code=400)
    return _voltar("ia")


@router.post("/ia/gemini/modelo", response_class=HTMLResponse)
async def definir_modelo_gemini(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    set_config(s, "modelo_gemini", _campo(form, "modelo") or MODELO_GEMINI_PADRAO)
    s.commit()
    return _voltar("ia")


@router.post("/ia/modelo", response_class=HTMLResponse)
async def definir_modelo(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    set_config(s, "modelo_ia", _campo(form, "modelo") or MODELO_PADRAO)
    s.commit()
    return _voltar("ia")


# ---------- investimentos ----------

@router.post("/investimentos/token", response_class=HTMLResponse)
async def guardar_token_brapi(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        salvar_token(_campo(form, "token"))
    except ValueError as exc:
        return _tela(request, s, hoje, "investimentos", erro=str(exc), status_code=400)
    return _voltar("investimentos")


@router.post("/investimentos/token/remover", response_class=HTMLResponse)
def apagar_token_brapi(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    try:
        remover_token()
    except ValueError as exc:
        return _tela(request, s, hoje, "investimentos", erro=str(exc), status_code=400)
    return _voltar("investimentos")


@router.post("/investimentos/historico", response_class=HTMLResponse)
def atualizar_historico(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje),
                        agora=Depends(get_agora), prov: Provedores = Depends(get_provedores)):
    from app.web.routes.investimentos import atualizar_mercado

    falhas = atualizar_mercado(s, prov, agora, hoje, forcar=True)
    if falhas:
        return _tela(request, s, hoje, "investimentos", erro="Não foi possível atualizar: " + ", ".join(falhas)
                     + ". Os dados em cache foram mantidos.", status_code=502)
    return _tela(request, s, hoje, "investimentos", mensagem="Histórico de cotações atualizado.")


# ---------- Open Finance (Pluggy) ----------

def _cliente_pluggy(request: Request) -> ClientePluggy | None:
    """Cliente injetável (testes usam um falso); em produção, as chaves do keyring."""
    return getattr(request.app.state, "cliente_pluggy", None) or criar_cliente()


@router.post("/openfinance/chaves", response_class=HTMLResponse)
async def guardar_chaves_pluggy(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        salvar_chaves(_campo(form, "client_id"), _campo(form, "client_secret"))
    except ValueError as exc:
        return _tela(request, s, hoje, "openfinance", erro=str(exc), status_code=400)
    return _voltar("openfinance")


@router.post("/openfinance/chaves/remover", response_class=HTMLResponse)
def apagar_chaves_pluggy(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    try:
        remover_chaves()
    except ValueError as exc:
        return _tela(request, s, hoje, "openfinance", erro=str(exc), status_code=400)
    return _voltar("openfinance")


@router.post("/openfinance/itens", response_class=HTMLResponse)
async def novo_item_pluggy(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        adicionar_item(s, _campo(form, "item_id"))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, "openfinance", erro=str(exc), status_code=400)
    return _voltar("openfinance")


@router.post("/openfinance/itens/remover", response_class=HTMLResponse)
async def tirar_item_pluggy(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    remover_item(s, _campo(form, "item_id"))
    s.commit()
    return _voltar("openfinance")


@router.post("/openfinance/testar", response_class=HTMLResponse)
def testar_pluggy(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    cliente = _cliente_pluggy(request)
    itens = itens_configurados(s)
    if cliente is None:
        return _tela(request, s, hoje, "openfinance", erro="Guarde o Client ID e o Client Secret antes de testar.",
                     status_code=400)
    if not itens:
        return _tela(request, s, hoje, "openfinance", erro="Adicione ao menos um ID de item para testar.",
                     status_code=400)
    try:
        status = consultar_itens(s, cliente, itens, hoje)
        s.commit()
    except OpenFinanceErro as exc:
        return _tela(request, s, hoje, "openfinance", erro=str(exc), status_code=502)
    ok = sum(1 for st in status if not st.erro)
    return _tela(request, s, hoje, "openfinance",
                 mensagem=f"Conexão funcionando: {ok} de {len(status)} item(ns) respondeu.")
