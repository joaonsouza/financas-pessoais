from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.services.resumo import resumo_do_mes, texto_whatsapp
from app.web.deps import get_agora, get_hoje, get_session, mes_selecionado
from app.web.templating import render

router = APIRouter(prefix="/resumo")


@router.get("", response_class=HTMLResponse)
def resumo(request: Request, mes: str = Depends(mes_selecionado), s: Session = Depends(get_session),
           hoje: date = Depends(get_hoje), agora: datetime = Depends(get_agora)):
    r = resumo_do_mes(s, mes, hoje)
    return render(request, "resumo/index.html", {"mes": mes, "r": r, "texto": texto_whatsapp(r), "agora": agora})
