from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.core.periodo import INTERVALOS_MESES, competencia_de, validar_competencia, validar_intervalo
from app.models import Categoria, Conta, Recorrencia
from app.services.recorrencia import editar_recorrencia, encerrar_recorrencia, gerar_pendentes
from app.web.deps import get_hoje, get_session
from app.web.templating import render

router = APIRouter(prefix="/lancamentos/recorrencias")


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _int_ou_none(texto: str) -> int | None:
    return int(texto) if texto.isdigit() else None


def _tela_recorrencias(request: Request, s: Session, hoje: date, erro: str | None = None, status_code: int = 200):
    categorias = list(s.scalars(select(Categoria).order_by(Categoria.tipo, Categoria.nome)))
    return render(request, "lancamentos/recorrencias.html", {
        "hoje": hoje, "erro": erro, "competencia_atual": competencia_de(hoje),
        "intervalos": INTERVALOS_MESES,
        "recorrencias": list(s.scalars(select(Recorrencia).order_by(Recorrencia.ativa.desc(), Recorrencia.descricao))),
        "contas": list(s.scalars(select(Conta).where(Conta.ativa).order_by(Conta.nome))),
        "contas_por_id": {c.id: c for c in s.scalars(select(Conta))},
        "categorias_despesa": [c for c in categorias if c.tipo == "despesa"],
        "categorias_receita": [c for c in categorias if c.tipo == "receita"],
    }, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def recorrencias(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    return _tela_recorrencias(request, s, hoje)


def _dia(texto: str) -> int:
    if not texto.isdigit() or not 1 <= int(texto) <= 31:
        raise ValueError("o dia deve estar entre 1 e 31")
    return int(texto)


def _conta(s: Session, texto: str, rotulo: str) -> Conta:
    conta = s.get(Conta, int(texto)) if texto.isdigit() else None
    if conta is None:
        raise ValueError(f"escolha a {rotulo}")
    return conta


@router.post("", response_class=HTMLResponse)
async def nova_recorrencia(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        tipo = _campo(form, "tipo")
        if tipo not in ("receita", "despesa", "transferencia"):
            raise ValueError("tipo inválido")
        descricao = _campo(form, "descricao")
        if not descricao:
            raise ValueError("informe a descrição")
        conta = _conta(s, _campo(form, "conta_id"), "conta")
        destino = _conta(s, _campo(form, "conta_destino_id"), "conta de destino") if tipo == "transferencia" else None
        if conta.tipo == "cartao" and tipo != "despesa":
            raise ValueError("no cartão só é possível recorrência de despesa")
        valor = parse_reais(_campo(form, "valor"))
        if valor <= 0:
            raise ValueError("o valor deve ser maior que zero")
        s.add(Recorrencia(
            descricao=descricao, tipo=tipo, conta_id=conta.id, conta_destino_id=destino.id if destino else None,
            categoria_id=_int_ou_none(_campo(form, "categoria_id")) if tipo != "transferencia" else None,
            valor=valor, dia=_dia(_campo(form, "dia")),
            inicio=validar_competencia(_campo(form, "inicio") or competencia_de(hoje)),
            valor_variavel=bool(form.get("valor_variavel")),
            tolerancia_pct=int(_campo(form, "tolerancia_pct") or 0),
            intervalo_meses=validar_intervalo(_intervalo(form, 1)),
            fim=validar_competencia(_campo(form, "fim")) if _campo(form, "fim") else None,
        ))
        s.flush()
        gerar_pendentes(s, hoje)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_recorrencias(request, s, hoje, erro=str(exc), status_code=400)
    return RedirectResponse("/lancamentos/recorrencias", status_code=303)


def _intervalo(form, padrao: int) -> int:
    texto = _campo(form, "intervalo_meses")
    return int(texto) if texto.isdigit() else padrao


def _obter_recorrencia(s: Session, rec_id: int) -> Recorrencia:
    rec = s.get(Recorrencia, rec_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="recorrência não encontrada")
    return rec


@router.post("/{rec_id}", response_class=HTMLResponse)
async def alterar_recorrencia(request: Request, rec_id: int, s: Session = Depends(get_session),
                              hoje: date = Depends(get_hoje)):
    rec = _obter_recorrencia(s, rec_id)
    form = await request.form()
    try:
        valor = parse_reais(_campo(form, "valor"))
        if valor <= 0:
            raise ValueError("o valor deve ser maior que zero")
        campos = {
            "descricao": _campo(form, "descricao") or rec.descricao,
            "conta_id": _conta(s, _campo(form, "conta_id"), "conta").id,
            "valor": valor,
            "valor_variavel": bool(form.get("valor_variavel")),
        }
        if rec.frequencia != "semanal":  # a regra semanal (dias, intervalo) só se muda ao recriar
            campos["dia"] = _dia(_campo(form, "dia"))
            campos["intervalo_meses"] = validar_intervalo(_intervalo(form, rec.intervalo_meses))
        if rec.tipo != "transferencia":
            campos["categoria_id"] = _int_ou_none(_campo(form, "categoria_id"))
        editar_recorrencia(s, rec, a_partir=validar_competencia(_campo(form, "a_partir")), hoje=hoje, **campos)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_recorrencias(request, s, hoje, erro=str(exc), status_code=400)
    return RedirectResponse("/lancamentos/recorrencias", status_code=303)


@router.post("/{rec_id}/encerrar", response_class=HTMLResponse)
async def encerrar(request: Request, rec_id: int, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    rec = _obter_recorrencia(s, rec_id)
    form = await request.form()
    try:
        encerrar_recorrencia(s, rec, a_partir=validar_competencia(_campo(form, "a_partir")))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_recorrencias(request, s, hoje, erro=str(exc), status_code=400)
    return RedirectResponse("/lancamentos/recorrencias", status_code=303)
