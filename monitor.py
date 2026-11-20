#!/usr/bin/env python3
"""Monitor de ações e FIIs da B3.

Busca cotações na brapi.dev, aplica as regras do config.json e grava:
  data/sinais.json        -> o que o app lê (sinais + cotação de todos)
  data/historico/XXXX.csv -> fechamentos acumulados por ativo

Só usa a biblioteca padrão do Python. Uso:
  BRAPI_TOKEN=... python3 monitor.py
  python3 monitor.py --tickers PETR4,VALE3 --sem-noticias   (teste rápido)
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from indicadores import avaliar, ordem_de_queda, rsi, variacao_pct

RAIZ = Path(__file__).resolve().parent
BRAPI = "https://brapi.dev/api"
BRASILIA = timezone(timedelta(hours=-3))
VERSAO_CONTRATO = 1


# ---------------------------------------------------------------- rede

def baixar(url, token=None, tentativas=3):
    """GET com novas tentativas em falha temporária. Devolve bytes."""
    cabecalhos = {"User-Agent": "monitor-acoes/1.0"}
    if token:
        cabecalhos["Authorization"] = f"Bearer {token}"
    ultimo_erro = None
    for tentativa in range(tentativas):
        try:
            pedido = urllib.request.Request(url, headers=cabecalhos)
            with urllib.request.urlopen(pedido, timeout=30) as resposta:
                return resposta.read()
        except urllib.error.HTTPError as erro:
            ultimo_erro = erro
            if erro.code not in (429, 500, 502, 503, 504):
                corpo = erro.read().decode("utf-8", "replace")[:200]
                raise RuntimeError(f"HTTP {erro.code}: {corpo}") from erro
        except (urllib.error.URLError, TimeoutError) as erro:
            ultimo_erro = erro
        time.sleep(2 ** tentativa * 2)
    raise RuntimeError(f"falhou após {tentativas} tentativas: {ultimo_erro}")


def baixar_json(url, token=None):
    return json.loads(baixar(url, token))


# ------------------------------------------------------------ universo

def listar(tipo, limite, token):
    url = (f"{BRAPI}/quote/list?type={tipo}&sortBy=volume"
           f"&sortOrder=desc&limit={limite}")
    return baixar_json(url, token).get("stocks", [])


def montar_universo(cfg, token):
    """Devolve {ticker: {"tipo": "acao"|"fii", "nome": str}}.

    Por padrão o universo é dinâmico (os mais negociados do dia), para a
    lista não envelhecer quando a B3 troca códigos ou a carteira do índice.
    """
    u = cfg["universo"]
    universo = {}

    def serve(item):
        return (item.get("close") or 0) >= u["preco_minimo"]

    if u["acoes_top"] > 0:
        acoes = [i for i in listar("stock", 300, token)
                 if i.get("subType") in ("stock", "unit")
                 and not i["stock"].endswith("F") and serve(i)]
        for i in acoes[:u["acoes_top"]]:
            universo[i["stock"]] = {"tipo": "acao", "nome": i.get("name") or i["stock"]}

    if u["fiis_top"] > 0:
        fiis = [i for i in listar("fund", 500, token)
                if i.get("subType") == "fii" and serve(i)]
        for i in fiis[:u["fiis_top"]]:
            universo[i["stock"]] = {"tipo": "fii", "nome": i.get("name") or i["stock"]}

    # Extras entram sempre, mesmo fora dos mais negociados.
    for chave, tipo in (("extras_acoes", "acao"), ("extras_fiis", "fii")):
        for t in u.get(chave, []):
            universo.setdefault(t.upper(), {"tipo": tipo, "nome": t.upper()})
    for t in u.get("ignorar", []):
        universo.pop(t.upper(), None)
    return universo


def tipo_por_codigo(ticker):
    """Palpite usado só na opção --tickers: final 11 costuma ser FII."""
    return "fii" if ticker.upper().endswith("11") else "acao"


# ------------------------------------------------------------- cotação

def dia_brasilia(epoch):
    return datetime.fromtimestamp(epoch, BRASILIA).date().isoformat()


def buscar_ativo(ticker, token):
    """Uma chamada por ativo: cotação atual + 3 meses de fechamentos."""
    dados = baixar_json(f"{BRAPI}/quote/{ticker}?range=3mo&interval=1d", token)
    if dados.get("error") or not dados.get("results"):
        raise RuntimeError(dados.get("message") or "sem resultado")
    r = dados["results"][0]
    preco = r.get("regularMarketPrice")
    if not preco:
        raise RuntimeError("sem preço")

    fechamentos = []  # (data, fechamento ajustado, fechamento bruto, volume)
    for h in r.get("historicalDataPrice") or []:
        if h.get("close") is None or h.get("date") is None:
            continue
        ajustado = h.get("adjustedClose") or h["close"]
        fechamentos.append((dia_brasilia(h["date"]), ajustado, h["close"], h.get("volume") or 0))
    fechamentos.sort()

    pregao = fechamentos[-1][0] if fechamentos else None
    momento = r.get("regularMarketTime")
    if momento:
        dia = datetime.fromisoformat(momento.replace("Z", "+00:00")).astimezone(BRASILIA)
        # Negócio em horário de pregão depois do último candle diário:
        # entra como ponto de hoje (o candle do dia ainda não fechou).
        em_pregao = dia.weekday() < 5 and 10 <= dia.hour <= 19
        if fechamentos and dia.date().isoformat() > pregao and em_pregao:
            pregao = dia.date().isoformat()
            fechamentos.append((pregao, preco, preco, r.get("regularMarketVolume") or 0))

    return {
        "ticker": ticker,
        "nome_api": r.get("longName") or r.get("shortName"),
        "preco": preco,
        "var_dia": r.get("regularMarketChangePercent"),
        "min_52s": r.get("fiftyTwoWeekLow"),
        "max_52s": r.get("fiftyTwoWeekHigh"),
        "pl": r.get("priceEarnings"),
        "pregao": pregao,
        "fechamentos": fechamentos,
        "serie": [f[1] for f in fechamentos],
    }


# ----------------------------------------------------------- histórico

def atualizar_historico(pasta, ticker, fechamentos):
    """Junta os fechamentos novos ao CSV do ativo; devolve a série longa."""
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / f"{ticker}.csv"
    linhas = {}
    if arquivo.exists():
        with arquivo.open(newline="", encoding="utf-8") as f:
            for linha in csv.DictReader(f):
                linhas[linha["data"]] = (linha["fechamento"], linha["volume"])
    antes = dict(linhas)
    for data, _ajustado, bruto, volume in fechamentos:
        # Dado novo da API vence o gravado (corrige um ponto parcial do dia).
        linhas[data] = (f"{bruto:.4f}".rstrip("0").rstrip("."), str(int(volume)))
    if linhas != antes:
        with arquivo.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["data", "fechamento", "volume"])
            for data in sorted(linhas):
                w.writerow([data, *linhas[data]])
    return [float(linhas[d][0]) for d in sorted(linhas)]


# ------------------------------------------------------------ notícias

def buscar_noticias(ticker, quantas, dias):
    """Manchetes recentes do Google Notícias (só título, fonte e link)."""
    consulta = urllib.parse.quote(f"{ticker} when:{dias}d")
    url = (f"https://news.google.com/rss/search?q={consulta}"
           "&hl=pt-BR&gl=BR&ceid=BR:pt-419")
    raiz = ET.fromstring(baixar(url))
    noticias = []
    for item in raiz.findall("./channel/item"):
        titulo = (item.findtext("title") or "").strip()
        fonte = (item.findtext("source") or "").strip()
        if fonte and titulo.endswith(f" - {fonte}"):
            titulo = titulo[: -len(fonte) - 3]
        try:
            data = parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        noticias.append({
            "titulo": titulo,
            "fonte": fonte,
            "link": item.findtext("link"),
            "data": data.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return noticias[:quantas]  # mantém a ordem de relevância do Google


# ----------------------------------------------------------- principal

def arredondar(valor, casas=2):
    return None if valor is None else round(valor, casas)


def executar(cfg, token, saida, tickers=None, com_noticias=True):
    if tickers:
        universo = {t: {"tipo": tipo_por_codigo(t), "nome": t} for t in tickers}
    else:
        universo = montar_universo(cfg, token)
    print(f"Universo: {len(universo)} ativos")

    anterior = {}
    if saida.exists():
        try:
            anterior = {s["ticker"]: s for s in json.loads(saida.read_text("utf-8"))["sinais"]}
        except (ValueError, KeyError):
            anterior = {}

    sinais, cotacoes, erros, pregoes = [], [], [], []
    for ticker, info in sorted(universo.items()):
        try:
            ativo = buscar_ativo(ticker, token)
        except Exception as erro:  # um ativo com problema não derruba a rodada
            erros.append(f"{ticker}: {erro}")
            print(f"  ! {ticker}: {erro}")
            continue
        finally:
            time.sleep(cfg.get("pausa_entre_chamadas_s", 0.3))

        longa = atualizar_historico(saida.parent / "historico", ticker, ativo["fechamentos"])
        if not ativo["min_52s"] and len(longa) >= 200:
            ativo["min_52s"] = min(longa[-250:])
        if ativo["pregao"]:
            pregoes.append(ativo["pregao"])

        # Linha enxuta de todo ativo monitorado: é o que o app usa para os
        # favoritos, mesmo quando o ativo não tem sinal.
        basico = {
            "ticker": ticker,
            "nome": info["nome"] if info["nome"] != ticker else (ativo["nome_api"] or ticker),
            "tipo": info["tipo"],
            "preco": arredondar(ativo["preco"]),
            "var_dia": arredondar(ativo["var_dia"]),
            "var_semana": arredondar(variacao_pct(ativo["serie"], 5)),
            "var_mes": arredondar(variacao_pct(ativo["serie"], 21)),
        }
        cotacoes.append(basico)

        regras = cfg["regras"][info["tipo"]]
        disparadas = avaliar(ativo, regras)
        if len(disparadas) < cfg["pontos_minimos"]:
            continue

        dist = None
        if ativo["min_52s"]:
            dist = (ativo["preco"] / ativo["min_52s"] - 1) * 100
        sinais.append({
            **basico,
            "min_52s": arredondar(ativo["min_52s"]),
            "max_52s": arredondar(ativo["max_52s"]),
            "dist_min_52s": arredondar(dist, 1),
            "rsi14": arredondar(rsi(ativo["serie"], 14), 1),
            "pl": arredondar(ativo["pl"], 1) if info["tipo"] == "acao" else None,
            "regras": disparadas,
            "pontos": len(disparadas),
            "desde": anterior.get(ticker, {}).get("desde") or ativo["pregao"],
            "noticias": [],
        })

    if universo and len(erros) == len(universo):
        print("Nenhum ativo respondeu; mantendo o arquivo anterior.")
        return 1

    # Quem mais caiu hoje vem primeiro, nos sinais e na lista completa.
    sinais.sort(key=ordem_de_queda)
    sinais = sinais[: cfg["max_sinais"]]
    cotacoes.sort(key=ordem_de_queda)

    n = cfg["noticias"]
    if com_noticias and n.get("ativa"):
        for sinal in sinais[: n["max_ativos"]]:
            try:
                sinal["noticias"] = buscar_noticias(sinal["ticker"], n["por_ativo"], n["dias"])
            except Exception as erro:  # notícia é complemento; segue sem ela
                print(f"  ! notícias {sinal['ticker']}: {erro}")
            time.sleep(0.5)

    resultado = {
        "versao": VERSAO_CONTRATO,
        "gerado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pregao": max(pregoes) if pregoes else None,
        "resumo": {
            "monitorados": len(universo),
            "com_sinal": len(sinais),
            "erros": len(erros),
        },
        "sinais": sinais,
        "cotacoes": cotacoes,
        "erros": erros,
    }
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(resultado, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(f"{len(sinais)} sinais, {len(erros)} erros -> {saida}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=str(RAIZ / "config.json"))
    p.add_argument("--saida", default=str(RAIZ / "data" / "sinais.json"))
    p.add_argument("--tickers", help="lista separada por vírgula (ignora o universo)")
    p.add_argument("--sem-noticias", action="store_true")
    a = p.parse_args()

    cfg = json.loads(Path(a.config).read_text("utf-8"))
    token = os.environ.get("BRAPI_TOKEN") or None
    if not token:
        print("Aviso: BRAPI_TOKEN não definido; só os tickers de teste da brapi respondem.")
    tickers = [t.strip().upper() for t in a.tickers.split(",")] if a.tickers else None
    return executar(cfg, token, Path(a.saida), tickers, not a.sem_noticias)


if __name__ == "__main__":
    sys.exit(main())
