"""Reserva inteligente: ritmo do mês, projeção de consumo (e alerta) e plano para chegar no alvo."""
import re
from datetime import date

from app.models import Meta, dispensar_alerta
from app.services.indicadores_curso import alertas
from app.services.reserva_plano import plano_reserva, projecao_reserva, ritmo_mensal
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 5)
COMP = "2026-10"


def _mes(db, conta, mes: int, receita: int, despesa: int, **kw):
    """Uma receita e uma despesa efetivadas no mês de 2026."""
    novo_lancamento(db, conta, date(2026, mes, 10), receita, descricao="Salário", **kw)
    return novo_lancamento(db, conta, date(2026, mes, 12), -despesa, descricao="Contas", **kw)


def _reserva(db, **campos) -> Meta:
    meta = Meta(**({"nome": "Reserva", "tipo": "reserva_emergencia", "valor_alvo": 1_800_000,
                    "multiplicador_reserva": 3, "valor_manual": 1_000_000} | campos))
    db.add(meta)
    db.flush()
    return meta


def _deficit(db, valor: int = 250_000):
    """Setembro fecha no vermelho: receita 2.000, despesa 2.000 + valor."""
    conta = nova_conta(db)
    _mes(db, conta, 9, 200_000, 200_000 + valor)
    return conta


# ---------- ritmo_mensal ----------

def test_ritmo_media_dos_tres_meses_completos(db):
    conta = nova_conta(db)
    _mes(db, conta, 7, 500_000, 400_000)   # +1.000
    _mes(db, conta, 8, 500_000, 450_000)   # +500
    _mes(db, conta, 9, 500_000, 350_000)   # +1.500
    _mes(db, conta, 10, 500_000, 100_000)  # o mês atual não entra na média
    r = ritmo_mensal(db, HOJE)
    assert (r.sobra, r.base, r.meses) == (100_000, "média de jul–set", ["2026-07", "2026-08", "2026-09"])


def test_ritmo_so_um_mes_completo_usa_o_nome_do_mes(db):
    conta = nova_conta(db)
    _mes(db, conta, 9, 500_000, 350_000)
    r = ritmo_mensal(db, HOJE)
    assert (r.sobra, r.base, r.meses) == (150_000, "setembro", ["2026-09"])


def test_ritmo_ignora_mes_sem_lancamento(db):
    conta = nova_conta(db)
    _mes(db, conta, 7, 500_000, 400_000)   # +1.000
    _mes(db, conta, 9, 500_000, 300_000)   # +2.000; agosto vazio não puxa a média para baixo
    r = ritmo_mensal(db, HOJE)
    assert (r.sobra, r.base, r.meses) == (150_000, "média de jul–set", ["2026-07", "2026-09"])


def test_ritmo_sem_mes_completo_usa_o_previsto_do_mes_atual(db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 1), 400_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 20), -150_000, status="pendente", descricao="Aluguel")
    r = ritmo_mensal(db, HOJE)
    assert (r.sobra, r.base, r.meses) == (250_000, "previsto deste mês", [])


def test_ritmo_aporte_de_meta_nao_conta_como_gasto(db):
    conta = nova_conta(db)
    meta = _reserva(db)
    _mes(db, conta, 9, 500_000, 300_000)                                  # +2.000
    aporte = novo_lancamento(db, conta, date(2026, 9, 15), -100_000, descricao="Guardar")
    aporte.meta_id = meta.id
    db.flush()
    assert ritmo_mensal(db, HOJE).sobra == 200_000                        # o aporte volta para a sobra


# ---------- projecao_reserva ----------

def test_projecao_none_quando_sobra_nao_negativa(db):
    conta = nova_conta(db)
    _mes(db, conta, 9, 500_000, 350_000)
    assert projecao_reserva(db, _reserva(db), HOJE) is None


def test_projecao_meses_ate_acabar_e_mes_estimado(db):
    _deficit(db)                                                          # déficit de 2.500; reserva de 10.000
    p = projecao_reserva(db, _reserva(db), HOJE)
    assert (p.deficit, p.meses_ate_acabar, p.acaba_em, p.cortar, p.base) == (250_000, 4.0, "fev/2027", 250_000, "setembro")


def test_projecao_reserva_menor_que_o_deficit(db):
    _deficit(db)
    p = projecao_reserva(db, _reserva(db, valor_manual=100_000), HOJE)
    assert (p.meses_ate_acabar, p.acaba_em, p.cortar) == (0.4, None, 250_000)


def test_projecao_sugestoes_so_de_desejos_limitadas_a_duas(db):
    conta = _deficit(db)
    lazer = nova_categoria(db, "Lazer", natureza="desejo")
    cinema = nova_categoria(db, "Cinema", pai=lazer, natureza="desejo")
    comer = nova_categoria(db, "Restaurantes", natureza="desejo")
    stream = nova_categoria(db, "Streaming", natureza="desejo")
    aluguel = nova_categoria(db, "Aluguel", natureza="necessidade")
    for cat, valor in ((aluguel, 90_000), (lazer, 10_000), (cinema, 20_000), (comer, 25_000), (stream, 5_000)):
        novo_lancamento(db, conta, date(2026, 10, 3), -valor, categoria=cat, status="pendente")
    p = projecao_reserva(db, _reserva(db), HOJE)
    assert p.sugestoes == [("Lazer", lazer.id, 30_000), ("Restaurantes", comer.id, 25_000)]


def test_projecao_com_menos_de_duas_sugestoes_mostra_as_que_houver(db):
    conta = _deficit(db)
    assert projecao_reserva(db, _reserva(db), HOJE).sugestoes == []
    lazer = nova_categoria(db, "Lazer", natureza="desejo")
    novo_lancamento(db, conta, date(2026, 10, 3), -7_000, categoria=lazer)
    assert projecao_reserva(db, _reserva(db, nome="Outra"), HOJE).sugestoes == [("Lazer", lazer.id, 7_000)]


# ---------- alerta reserva_acaba ----------

def _alerta(db):
    return next((a for a in alertas(db, COMP, HOJE) if a.chave == "reserva_acaba"), None)


def test_alerta_perigo_quando_acaba_em_ate_dois_meses(db):
    _deficit(db)
    _reserva(db, valor_manual=400_000)                                    # 1,6 mês
    a = _alerta(db)
    assert a.nivel == "perigo" and a.titulo == "Reserva acaba em ~1,6 meses" and a.acao_url == "/planejamento#reserva"
    assert "R$ 2.500,00" in a.texto and "setembro" in a.texto and "corte R$ 2.500,00/mês para parar de consumir" in a.texto


def test_alerta_perigo_quando_a_reserva_nao_cobre_o_deficit(db):
    _deficit(db)
    _reserva(db, valor_manual=100_000)
    a = _alerta(db)
    assert a.nivel == "perigo" and a.titulo == "Reserva não cobre o déficit do mês"


def test_alerta_atencao_ate_seis_meses_e_nenhum_acima(db):
    _deficit(db)
    meta = _reserva(db)                                                   # 4 meses
    a = _alerta(db)
    assert a.nivel == "atencao" and a.titulo == "Reserva acaba em ~4 meses"
    meta.valor_manual = 2_000_000                                         # 8 meses
    db.flush()
    assert _alerta(db) is None


def test_alerta_pode_ser_dispensado(db):
    _deficit(db)
    _reserva(db)
    assert _alerta(db) is not None
    dispensar_alerta(db, COMP, "reserva_acaba")
    db.flush()
    assert _alerta(db) is None


# ---------- plano_reserva ----------

def test_plano_datas_dos_degraus_com_aporte_programado(db):
    conta = nova_conta(db)
    _mes(db, conta, 9, 500_000, 400_000)                                  # a sobra existe, mas o aporte manda
    meta = _reserva(db, valor_manual=150_000, pagar_primeiro=True, valor_mensal_pagar=50_000)
    pl = plano_reserva(db, meta, HOJE)
    assert pl.ritmo == 50_000 and not pl.primeiro_parar
    # R$ 1.000 já atingido: sobram 1 mês (faltam 4.500 = 9 meses), 3 meses (16.500 = 33) e 6 meses
    assert [(d.rotulo, d.data_estimada) for d in pl.degraus[:2]] == [("1 mês", "jul/2027"), ("3 meses", "jul/2029")]
    assert len(pl.degraus) == 3


def test_plano_datas_dos_degraus_com_a_sobra_do_mes(db):
    conta = nova_conta(db)
    _mes(db, conta, 9, 500_000, 400_000)                                  # sobra de 1.000
    pl = plano_reserva(db, _reserva(db, valor_manual=150_000), HOJE)
    assert pl.ritmo == 100_000
    assert pl.degraus[0].rotulo == "1 mês" and pl.degraus[0].data_estimada == "mar/2027"   # faltam 4.500 = 5 meses


def test_plano_sugestoes_de_12_e_24_meses(db):
    pl = plano_reserva(db, _reserva(db, valor_manual=150_000), HOJE)      # faltam 16.500
    assert (pl.sugestao_12, pl.sugestao_24) == (137_500, 68_750)
    assert pl.ritmo == 0 and all(d.data_estimada is None for d in pl.degraus)


def test_plano_primeiro_parar_quando_sobra_negativa_e_sem_aporte(db):
    _deficit(db)
    pl = plano_reserva(db, _reserva(db), HOJE)
    assert pl.primeiro_parar and pl.ritmo == 0 and all(d.data_estimada is None for d in pl.degraus)


def test_plano_com_aporte_nao_pede_para_parar_mesmo_no_vermelho(db):
    _deficit(db)
    pl = plano_reserva(db, _reserva(db, pagar_primeiro=True, valor_mensal_pagar=50_000), HOJE)
    assert not pl.primeiro_parar and pl.ritmo == 50_000


def test_plano_alvo_ja_atingido(db):
    pl = plano_reserva(db, _reserva(db, valor_manual=2_000_000), HOJE)
    assert (pl.sugestao_12, pl.sugestao_24) == (0, 0)
    assert all(d.rotulo != "3 meses" for d in pl.degraus)                 # degrau atingido não aparece


def test_plano_degraus_so_os_que_faltam(db):
    pl = plano_reserva(db, _reserva(db, valor_manual=150_000), HOJE)
    assert [d.rotulo for d in pl.degraus] == ["1 mês", "3 meses", "6 meses"]
    assert re.fullmatch(r"[a-z]{3}/\d{4}", plano_reserva(db, _reserva(db, nome="B", pagar_primeiro=True,
                        valor_mensal_pagar=10_000), HOJE).degraus[0].data_estimada)
