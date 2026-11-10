from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, dia_no_mes, primeiro_dia, somar_meses, ultimo_dia
from app.models import Caixinha, Conta, Lancamento, Recorrencia
from app.services.lancamentos import criar_lancamento, criar_transferencia

HORIZONTE_PADRAO = 36  # meses à frente com lançamentos pendentes já criados
HORIZONTE_SEMANAL = 12  # semanal gera mais linhas por mês: janela menor

CAMPOS_EDITAVEIS = {
    "descricao", "conta_id", "conta_destino_id", "categoria_id", "valor", "valor_variavel",
    "tolerancia_pct", "dia", "fim", "eh_assinatura", "intervalo_meses", "frequencia", "intervalo_semanas",
    "dias_semana", "fim_data", "max_ocorrencias",
}


def valor_estimado(s: Session, rec: Recorrencia) -> int:
    if not rec.valor_variavel:
        return rec.valor
    ultimo = s.scalar(
        select(Lancamento)
        .where(
            Lancamento.recorrencia_id == rec.id,
            Lancamento.conta_id == rec.conta_id,
            Lancamento.status == "efetivado",
        )
        .order_by(Lancamento.data.desc())
        .limit(1)
    )
    return abs(ultimo.valor) if ultimo else rec.valor


def _ja_existe(s: Session, rec: Recorrencia, competencia: str) -> bool:
    consulta = select(Lancamento.id).where(
        Lancamento.recorrencia_id == rec.id, Lancamento.competencia_recorrencia == competencia
    ).limit(1)
    return s.scalar(consulta) is not None


def _materializar(s: Session, rec: Recorrencia, chave: str, data: date) -> None:
    conta = s.get(Conta, rec.conta_id)
    valor = valor_estimado(s, rec)
    if rec.tipo == "transferencia":
        pernas = criar_transferencia(s, origem=conta, destino=s.get(Conta, rec.conta_destino_id),
                                     data=data, valor_abs=valor, descricao=rec.descricao)
    else:
        pernas = (criar_lancamento(s, conta=conta, data=data, descricao=rec.descricao, valor_abs=valor,
                                   tipo=rec.tipo, categoria_id=rec.categoria_id),)
    for lanc in pernas:
        lanc.recorrencia_id = rec.id
        lanc.competencia_recorrencia = chave
        lanc.origem = "recorrencia"
        # aporte "como despesa" não entra na meta: é despesa do mês (orçamento, 50/30/20); o guardado vem da caixinha
        lanc.meta_id = None if rec.aporte_como_despesa else (rec.origem_meta_id or rec.meta_id)
        if rec.caixinha_id and lanc.conta_id == s.get(Caixinha, rec.caixinha_id).conta_id:
            lanc.caixinha_id = rec.caixinha_id  # a perna que cai na conta da caixinha entra nela
    s.flush()


def _datas_da_regra(rec: Recorrencia):
    """Datas agendadas pela regra, em ordem, sem fim. A chave é o que identifica a ocorrência: a competência
    (AAAA-MM) na mensal e a data (AAAA-MM-DD) na semanal."""
    if rec.frequencia == "semanal":
        inicio = rec.data_inicio or dia_no_mes(rec.inicio, rec.dia)
        dias = sorted({int(d) for d in (rec.dias_semana or str(inicio.weekday())).split(",") if d != ""})
        segunda = inicio - timedelta(days=inicio.weekday())
        semana = 0
        while True:
            base = segunda + timedelta(weeks=semana * rec.intervalo_semanas)
            for dia in dias:
                data = base + timedelta(days=dia)
                if data >= inicio:
                    yield data.isoformat(), data
            semana += 1
    else:
        k = 0
        while True:
            competencia = somar_meses(rec.inicio, k * rec.intervalo_meses)
            yield competencia, dia_no_mes(competencia, rec.dia)
            k += 1


def ocorrencias(rec: Recorrencia, ate: date):
    """(número, chave, data) de cada ocorrência da regra até `ate`, respeitando fim, fim_data e max_ocorrencias."""
    n = 0
    for chave, data in _datas_da_regra(rec):
        if data > ate:
            return
        if (rec.fim and competencia_de(data) > rec.fim) or (rec.fim_data and data > rec.fim_data):
            return
        n += 1
        if rec.max_ocorrencias and n > rec.max_ocorrencias:
            return
        yield n, chave, data


def gerar_pendentes(s: Session, hoje: date, horizonte: int | None = None) -> int:
    inicio_janela = primeiro_dia(competencia_de(hoje))
    criadas = 0
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.ativa)).all():
        meses = horizonte if horizonte is not None else (
            HORIZONTE_SEMANAL if rec.frequencia == "semanal" else HORIZONTE_PADRAO)
        ate = ultimo_dia(somar_meses(competencia_de(hoje), meses))
        for _, chave, data in ocorrencias(rec, ate):
            if data < inicio_janela or _ja_existe(s, rec, chave):
                continue
            _materializar(s, rec, chave, data)
            criadas += 1
    return criadas


def encerrar_antes_de(rec: Recorrencia, chave: str) -> None:
    """A recorrência termina na ocorrência anterior à de `chave`; sem nenhuma anterior, fica inativa."""
    if rec.frequencia == "semanal":
        rec.fim_data = date.fromisoformat(chave) - timedelta(days=1)
        if rec.fim_data < (rec.data_inicio or dia_no_mes(rec.inicio, rec.dia)):
            rec.ativa = False
    else:
        rec.fim = somar_meses(chave, -1)
        if rec.fim < rec.inicio:
            rec.ativa = False


def _apagar_pendentes_desde(s: Session, rec: Recorrencia, a_partir: str) -> None:
    pendentes = list(s.scalars(select(Lancamento).where(
        Lancamento.recorrencia_id == rec.id,
        Lancamento.status == "pendente",
        Lancamento.competencia_recorrencia >= a_partir,
    )))
    for lanc in pendentes:
        lanc.transferencia_par_id = None
    s.flush()
    for lanc in pendentes:
        s.delete(lanc)
    s.flush()


def editar_recorrencia(s: Session, rec: Recorrencia, *, a_partir: str, hoje: date,
                       horizonte: int | None = None, **campos) -> None:
    invalidos = set(campos) - CAMPOS_EDITAVEIS
    if invalidos:
        raise ValueError(f"campos não editáveis: {sorted(invalidos)}")
    for campo, valor in campos.items():
        setattr(rec, campo, valor)
    _apagar_pendentes_desde(s, rec, a_partir)
    gerar_pendentes(s, hoje, horizonte)


def encerrar_recorrencia(s: Session, rec: Recorrencia, *, a_partir: str) -> None:
    rec.fim = somar_meses(a_partir, -1)
    if rec.fim < rec.inicio:
        rec.ativa = False
    _apagar_pendentes_desde(s, rec, a_partir)


def reparar_vinculos(s: Session) -> int:
    """Recorrências criadas no formulário antes de guardarem o objetivo/caixinha: só a primeira ocorrência tinha
    o vínculo. Passa o vínculo para a recorrência e para as ocorrências que ficaram sem ele. Só age enquanto a
    recorrência ainda não guarda o vínculo (depois, quem tirou de uma ocorrência de propósito não é desfeito)."""
    reparados = 0
    for rec in s.scalars(select(Recorrencia)):
        lancs = list(s.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id,
                                                        Lancamento.status != "cancelado")))
        if rec.origem_meta_id is None and rec.meta_id is None:
            meta_id = next((l.meta_id for l in lancs if l.meta_id), None)
            if meta_id:
                rec.meta_id = meta_id
                for l in lancs:
                    if l.meta_id is None:
                        l.meta_id = meta_id
                        reparados += 1
        if rec.caixinha_id is None:
            caixinha_id = next((l.caixinha_id for l in lancs if l.caixinha_id), None)
            caixinha = s.get(Caixinha, caixinha_id) if caixinha_id else None
            if caixinha:
                rec.caixinha_id = caixinha.id
                for l in lancs:
                    if l.caixinha_id is None and l.conta_id == caixinha.conta_id:
                        l.caixinha_id = caixinha.id
                        reparados += 1
    s.flush()
    return reparados


_NOMES_DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def resumo_regra(rec: Recorrencia) -> str:
    """A regra em português: "toda semana (quinta)", "a cada 3 meses", "todo ano · até 31/12/2030"..."""
    if rec.frequencia == "semanal":
        dias = sorted({int(d) for d in (rec.dias_semana or "").split(",") if d != ""})
        quando = ", ".join(_NOMES_DIAS[d] for d in dias)
        base = ("toda semana" if rec.intervalo_semanas == 1 else f"a cada {rec.intervalo_semanas} semanas")
        base += f" ({quando})" if quando else ""
    elif rec.intervalo_meses % 12 == 0:
        anos = rec.intervalo_meses // 12
        base = "todo ano" if anos == 1 else f"a cada {anos} anos"
    else:
        base = "todo mês" if rec.intervalo_meses == 1 else f"a cada {rec.intervalo_meses} meses"
    if rec.fim_data:
        base += f" · até {rec.fim_data:%d/%m/%Y}"
    if rec.max_ocorrencias:
        base += f" · {rec.max_ocorrencias} vezes"
    return base


def custo_mensal_recorrencias(s: Session) -> tuple[int, int]:
    """(quantidade, centavos por mês) das recorrências de despesa ativas; o mês tem 52/12 semanas."""
    ativas = list(s.scalars(select(Recorrencia).where(Recorrencia.ativa, Recorrencia.tipo == "despesa")))
    total = 0
    for r in ativas:
        if r.frequencia == "semanal":
            dias = len(r.dias_semana.split(",")) if r.dias_semana else 1
            total += r.valor * dias * 52 // (12 * r.intervalo_semanas)
        else:
            total += r.valor // r.intervalo_meses
    return len(ativas), total
