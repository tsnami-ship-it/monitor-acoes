"""Cálculos e regras do monitor.

Só funções puras: não acessam rede nem disco, então são fáceis de testar
(ver tests/test_indicadores.py). Toda "série" é uma lista de preços de
fechamento em ordem cronológica, o mais recente por último.
"""


def variacao_pct(serie, pregoes):
    """Variação % do último preço contra o de `pregoes` pregões atrás."""
    if len(serie) <= pregoes:
        return None
    base = serie[-1 - pregoes]
    if not base:
        return None
    return (serie[-1] / base - 1) * 100


def media(serie, pregoes):
    """Média simples dos últimos `pregoes` fechamentos."""
    if len(serie) < pregoes or pregoes <= 0:
        return None
    return sum(serie[-pregoes:]) / pregoes


def rsi(serie, periodo=14):
    """Índice de Força Relativa (suavização de Wilder). 0 a 100."""
    if len(serie) < periodo + 1:
        return None
    difs = [b - a for a, b in zip(serie, serie[1:])]
    ganho = sum(d for d in difs[:periodo] if d > 0) / periodo
    perda = sum(-d for d in difs[:periodo] if d < 0) / periodo
    for d in difs[periodo:]:
        ganho = (ganho * (periodo - 1) + max(d, 0)) / periodo
        perda = (perda * (periodo - 1) + max(-d, 0)) / periodo
    if perda == 0:
        return 100.0 if ganho > 0 else 50.0
    return 100 - 100 / (1 + ganho / perda)


def br(numero, casas=1):
    """Número no formato brasileiro: 12.5 -> '12,5'."""
    return f"{numero:.{casas}f}".replace(".", ",")


def avaliar(ativo, regras):
    """Aplica as regras ativas a um ativo e devolve as que dispararam.

    `ativo` precisa de: serie, preco, var_dia, min_52s.
    `regras` é o bloco do config.json para o tipo do ativo.
    Cada item devolvido é {"id": ..., "texto": ...}; o texto só descreve
    o fato, sem recomendar nada.
    """
    serie, preco = ativo["serie"], ativo["preco"]
    disparadas = []

    def ativa(nome):
        return regras.get(nome, {}).get("ativa", False)

    if ativa("queda_dia") and ativo.get("var_dia") is not None:
        if ativo["var_dia"] <= regras["queda_dia"]["limite_pct"]:
            disparadas.append({
                "id": "queda_dia",
                "texto": f"Caiu {br(-ativo['var_dia'])}% no dia",
            })

    for nome in ("queda_semana", "queda_mes"):
        if not ativa(nome):
            continue
        n = regras[nome]["pregoes"]
        var = variacao_pct(serie, n)
        if var is not None and var <= regras[nome]["limite_pct"]:
            disparadas.append({
                "id": nome,
                "texto": f"Caiu {br(-var)}% em {n} pregões",
            })

    if ativa("perto_minima_52s") and ativo.get("min_52s"):
        dist = (preco / ativo["min_52s"] - 1) * 100
        if dist <= regras["perto_minima_52s"]["margem_pct"]:
            disparadas.append({
                "id": "perto_minima_52s",
                "texto": f"A {br(max(dist, 0))}% da mínima de 52 semanas "
                         f"(R$ {br(ativo['min_52s'], 2)})",
            })

    if ativa("rsi_baixo"):
        valor = rsi(serie, regras["rsi_baixo"]["periodo"])
        if valor is not None and valor <= regras["rsi_baixo"]["limite"]:
            disparadas.append({
                "id": "rsi_baixo",
                "texto": f"RSI {regras['rsi_baixo']['periodo']} em {br(valor)}",
            })

    if ativa("abaixo_da_media"):
        n = regras["abaixo_da_media"]["pregoes"]
        m = media(serie, n)
        if m:
            desconto = (1 - preco / m) * 100
            if desconto >= regras["abaixo_da_media"]["desconto_pct"]:
                disparadas.append({
                    "id": "abaixo_da_media",
                    "texto": f"{br(desconto)}% abaixo da média de {n} pregões",
                })

    return disparadas


def ordem_de_queda(item):
    """Chave de ordenação: maior queda no dia primeiro; sem dado, por último."""
    var = item.get("var_dia")
    return (var is None, var if var is not None else 0, -item.get("pontos", 0))
