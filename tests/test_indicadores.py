import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicadores import avaliar, br, media, ordem_de_queda, rsi, variacao_pct  # noqa: E402

REGRAS = {
    "queda_dia": {"ativa": True, "limite_pct": -4},
    "queda_semana": {"ativa": True, "pregoes": 5, "limite_pct": -8},
    "queda_mes": {"ativa": True, "pregoes": 21, "limite_pct": -15},
    "perto_minima_52s": {"ativa": True, "margem_pct": 5},
    "rsi_baixo": {"ativa": True, "periodo": 14, "limite": 30},
    "abaixo_da_media": {"ativa": True, "pregoes": 50, "desconto_pct": 10},
}


def ativo(serie, var_dia=0.0, min_52s=None):
    return {"serie": serie, "preco": serie[-1], "var_dia": var_dia, "min_52s": min_52s}


class Indicadores(unittest.TestCase):
    def test_variacao(self):
        self.assertAlmostEqual(variacao_pct([100, 90], 1), -10)
        self.assertAlmostEqual(variacao_pct([50, 1, 1, 1, 1, 55], 5), 10)
        self.assertIsNone(variacao_pct([100], 1))

    def test_media(self):
        self.assertEqual(media([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(media([1], 2))

    def test_rsi_extremos(self):
        self.assertEqual(rsi(list(range(1, 40))), 100.0)
        self.assertAlmostEqual(rsi(list(range(40, 1, -1))), 0.0)
        self.assertEqual(rsi([10] * 30), 50.0)
        self.assertIsNone(rsi([1, 2, 3]))

    def test_rsi_valor_conhecido(self):
        # Exemplo clássico de Wilder; RSI de 14 períodos ~ 70,5
        serie = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
        self.assertAlmostEqual(rsi(serie), 70.46, places=1)

    def test_br(self):
        self.assertEqual(br(12.345), "12,3")
        self.assertEqual(br(3.6, 2), "3,60")


class Regras(unittest.TestCase):
    def ids(self, a, regras=REGRAS):
        return [r["id"] for r in avaliar(a, regras)]

    def test_estavel_nao_dispara(self):
        self.assertEqual(self.ids(ativo([10.0] * 60, 0.0, 8.0)), [])

    def test_queda_dia(self):
        serie = [10.0, 10.2] * 30 + [9.7]
        self.assertEqual(self.ids(ativo(serie, -4.9, 5.0)), ["queda_dia"])

    def test_limite_exato_dispara(self):
        self.assertIn("queda_dia", self.ids(ativo([10.0] * 60, -4.0)))
        self.assertNotIn("queda_dia", self.ids(ativo([10.0] * 60, -3.99)))

    def test_queda_forte_dispara_varias(self):
        serie = [20.0] * 40 + [20 - i * 0.4 for i in range(1, 23)]
        disparadas = self.ids(ativo(serie, -3.0, serie[-1]))
        for esperado in ("queda_semana", "queda_mes", "perto_minima_52s",
                         "rsi_baixo", "abaixo_da_media"):
            self.assertIn(esperado, disparadas)

    def test_regra_desligada(self):
        regras = {**REGRAS, "queda_dia": {"ativa": False, "limite_pct": -4}}
        self.assertEqual(self.ids(ativo([10.0] * 60, -9.0), regras), [])

    def test_dados_faltando_nao_quebram(self):
        self.assertEqual(self.ids(ativo([10.0, 10.0], None, None)), [])

    def test_texto_descreve_sem_recomendar(self):
        texto = avaliar(ativo([10.0] * 59 + [9.0], -10.0), REGRAS)[0]["texto"]
        self.assertEqual(texto, "Caiu 10,0% no dia")


class Ordem(unittest.TestCase):
    def test_maior_queda_primeiro_e_sem_dado_por_ultimo(self):
        itens = [
            {"ticker": "SUBIU", "var_dia": 2.0, "pontos": 3},
            {"ticker": "SEMDADO", "var_dia": None, "pontos": 5},
            {"ticker": "CAIU", "var_dia": -1.0, "pontos": 1},
            {"ticker": "DESPENCOU", "var_dia": -9.0, "pontos": 1},
        ]
        ordem = [i["ticker"] for i in sorted(itens, key=ordem_de_queda)]
        self.assertEqual(ordem, ["DESPENCOU", "CAIU", "SUBIU", "SEMDADO"])

    def test_empate_decide_por_mais_regras(self):
        itens = [{"ticker": "A", "var_dia": -3.0, "pontos": 1},
                 {"ticker": "B", "var_dia": -3.0, "pontos": 2}]
        self.assertEqual(sorted(itens, key=ordem_de_queda)[0]["ticker"], "B")


if __name__ == "__main__":
    unittest.main()
