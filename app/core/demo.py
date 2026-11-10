"""Modo demonstração (`FINANCAS_DEMO=1`, ligado pelo iniciar_demo.bat): banco fictício, faixa no topo, sem backup e
sem IA. Lido a cada chamada para os testes ligarem e desligarem."""
import os


def modo_demo() -> bool:
    return os.environ.get("FINANCAS_DEMO") == "1"
