"""Criação rápida de categoria pelo seletor (o resto da gestão fica em Configurações)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.models import Categoria
from app.services.categorias_seletor import criar_categoria_rapida
from app.web.deps import get_session

router = APIRouter(prefix="/categorias")


@router.post("/rapida")
async def criar_rapida(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    pai = str(form.get("pai_id") or "").strip()
    try:
        nova = criar_categoria_rapida(s, str(form.get("nome") or ""), str(form.get("tipo") or ""),
                                      int(pai) if pai.isdigit() else None)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return JSONResponse({"erro": str(exc)}, status_code=400)
    pai_nome = s.get(Categoria, nova.pai_id).nome if nova.pai_id else None
    return {"id": nova.id, "nome": nova.nome, "cor": nova.cor, "pai_id": nova.pai_id, "tipo": nova.tipo,
            "rotulo": f"{pai_nome} › {nova.nome}" if pai_nome else nova.nome}
