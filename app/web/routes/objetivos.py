"""Endereços antigos de Objetivos: a lista mora no Planejamento e o detalhe em /planejamento/metas."""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/objetivos")


@router.get("")
def lista(request: Request):
    qs = request.url.query
    return RedirectResponse(f"/planejamento{'?' + qs if qs else ''}#metas", status_code=301)


@router.get("/{meta_id}")
def detalhe(meta_id: int):
    return RedirectResponse(f"/planejamento/metas/{meta_id}", status_code=301)
