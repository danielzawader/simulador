#!/usr/bin/env python3
"""Salva os ajustes da B3 de DI1 e DAP em di-dap-futuro/ajustes.json.

Regras:
- Ajuste: só do arquivo PR oficial da B3 (Price Report, BVBG.086, campo AdjstdQtTax), o mesmo do backfill.py. Cada
  execução confere no PR os últimos pregões encerrados que ainda não estão em "ajuste_pr" (depois das 18:30 de um dia útil,
  o do próprio dia entra na conta). O pregão entra em "ajuste_pr" quando o PR traz o ajuste de todos os seus DI1 e DAP; o do
  DAP pode sair só na 3ª publicação do dia (~19:40-20:05 de Brasília): até lá fica gravado o que já saiu (o DI1) e a
  execução seguinte tenta de novo.
- O campo prvsDayAdjstmntPric da API de cotação NÃO serve para o ajuste: durante o pregão seguinte ele traz o ajuste
  anterior CORRIGIDO (o PU do ajuste atualizado pelo CDI no DI1 e pelo IPCA pro rata no DAP, em taxa, com um dia útil a
  menos) e, depois do fechamento do DI, já é o ajuste do dia (o do DAP muda mais tarde). Com as execuções agendadas do GitHub
  saindo de 4 a 10 h atrasadas, a regra antiga ("antes das 18:30 é o ajuste do pregão anterior") gravou esses números em
  10, 11, 12, 13 e 16/11/2026 (refeitos com o PR oficial em 18/11/2026).
- Depois das 18:30 de um dia útil grava o fechamento do dia pela API de cotação (último negócio, contratos negociados e em
  aberto).

Formato: {"atualizado": "...", "campos": [...], "DI1": {"F28": {"2026-11-10": [ajuste, ultimo, negociados, em_aberto]}},
          "DAP": {...}, "vencimentos": {...}, "ajuste_pr": ["2026-11-17", ...]}
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile

ARQ = os.path.join(os.path.dirname(__file__), "..", "..", "di-dap-futuro", "ajustes.json")
URL = "https://cotacao.b3.com.br/mds/api/v1/DerivativeQuotation/"
URL_PR = "https://www.b3.com.br/pesquisapregao/download?filelist=PR%s.zip,"
UA_PR = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
PREGOES_PR = 3      # pregões encerrados que cada execução confere no PR (o mais recente e os 2 anteriores)
PRAZO_PR = 240      # segundos para baixar um PR inteiro (a B3 às vezes manda a conta-gotas)


def pascoa(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(y, mes, dia)


def feriados(y):
    fx = [(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (12, 25)]
    if y >= 2024:
        fx.append((11, 20))
    s = {dt.date(y, m, d) for m, d in fx}
    p = pascoa(y)
    s |= {p - dt.timedelta(48), p - dt.timedelta(47), p - dt.timedelta(2), p + dt.timedelta(60)}
    return s


def util(d):
    return d.weekday() < 5 and d not in feriados(d.year)


def util_anterior(d):
    d -= dt.timedelta(1)
    while not util(d):
        d -= dt.timedelta(1)
    return d


def baixar(c):
    req = urllib.request.Request(URL + c, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def baixar_pr(dia):
    """Bytes do PRaammdd.zip do pregão (um zip com outro zip dentro e um XML de ~130 MB por publicação do dia)."""
    req = urllib.request.Request(URL_PR % dia.strftime("%y%m%d"), headers=UA_PR)
    t0, partes = time.monotonic(), []
    with urllib.request.urlopen(req, timeout=60) as r:
        while True:
            b = r.read(1 << 20)
            if not b:
                return b"".join(partes)
            partes.append(b)
            if time.monotonic() - t0 > PRAZO_PR:
                raise TimeoutError(f"o download passou de {PRAZO_PR} s")


def membros_xml(raw):
    """Um XML (bytes) por vez, sem segurar todas as publicações na memória."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return
    for n in z.namelist():
        b = z.read(n)
        if b[:2] == b"PK":
            yield from membros_xml(b)
        elif b.lstrip(b"\xef\xbb\xbf \r\n\t")[:1] == b"<":
            yield b


def blocos(x):
    """Só os blocos PricRpt de DI1 e DAP (varrer o XML inteiro com regex é lento)."""
    for alvo in (b"<TckrSymb>DI1", b"<TckrSymb>DAP"):
        i = x.find(alvo)
        while i >= 0:
            a = x.rfind(b"<PricRpt", 0, i)
            b = x.find(b"</PricRpt>", i)
            if a < 0 or b < 0:
                break
            yield x[a:b]
            i = x.find(alvo, b)


def tag(t):
    return re.compile(rb"<(?:\w+:)?%s(?:\s[^>]*)?>([^<]*)</(?:\w+:)?%s>" % (t, t))


R_DT = re.compile(rb"<(?:\w+:)?TradDt>\s*<(?:\w+:)?Dt>([^<]*)</")
R_TCK, R_TAX = tag(b"TckrSymb"), tag(b"AdjstdQtTax")


def ajustes_pr(dia, raw=None):
    """({"DI1": {suf: taxa}, "DAP": {suf: taxa}}, [contratos ainda sem ajuste]) do PR do pregão `dia`; None se ainda não saiu.
    Lê só os blocos desse pregão (TradDt): o PR260731.zip, por exemplo, traz também uma publicação de 15/12/2020."""
    alvo = dia.isoformat().encode()
    taxa, vistos = {}, set()
    for x in membros_xml(baixar_pr(dia) if raw is None else raw):
        for b in blocos(x):
            d, t = R_DT.search(b), R_TCK.search(b)
            if not d or not t or d.group(1).strip() != alvo:
                continue
            sym = t.group(1).strip().decode("ascii", "replace")
            if len(sym) != 6 or sym[:3] not in ("DI1", "DAP"):
                continue
            vistos.add(sym)
            v = R_TAX.search(b)
            if sym not in taxa and v and v.group(1).strip():
                taxa[sym] = float(v.group(1))
        if vistos and vistos.issubset(taxa):
            break                       # as publicações seguintes do dia repetem o ajuste
    if not vistos:
        return None
    out = {"DI1": {}, "DAP": {}}
    for sym, v in taxa.items():
        out[sym[:3]][sym[3:]] = v
    return out, sorted(vistos - set(taxa))


def main(agora=None):
    agora = agora or dt.datetime.now(dt.timezone(dt.timedelta(hours=-3)))
    hoje = agora.date()
    minutos = agora.hour * 60 + agora.minute
    fechamento = util(hoje) and minutos >= 18 * 60 + 30

    try:
        hist = json.load(open(ARQ, encoding="utf-8"))
    except FileNotFoundError:
        hist = {}
    antes = json.dumps(hist, sort_keys=True)
    hist.setdefault("campos", ["ajuste", "ultimo", "negociados", "em_aberto"])
    feitos = set(hist.get("ajuste_pr") or [])
    erros = 0

    # ajuste oficial: PR da B3 dos últimos pregões encerrados que ainda não vieram completos
    d = hoje if fechamento else util_anterior(hoje)
    for _ in range(PREGOES_PR):
        k = d.isoformat()
        if k not in feitos:
            try:
                res = ajustes_pr(d)
            except Exception as e:  # noqa: BLE001 - rede/B3: a próxima execução tenta de novo
                print(f"PR {k}: {type(e).__name__}: {e}")
                erros += 1
                res = False
            if res is None:
                print(f"PR {k}: ainda não saiu")
            elif res:
                aj, faltam = res
                for c in ("DI1", "DAP"):
                    bloco = hist.setdefault(c, {})
                    for suf, v in aj[c].items():
                        bloco.setdefault(suf, {}).setdefault(k, [None, None, None, None])[0] = v
                if not faltam:
                    feitos.add(k)
                print(f"PR {k}: ajuste de {len(aj['DI1'])} DI1 e {len(aj['DAP'])} DAP"
                      + (f"; ainda sem ajuste: {' '.join(faltam)}" if faltam else " (completo)"))
        d = util_anterior(d)
    if feitos:
        hist["ajuste_pr"] = sorted(feitos)[-20:]

    # fechamento do dia (depois das 18:30 de um dia útil) e vencimentos: API de cotação
    for c in ("DI1", "DAP"):
        try:
            j = baixar(c)
        except Exception as e:  # noqa: BLE001
            print(c, "cotação:", type(e).__name__, e)
            erros += 1
            continue
        if (j.get("BizSts") or {}).get("cd") != "OK":
            print(c, "resposta sem OK:", j.get("BizSts"))
            erros += 1
            continue
        bloco = hist.setdefault(c, {})
        for s in j.get("Scty", []):
            sym = s.get("symb") or ""
            if len(sym) != 6 or (s.get("mkt") or {}).get("cd") != "FUT":
                continue
            q = s.get("SctyQtn") or {}
            a = (s.get("asset") or {}).get("AsstSummry") or {}
            suf = sym[3:]
            if fechamento:
                reg = bloco.setdefault(suf, {}).setdefault(hoje.isoformat(), [None, None, None, None])
                reg[1] = q.get("curPrc")
                reg[2] = a.get("traddCtrctsQty")
                reg[3] = a.get("opnCtrcts")
            if a.get("mtrtyCode"):
                hist.setdefault("vencimentos", {})[c + suf] = a["mtrtyCode"]

    if json.dumps(hist, sort_keys=True) == antes:
        print("nada novo")
        return 1 if erros else 0
    hist["atualizado"] = agora.isoformat(timespec="minutes")
    os.makedirs(os.path.dirname(ARQ), exist_ok=True)
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print("gravado" + (f" ({erros} erro(s) acima)" if erros else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
