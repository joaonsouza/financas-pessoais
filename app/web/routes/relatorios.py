from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/relatorios")


@router.get("", response_class=RedirectResponse)
def redirecionar_relatorios(request: Request):
    query = str(request.query_params)
    destino = "/desempenho"
    if query:
        destino += f"?{query}"
    return RedirectResponse(destino, status_code=301)
