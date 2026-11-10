"""Sincronização Open Finance: busca na Pluggy e monta uma proposta de importação por conta (nada é gravado)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.pluggy_ import identificador_da_conta, resultado_de_conta
from app.importers.base import ResultadoLeitura
from app.models import Conta, get_config, set_config
from app.services.importacao import Proposta, propor_importacao
from app.services.openfinance.pluggy import ClientePluggy, OpenFinanceErro

SOBREPOSICAO_DIAS = 7
JANELA_INICIAL_DIAS = 90
AVISO_CONSENTIMENTO_DIAS = 30
DESATUALIZADO_DIAS = 3

CHAVE_ITENS = "pluggy_itens"
CHAVE_ULTIMA_SYNC = "pluggy_ultima_sync"
CHAVE_STATUS = "pluggy_status_itens"


@dataclass
class StatusItem:
    item_id: str
    nome: str = ""
    status: str = ""
    atualizado_em: str | None = None     # ISO
    consentimento_ate: str | None = None  # ISO (data)
    alertas: list[str] = field(default_factory=list)
    erro: str | None = None

    @property
    def ok(self) -> bool:
        return not self.alertas and not self.erro


@dataclass
class PropostaPorConta:
    item_id: str
    account_id: str
    nome_pluggy: str
    tipo: str                                  # "cartao" | "corrente"
    desde: date
    ate: date
    resultado: ResultadoLeitura | None = None
    conta: Conta | None = None                 # vínculo (identificador_externo); None = o usuário escolhe
    proposta: Proposta | None = None
    candidatas: list[Conta] = field(default_factory=list)
    erro: str | None = None


def itens_configurados(s: Session) -> list[str]:
    return list(get_config(s, CHAVE_ITENS, []) or [])


def adicionar_item(s: Session, item_id: str) -> None:
    item_id = item_id.strip()
    if not item_id:
        raise ValueError("informe o ID do item")
    itens = itens_configurados(s)
    if item_id not in itens:
        set_config(s, CHAVE_ITENS, itens + [item_id])


def remover_item(s: Session, item_id: str) -> None:
    set_config(s, CHAVE_ITENS, [i for i in itens_configurados(s) if i != item_id])
    status = {k: v for k, v in (get_config(s, CHAVE_STATUS, {}) or {}).items() if k != item_id}
    set_config(s, CHAVE_STATUS, status)


# ---------- status dos itens ----------

_ALERTAS_STATUS = {
    "LOGIN_ERROR": "Erro de login: reconecte a instituição no Meu Pluggy.",
    "OUTDATED": "Conexão desatualizada: atualize o item no Meu Pluggy.",
    "WAITING_USER_INPUT": "A instituição aguarda uma ação sua no Meu Pluggy.",
}


def _data_iso(texto) -> date | None:
    try:
        return date.fromisoformat(str(texto)[:10]) if texto else None
    except ValueError:
        return None


def _status_do_item(item_id: str, dados: dict, hoje: date) -> StatusItem:
    st = StatusItem(item_id=item_id, nome=((dados.get("connector") or {}).get("name") or ""),
                    status=str(dados.get("status") or ""))
    atualizado, consentimento = _data_iso(dados.get("lastUpdatedAt")), _data_iso(dados.get("consentExpiresAt"))
    st.atualizado_em = atualizado.isoformat() if atualizado else None
    st.consentimento_ate = consentimento.isoformat() if consentimento else None
    if st.status in _ALERTAS_STATUS:
        st.alertas.append(_ALERTAS_STATUS[st.status])
    if (dados.get("executionStatus") or "").upper().endswith("ERROR") and st.status not in _ALERTAS_STATUS:
        st.alertas.append("A última atualização da conexão falhou.")
    if atualizado and (hoje - atualizado).days > DESATUALIZADO_DIAS:
        st.alertas.append(f"Dados do banco atualizados pela última vez há {(hoje - atualizado).days} dias.")
    if consentimento and (consentimento - hoje).days <= AVISO_CONSENTIMENTO_DIAS:
        st.alertas.append("Consentimento expirado." if consentimento < hoje
                          else f"Consentimento expira em {consentimento:%d/%m/%Y}.")
    return st


def consultar_itens(s: Session, cliente: ClientePluggy, item_ids: list[str], hoje: date) -> list[StatusItem]:
    """Consulta cada item e guarda o status para a tela. Erro de um item não derruba os outros."""
    cliente.autenticar()  # chaves inválidas ou sem rede: falha logo, uma vez só
    resultado: list[StatusItem] = []
    for item_id in item_ids:
        try:
            resultado.append(_status_do_item(item_id, cliente.item(item_id), hoje))
        except OpenFinanceErro as exc:
            resultado.append(StatusItem(item_id=item_id, erro=str(exc)))
    set_config(s, CHAVE_STATUS, {st.item_id: st.__dict__ for st in resultado})
    return resultado


def status_dos_itens(s: Session) -> dict[str, StatusItem]:
    return {k: StatusItem(**v) for k, v in (get_config(s, CHAVE_STATUS, {}) or {}).items()}


# ---------- última sincronização ----------

def ultima_sync_da_conta(s: Session, account_id: str) -> date | None:
    return _data_iso((get_config(s, CHAVE_ULTIMA_SYNC, {}) or {}).get(account_id))


def registrar_sincronizacao(s: Session, account_id: str, quando: date) -> None:
    mapa = dict(get_config(s, CHAVE_ULTIMA_SYNC, {}) or {})
    mapa[account_id] = quando.isoformat()
    set_config(s, CHAVE_ULTIMA_SYNC, mapa)


def ultima_sincronizacao(s: Session) -> date | None:
    datas = [d for d in (_data_iso(v) for v in (get_config(s, CHAVE_ULTIMA_SYNC, {}) or {}).values()) if d]
    return max(datas) if datas else None


# ---------- proposta ----------

def _conta_vinculada(s: Session, account_id: str) -> Conta | None:
    return s.scalar(select(Conta).where(Conta.identificador_externo == identificador_da_conta(account_id),
                                        Conta.ativa))


def _janela(s: Session, account_id: str, conta: Conta | None, hoje: date, desde: date | None) -> date:
    if desde is not None:
        return desde
    ultima = ultima_sync_da_conta(s, account_id)
    if ultima is not None:
        return ultima - timedelta(days=SOBREPOSICAO_DIAS)  # a deduplicação absorve a sobreposição
    return conta.data_saldo_inicial if conta is not None else hoje - timedelta(days=JANELA_INICIAL_DIAS)


def propor_para_conta(s: Session, pc: PropostaPorConta, conta: Conta, hoje: date) -> PropostaPorConta:
    """Gera a proposta de uma conta já escolhida (vínculo existente ou escolhido na tela)."""
    pc.conta = conta
    pc.proposta = propor_importacao(s, pc.resultado, conta, hoje,
                                    nome_arquivo=f"Open Finance · {pc.nome_pluggy}")
    return pc


def propor_sincronizacao(s: Session, cliente: ClientePluggy, item_ids: list[str], hoje: date,
                         desde: date | None = None) -> list[PropostaPorConta]:
    """Uma proposta por conta/cartão de cada item. Sem vínculo, `proposta` fica vazia e `candidatas` lista as
    contas do tipo certo para o usuário escolher. Registra também o status dos itens."""
    consultar_itens(s, cliente, item_ids, hoje)
    contas_app = list(s.scalars(select(Conta).where(Conta.ativa).order_by(Conta.nome)))
    propostas: list[PropostaPorConta] = []
    for item_id in item_ids:
        try:
            contas_pluggy = cliente.contas(item_id)
        except OpenFinanceErro as exc:
            propostas.append(PropostaPorConta(item_id, "", item_id, "corrente", hoje, hoje, erro=str(exc)))
            continue
        for cj in contas_pluggy:
            account_id = str(cj["id"])
            cartao = cj.get("type") == "CREDIT"
            conta = _conta_vinculada(s, account_id)
            inicio = _janela(s, account_id, conta, hoje, desde)
            nome = cj.get("name") or account_id
            pc = PropostaPorConta(item_id, account_id, nome, "cartao" if cartao else "corrente", inicio, hoje,
                                  candidatas=[c for c in contas_app if (c.tipo == "cartao") == cartao])
            try:
                transacoes = cliente.transacoes(account_id, inicio, hoje)
                faturas = cliente.faturas(account_id) if cartao else []
            except OpenFinanceErro as exc:
                pc.erro = str(exc)
                propostas.append(pc)
                continue
            pc.resultado = resultado_de_conta(cj, transacoes, faturas, representa_fatura=False)
            if conta is not None:
                propor_para_conta(s, pc, conta, hoje)
            propostas.append(pc)
    return propostas
