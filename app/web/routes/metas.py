from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.core.periodo import competencia_de, validar_competencia
from app.models import HORIZONTES, TIPOS_META, Carteira, Categoria, Conta, Meta
from app.services.metas import (
    CHECKLIST_SAQUE,
    DIAS_EXPIRA_EM_BREVE,
    agendado_da_meta,
    aplicar_pague_primeiro,
    aporte_mensal,
    economia_do_mes,
    excluir_meta,
    gasto_essencial,
    guardar,
    historico_meta,
    progresso_meta,
    reserva,
    retirar,
    sincronizar_caixinha_meta,
)
from app.services.reserva_plano import plano_reserva, projecao_reserva
from app.web.deps import get_hoje, get_session, mes_selecionado
from app.web.templating import render

router = APIRouter(prefix="/planejamento/metas")

ROTULOS_HORIZONTE = {"mes": "Neste mês", "ano": "Neste ano", "sonho": "Sonhos"}
ROTULOS_TIPO = {"economia_mensal": "Economia mensal", "objetivo": "Objetivo", "reserva_emergencia": "Reserva de emergência"}
ICONES = ["target", "piggy-bank", "plane", "car", "house", "graduation-cap", "heart", "gift", "laptop",
          "baby", "shield-check", "trending-up"]
CORES = ["#10b981", "#3b82f6", "#8b5cf6", "#ec4899", "#f97316", "#eab308", "#14b8a6", "#ef4444"]
ORDENS = {"prazo": "Prazo", "pct": "% concluído", "alvo": "Valor alvo", "nome": "Nome"}


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _int_ou_none(texto) -> int | None:
    texto = str(texto or "").strip()
    return int(texto) if texto.isdigit() else None


def _dinheiro_ou_none(texto: str) -> int | None:
    return parse_reais(texto) if texto.strip() else None


def _ordenar(itens: list, ordem: str) -> list:
    chaves = {
        "prazo": lambda x: (x[0].prazo is None, x[0].prazo or date.max, x[0].nome.casefold()),
        "pct": lambda x: (-x[1].pct, x[0].nome.casefold()),
        "alvo": lambda x: (-x[1].alvo, x[0].nome.casefold()),
        "nome": lambda x: x[0].nome.casefold(),
    }
    return sorted(itens, key=chaves[ordem])


def _link(aba: str, ordem: str, horizonte: str, mes: str) -> str:
    params = {k: v for k, v in (("mes", mes), ("aba", aba), ("ordem", ordem), ("horizonte", horizonte))
              if v and (k, v) not in (("aba", "andamento"), ("ordem", "prazo"))}
    return "/planejamento" + ("?" + urlencode(params) if params else "") + "#metas"


def expira_em_breve(itens: list, hoje: date):
    """Objetivo em andamento de prazo mais próximo, se vence (ou venceu) em até 90 dias."""
    candidatos = [(m, p) for m, p in itens if m.ativa and m.prazo and not p.concluido
                  and m.prazo <= hoje + timedelta(days=DIAS_EXPIRA_EM_BREVE)]
    return min(candidatos, key=lambda x: x[0].prazo, default=None)


def _contexto_form(s: Session, hoje: date) -> dict:
    return {
        "gasto_essencial": gasto_essencial(s, hoje)[0],  # o formulário sugere o alvo da reserva com ele
        "contas": list(s.scalars(select(Conta).where(Conta.ativa, Conta.tipo != "cartao").order_by(Conta.nome))),
        # a reserva escolhe a carteira em que aparece nos Investimentos (além da consolidada)
        "carteiras": list(s.scalars(select(Carteira).order_by(Carteira.padrao.desc(), Carteira.nome))),
        "tipos": ROTULOS_TIPO, "horizontes": ROTULOS_HORIZONTE, "icones": ICONES, "cores": CORES,
        "categorias_despesa": list(s.scalars(select(Categoria).where(
            Categoria.tipo == "despesa", Categoria.ativa).order_by(Categoria.nome))),
    }


def _preencher(meta: Meta, form) -> None:
    nome = _campo(form, "nome")
    if not nome:
        raise ValueError("informe o nome da meta")
    tipo = _campo(form, "tipo") or "objetivo"
    horizonte = _campo(form, "horizonte") or "ano"
    if tipo not in TIPOS_META or horizonte not in HORIZONTES:
        raise ValueError("tipo ou horizonte inválido")
    meta.nome, meta.tipo, meta.horizonte = nome, tipo, horizonte
    # na reserva, vazio = usar o alvo sugerido (gasto essencial × meses)
    meta.valor_alvo = _dinheiro_ou_none(_campo(form, "valor_alvo")) or 0
    prazo = _campo(form, "prazo")
    try:
        meta.prazo = date.fromisoformat(prazo) if prazo else None
    except ValueError as exc:
        raise ValueError("prazo inválido") from exc
    meta.conta_id = _int_ou_none(form.get("conta_id"))
    meta.valor_manual = _dinheiro_ou_none(_campo(form, "valor_manual"))
    meta.multiplicador_reserva = 6 if _campo(form, "multiplicador_reserva") == "6" else 3
    meta.carteira_id = _int_ou_none(form.get("carteira_id")) if tipo == "reserva_emergencia" else None
    meta.pagar_primeiro = bool(form.get("pagar_primeiro"))
    meta.conta_origem_id = _int_ou_none(form.get("conta_origem_id"))
    dia = _int_ou_none(form.get("dia_pagar"))
    if dia is not None and not 1 <= dia <= 31:
        raise ValueError("o dia de pagar deve estar entre 1 e 31")
    meta.dia_pagar = dia
    meta.valor_mensal_pagar = _dinheiro_ou_none(_campo(form, "valor_mensal_pagar"))
    # o que o formulário esconde não vale: sem "guardar todo mês" não há automático, anual nem "como despesa";
    # automático e anual dependem do prazo, que só o objetivo tem; "como despesa", da conta da meta
    objetivo = meta.pagar_primeiro and tipo == "objetivo"
    meta.aporte_automatico = objetivo and _campo(form, "aporte") == "automatico"
    meta.repete_anual = objetivo and bool(form.get("repete_anual"))
    meta.aporte_como_despesa = (meta.pagar_primeiro and meta.conta_id is not None
                                and bool(form.get("aporte_como_despesa")))
    meta.categoria_id = _int_ou_none(form.get("categoria_id")) if meta.aporte_como_despesa else None
    if _campo(form, "icone") in ICONES:
        meta.icone = _campo(form, "icone")
    if _campo(form, "cor") in CORES:
        meta.cor = _campo(form, "cor")
    meta.ativa = bool(form.get("ativa")) if meta.id is not None else True


async def _salvar(request: Request, s: Session, hoje: date, meta: Meta | None):
    form = await request.form()
    try:  # o erro volta para o Planejamento no mês em que o formulário estava
        mes = validar_competencia(_campo(form, "mes"))
    except ValueError:
        mes = competencia_de(hoje)
    no_detalhe = _campo(form, "voltar") == "detalhe" and meta is not None
    try:
        nova = meta is None
        meta = meta or Meta(nome="", tipo="objetivo")
        _preencher(meta, form)
        with s.no_autoflush:  # o id inválido não pode chegar ao banco antes da checagem
            if meta.carteira_id and s.get(Carteira, meta.carteira_id) is None:
                raise ValueError("carteira não encontrada")
        if meta.categoria_id and (cat := s.get(Categoria, meta.categoria_id)) is not None and cat.tipo != "despesa":
            raise ValueError("a categoria da meta deve ser de despesa")
        if nova:
            s.add(meta)
        s.flush()
        sincronizar_caixinha_meta(s, meta, hoje)
        aplicar_pague_primeiro(s, meta, hoje)
        s.commit()
    except ValueError as exc:
        s.rollback()
        if no_detalhe:
            return _tela_detalhe(request, s, hoje, _obter(s, meta.id), erro=str(exc), status_code=422, editando=True)
        from app.web.routes.planejamento import tela_planejamento
        return tela_planejamento(request, s, mes, hoje, erro=str(exc), status_code=422,
                                abrir_criar=_campo(form, "voltar") != "planejamento")
    # o formulário da tela de detalhe volta para ela; o do Planejamento (reserva), para ele; o da lista, para a lista
    if _campo(form, "voltar") == "planejamento":
        return RedirectResponse("/planejamento#reserva", status_code=303)
    return RedirectResponse(f"/planejamento/metas/{meta.id}" if no_detalhe else "/planejamento#metas", status_code=303)


@router.post("")
async def criar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    return await _salvar(request, s, hoje, None)


def _obter(s: Session, meta_id: int) -> Meta:
    meta = s.get(Meta, meta_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="meta não encontrada")
    return meta


def _tela_detalhe(request: Request, s: Session, hoje: date, meta: Meta, erro: str | None = None,
                  status_code: int = 200, editando: bool = False, aporte: int | None = None):
    ctx = _contexto_form(s, hoje)
    return render(request, "objetivos/detalhe.html", {
        **ctx, "hoje": hoje, "erro": erro, "m": meta, "editando": editando, "aporte_pre": aporte,
        "p": progresso_meta(s, meta, hoje), "agendado": agendado_da_meta(s, meta),
        "mensal": aporte_mensal(s, meta, hoje),
        "r": reserva(s, meta, hoje) if meta.tipo == "reserva_emergencia" else None,
        "projecao": projecao_reserva(s, meta, hoje) if meta.tipo == "reserva_emergencia" else None,
        "plano": plano_reserva(s, meta, hoje) if meta.tipo == "reserva_emergencia" else None,
        "economia": economia_do_mes(s, competencia_de(hoje)) if meta.tipo == "economia_mensal" else None,
        "historico": historico_meta(s, meta), "conta_meta": s.get(Conta, meta.conta_id) if meta.conta_id else None,
        # guardar/retirar: qualquer conta menos a da meta
        "contas_mov": [c for c in ctx["contas"] if c.id != meta.conta_id],
        "checklist": CHECKLIST_SAQUE,
    }, status_code=status_code)


@router.get("/{meta_id}", response_class=HTMLResponse)
def detalhe(request: Request, meta_id: int, editar: int = 0, aporte: int | None = None,
            s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    # ?editar=1 abre a edição; ?aporte=<centavos> vem com "guardar todo mês" preenchido (nada é gravado antes de salvar)
    return _tela_detalhe(request, s, hoje, _obter(s, meta_id), editando=bool(editar) or aporte is not None,
                         aporte=aporte if aporte and aporte > 0 else None)


@router.post("/{meta_id}")
async def atualizar(request: Request, meta_id: int, s: Session = Depends(get_session),
                    hoje: date = Depends(get_hoje)):
    return await _salvar(request, s, hoje, _obter(s, meta_id))


async def _movimentar(request: Request, s: Session, hoje: date, meta_id: int, acao: str):
    meta = _obter(s, meta_id)
    form = await request.form()
    try:
        conta = s.get(Conta, _int_ou_none(form.get("conta_id")) or 0)
        if conta is None:
            raise ValueError("escolha a conta")
        try:
            data = date.fromisoformat(_campo(form, "data"))
        except ValueError as exc:
            raise ValueError("data inválida") from exc
        valor = parse_reais(_campo(form, "valor") or "0")
        efetivado = data <= hoje  # data futura = agendado (pendente)
        if acao == "guardar":
            guardar(s, meta, valor=valor, data=data, conta=conta, hoje=hoje, efetivado=efetivado)
        else:
            retirar(s, meta, valor=valor, data=data, conta=conta, hoje=hoje, efetivado=efetivado,
                    checklist={str(v) for v in form.getlist("checklist")})
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_detalhe(request, s, hoje, meta, erro=str(exc), status_code=422)
    return RedirectResponse(f"/planejamento/metas/{meta.id}", status_code=303)


@router.post("/{meta_id}/guardar", response_class=HTMLResponse)
async def guardar_rota(request: Request, meta_id: int, s: Session = Depends(get_session),
                       hoje: date = Depends(get_hoje)):
    return await _movimentar(request, s, hoje, meta_id, "guardar")


@router.post("/{meta_id}/retirar", response_class=HTMLResponse)
async def retirar_rota(request: Request, meta_id: int, s: Session = Depends(get_session),
                       hoje: date = Depends(get_hoje)):
    return await _movimentar(request, s, hoje, meta_id, "retirar")


@router.post("/{meta_id}/excluir")
def excluir(meta_id: int, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    excluir_meta(s, _obter(s, meta_id), hoje)
    s.commit()
    return RedirectResponse("/planejamento#metas", status_code=303)
