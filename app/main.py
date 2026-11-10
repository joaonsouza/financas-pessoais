import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (registra as tabelas)
from app.core import relogio
from app.core.demo import modo_demo
from app.db import Base, caminho_db_padrao, criar_engine, migrar
from app.models import Conta, Provisao, get_config, set_config
from app.services.backup import PASTA_BACKUP_PADRAO, copia_ao_lado, fazer_backup, precisa_backup
from app.services.categorias_padrao import migrar_inflacao_categorias
from app.services.invest_carteira import (
    garantir_carteira_padrao, migrar_alocacao, migrar_ativos_desejados, migrar_simbolos_cripto,
)
from app.services.metas import (
    migrar_para_caixinhas, migrar_provisoes_para_metas, recalcular_metas_automaticas, vincular_aportes_recorrentes,
)
from app.services.recorrencia import gerar_pendentes, reparar_vinculos
from app.web.deps import Provedores
from app.web.routes import ROTAS
from app.web.routes.investimentos import atualizar_mercado

log = logging.getLogger(__name__)

PASTA_STATIC = Path(__file__).parent / "web" / "static"
ROTAS_LIVRES = ("/setup", "/static", "/health", "/favicon.ico")
HOSTS_LOCAIS = ("127.0.0.1", "localhost")
INTERVALO_VERIFICACAO_BACKUP = 3600  # segundos
INTERVALO_COTACOES = timedelta(minutes=15)  # igual à validade do cache de cotações
INTERVALO_VERIFICACAO_COTACOES = 60  # segundos; o relógio de parede decide, então acordar do sleep já atualiza
PREFIXO_COPIA_ANTES_DAS_METAS = "financas.antes-metas-unificadas"  # + -AAAAMMDD-HHMMSS.db: não colide com cópia manual


def _db_path(url_banco: str | None) -> Path | None:
    if url_banco is None:
        return caminho_db_padrao()
    if url_banco.startswith("sqlite:///"):
        return Path(url_banco.removeprefix("sqlite:///"))
    return None


def executar_backup_se_preciso(fabrica, db_path: Path) -> None:
    if modo_demo():  # o banco fictício não vai para a pasta de backup real
        return
    with fabrica() as s:
        destino = Path(get_config(s, "pasta_backup", str(PASTA_BACKUP_PADRAO)))
        agora = relogio.agora().replace(tzinfo=None)
        try:
            if precisa_backup(destino, agora):
                fazer_backup(db_path, destino, agora)
            set_config(s, "erro_backup", None)
        except OSError as exc:
            set_config(s, "erro_backup", f"{agora:%d/%m/%Y %H:%M} — {exc}")
        s.commit()


def _migrar_provisoes(fabrica, db_path: Path | None) -> None:
    """Provisões viram metas; antes, uma cópia do banco ao lado dele (com banco em memória, não há o que copiar).
    Falhando, o app sobe mesmo assim, as provisões ficam como estavam e o Dashboard avisa."""
    with fabrica() as s:
        try:
            if db_path and s.scalar(select(func.count(Provisao.id))):
                copia_ao_lado(db_path, f"{PREFIXO_COPIA_ANTES_DAS_METAS}-{relogio.agora():%Y%m%d-%H%M%S}.db")
            migrar_provisoes_para_metas(s, relogio.hoje())
            set_config(s, "erro_migracao_metas", None)
            s.commit()
        except Exception as exc:
            s.rollback()
            log.exception("migração das provisões para metas falhou")
            try:  # banco travado ou corrompido: sem o aviso, mas o app sobe
                set_config(s, "erro_migracao_metas", f"{relogio.agora():%d/%m/%Y %H:%M} — {exc}")
                s.commit()
            except Exception:
                s.rollback()
                log.exception("não deu para gravar o erro da migração das provisões")


def _rotina_inicial(fabrica, db_path: Path | None) -> None:
    _migrar_provisoes(fabrica, db_path)
    with fabrica() as s:
        reparar_vinculos(s)
        gerar_pendentes(s, relogio.hoje())
        vincular_aportes_recorrentes(s)
        migrar_para_caixinhas(s, relogio.hoje())
        migrar_alocacao(s)
        migrar_simbolos_cripto(s)
        garantir_carteira_padrao(s)
        migrar_ativos_desejados(s)
        migrar_inflacao_categorias(s)
        recalcular_metas_automaticas(s, relogio.hoje())
        s.commit()
    if db_path:
        executar_backup_se_preciso(fabrica, db_path)


async def _loop_backup(fabrica, db_path: Path) -> None:
    while True:
        await asyncio.sleep(INTERVALO_VERIFICACAO_BACKUP)
        await asyncio.to_thread(executar_backup_se_preciso, fabrica, db_path)


def atualizar_mercado_em_segundo_plano(fabrica, agora: datetime, hoje: date, prov: Provedores | None = None) -> None:
    """Cotações vencidas, CDI/IPCA e Tesouro, para a tela de investimentos abrir já atualizada. Nunca levanta."""
    try:
        with fabrica() as s:
            falhas = atualizar_mercado(s, prov or Provedores(), agora, hoje)
        if falhas:
            log.warning("atualização de mercado em segundo plano falhou: %s", ", ".join(falhas))
    except Exception:  # banco travado e afins: a próxima volta tenta de novo
        log.exception("atualização de mercado em segundo plano falhou")


async def _loop_cotacoes(fabrica) -> None:
    ultima: datetime | None = None
    while True:
        agora = relogio.agora().replace(tzinfo=None)
        if ultima is None or agora - ultima >= INTERVALO_COTACOES:
            ultima = agora
            await asyncio.to_thread(atualizar_mercado_em_segundo_plano, fabrica, agora, agora.date())
        await asyncio.sleep(INTERVALO_VERIFICACAO_COTACOES)


def _pedido_bloqueado(request: Request) -> str | None:
    """O app não tem login e escuta só em 127.0.0.1; isto barra os dois jeitos de um site qualquer chegar nele
    pelo navegador do usuário:

    - DNS rebinding: o site troca o próprio DNS para 127.0.0.1 e passa a ler as páginas. O Host continua sendo o
      nome do site, então só aceitamos 127.0.0.1/localhost.
    - CSRF: um formulário ou fetch de outra página manda POST para cá. O navegador avisa a origem em
      Sec-Fetch-Site e Origin; métodos que alteram dados só passam vindos desta mesma origem.

    Pedido sem esses cabeçalhos (curl, testes, navegador antigo) passa: o risco é página web, e navegador atual
    sempre manda pelo menos o Origin num POST.
    """
    host = request.headers.get("host", "")
    if host.rsplit(":", 1)[0] not in HOSTS_LOCAIS:
        return "host não permitido"
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return "pedido de outro site"
    origem = request.headers.get("origin")
    if origem is not None and origem != f"{request.url.scheme}://{host}":
        return "pedido de outro site"
    return None


def criar_app(url_banco: str | None = None, *, rotinas: bool = True) -> FastAPI:
    engine = criar_engine(url_banco)
    Base.metadata.create_all(engine)
    migrar(engine)
    fabrica = sessionmaker(engine, expire_on_commit=False)
    db_path = _db_path(url_banco)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tarefas = []
        if rotinas:
            _rotina_inicial(fabrica, db_path)
            if db_path:  # banco de verdade: banco em memória (testes) não vai à rede
                tarefas.append(asyncio.create_task(_loop_backup(fabrica, db_path)))
                tarefas.append(asyncio.create_task(_loop_cotacoes(fabrica)))
        yield
        for tarefa in tarefas:
            tarefa.cancel()

    aplicacao = FastAPI(title="Finanças", lifespan=lifespan)
    aplicacao.state.engine = engine
    aplicacao.state.fabrica_sessao = fabrica
    aplicacao.state.db_path = db_path
    aplicacao.mount("/static", StaticFiles(directory=PASTA_STATIC), name="static")

    @aplicacao.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/favicon.svg", status_code=301)

    @aplicacao.middleware("http")
    async def exigir_setup(request: Request, call_next):
        if not request.url.path.startswith(ROTAS_LIVRES):
            with fabrica() as s:
                if not s.scalar(select(func.count(Conta.id))):
                    return RedirectResponse("/setup", status_code=303)
        return await call_next(request)

    # Registrado depois de exigir_setup, então roda antes dele.
    @aplicacao.middleware("http")
    async def so_pedidos_locais(request: Request, call_next):
        motivo = _pedido_bloqueado(request)
        if motivo:
            log.warning("pedido bloqueado (%s): %s %s", motivo, request.method, request.url.path)
            return PlainTextResponse(motivo, status_code=400 if motivo == "host não permitido" else 403)
        return await call_next(request)

    @aplicacao.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    for router in ROTAS:
        aplicacao.include_router(router)
    return aplicacao
