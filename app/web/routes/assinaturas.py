from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.models import Assinatura, Conta
from app.services.assinaturas import (
    assinatura_da_sugestao, cancelar_assinatura, criar_assinatura, editar_assinatura, ignorar_sugestao,
    painel_assinaturas, reativar_assinatura,
)
from app.services.faturas import cartoes_ativos
from app.web.deps import get_hoje, get_session
from app.web.templating import render

router = APIRouter(prefix="/lancamentos/assinaturas")
DESTINO = "/lancamentos/assinaturas"


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _dados(form) -> dict:
    conta = _campo(form, "conta_id")
    if not conta.isdigit():
        raise ValueError("escolha o cartão")
    intervalo, dia = _campo(form, "intervalo_meses"), _campo(form, "dia")
    return {
        "nome": _campo(form, "nome"), "valor": parse_reais(_campo(form, "valor") or "0"), "conta_id": int(conta),
        "intervalo_meses": int(intervalo) if intervalo.isdigit() else 1,
        "dia": int(dia) if dia.isdigit() else None, "padrao": _campo(form, "padrao") or None,
    }


def _tela(request: Request, s: Session, hoje: date, erro: str | None = None, status_code: int = 200):
    return render(request, "lancamentos/assinaturas.html", {
        "painel": painel_assinaturas(s, hoje), "hoje": hoje, "erro": erro,
        "cartoes": cartoes_ativos(s),
        "contas_por_id": {c.id: c for c in s.scalars(select(Conta))},
    }, status_code=status_code)


def _obter(s: Session, assinatura_id: int) -> Assinatura:
    a = s.get(Assinatura, assinatura_id)
    if a is None:
        raise HTTPException(status_code=404, detail="assinatura não encontrada")
    return a


def _depois(s: Session) -> RedirectResponse:
    s.commit()
    return RedirectResponse(DESTINO, status_code=303)


@router.get("", response_class=HTMLResponse)
def controle(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    return _tela(request, s, hoje)


@router.post("")
async def criar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        criar_assinatura(s, hoje=hoje, observacao=_campo(form, "observacao") or None, **_dados(form))
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, erro=str(exc), status_code=400)
    return _depois(s)


@router.post("/sugestoes/cadastrar")
async def cadastrar_sugestao(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    conta = _campo(form, "conta_id")
    sug = next((x for x in painel_assinaturas(s, hoje).sugestoes
                if x.descricao == _campo(form, "descricao") and str(x.conta_id) == conta), None)
    if sug is not None:
        try:
            assinatura_da_sugestao(s, sug, hoje)
        except ValueError as exc:
            s.rollback()
            return _tela(request, s, hoje, erro=str(exc), status_code=400)
    return _depois(s)


@router.post("/sugestoes/ignorar")
async def ignorar(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    if _campo(form, "conta_id").isdigit():
        ignorar_sugestao(s, _campo(form, "descricao"), int(_campo(form, "conta_id")))
    return _depois(s)


@router.post("/{assinatura_id:int}")
async def editar(request: Request, assinatura_id: int, s: Session = Depends(get_session),
                 hoje: date = Depends(get_hoje)):
    a = _obter(s, assinatura_id)
    form = await request.form()
    try:
        dados = _dados(form)
        dados["observacao"] = _campo(form, "observacao") or None
        if dados["padrao"] is None:
            dados.pop("padrao")
        editar_assinatura(s, a, **dados)
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, hoje, erro=str(exc), status_code=400)
    return _depois(s)


@router.post("/{assinatura_id:int}/cancelar")
async def cancelar(request: Request, assinatura_id: int, s: Session = Depends(get_session),
                   hoje: date = Depends(get_hoje)):
    a = _obter(s, assinatura_id)
    form = await request.form()
    try:
        em = date.fromisoformat(_campo(form, "em")) if _campo(form, "em") else hoje
    except ValueError:
        em = hoje
    cancelar_assinatura(a, em)
    return _depois(s)


@router.post("/{assinatura_id:int}/reativar")
def reativar(assinatura_id: int, s: Session = Depends(get_session)):
    reativar_assinatura(_obter(s, assinatura_id))
    return _depois(s)
