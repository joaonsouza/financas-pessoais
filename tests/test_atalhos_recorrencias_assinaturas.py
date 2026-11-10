from app.models import Recorrencia
from app.services.assinaturas import criar_assinatura, resumo_assinaturas
from app.services.recorrencia import custo_mensal_recorrencias
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta


def _cadastros(db):
    conta = nova_conta(db)
    db.add_all([
        Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=120000, dia=10, inicio="2026-10"),
        Recorrencia(descricao="Faxina", tipo="despesa", conta_id=conta.id, valor=18500, dia=8, inicio="2026-10",
                    frequencia="semanal", dias_semana="3"),
        Recorrencia(descricao="IPVA", tipo="despesa", conta_id=conta.id, valor=120000, dia=5, inicio="2026-10",
                    intervalo_meses=12),
        Recorrencia(descricao="Salário", tipo="receita", conta_id=conta.id, valor=900000, dia=5, inicio="2026-10"),
        Recorrencia(descricao="Antiga", tipo="despesa", conta_id=conta.id, valor=5000, dia=5, inicio="2026-01",
                    ativa=False),
    ])
    criar_assinatura(db, nome="Spotify", valor=4090, conta_id=conta.id, hoje=HOJE_TESTE)
    db.commit()


def test_custo_mensal_das_recorrencias_de_despesa_ativas(db):
    _cadastros(db)
    # 1.200 + 185 × 52/12 (801,66) + 1.200/12
    assert custo_mensal_recorrencias(db) == (3, 120000 + 80166 + 10000)
    assert resumo_assinaturas(db) == (1, 4090)


def test_lancamentos_tem_cards_no_lugar_dos_icones(client, db):
    _cadastros(db)
    texto = client.get("/lancamentos").text
    assert 'href="/lancamentos/recorrencias"' in texto and "R$ 2.101,66/mês" in texto
    assert 'href="/lancamentos/assinaturas"' in texto and "R$ 40,90/mês" in texto
    assert 'aria-label="Recorrências e assinaturas"' not in texto   # ícone antigo do topo


def test_cartoes_tem_card_de_assinaturas(client, db):
    _cadastros(db)
    texto = client.get("/cartoes").text
    assert 'href="/lancamentos/assinaturas"' in texto and "R$ 40,90/mês" in texto
