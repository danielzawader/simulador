#!/usr/bin/env python3
"""Salva os ajustes da B3 de DI1 e DAP em di-dap-futuro/ajustes.json.

Regras (a B3 troca o "ajuste anterior" em horário variável depois do fechamento):
- Antes das 18:30 de um dia útil, ou em dia sem pregão, o campo prvsDayAdjstmntPric
  é com certeza o ajuste do último pregão encerrado -> grava como ajuste desse pregão.
- Depois das 18:30 de um dia útil grava só o fechamento do dia
  (último negócio, contratos negociados e em aberto); o ajuste fica para a manhã seguinte.

Formato: {"atualizado": "...", "campos": [...], "DI1": {"F28": {"2026-11-10": [ajuste, ultimo, negociados, em_aberto]}}, "DAP": {...}}
"""
import datetime as dt
import json
import os
import sys
import urllib.request

ARQ = os.path.join(os.path.dirname(__file__), "..", "..", "di-dap-futuro", "ajustes.json")
URL = "https://cotacao.b3.com.br/mds/api/v1/DerivativeQuotation/"


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


def main():
    agora = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3)))
    hoje = agora.date()
    minutos = agora.hour * 60 + agora.minute
    fechamento = util(hoje) and minutos >= 18 * 60 + 30
    if fechamento:
        data_aj, data_fech = None, hoje
    else:
        data_aj, data_fech = util_anterior(hoje), None

    try:
        hist = json.load(open(ARQ, encoding="utf-8"))
    except FileNotFoundError:
        hist = {}
    hist.setdefault("campos", ["ajuste", "ultimo", "negociados", "em_aberto"])

    n = 0
    for c in ("DI1", "DAP"):
        j = baixar(c)
        if (j.get("BizSts") or {}).get("cd") != "OK":
            print(c, "resposta sem OK:", j.get("BizSts"))
            continue
        bloco = hist.setdefault(c, {})
        for s in j.get("Scty", []):
            sym = s.get("symb") or ""
            if len(sym) != 6 or (s.get("mkt") or {}).get("cd") != "FUT":
                continue
            q = s.get("SctyQtn") or {}
            a = (s.get("asset") or {}).get("AsstSummry") or {}
            suf = sym[3:]
            if data_aj and q.get("prvsDayAdjstmntPric") is not None:
                reg = bloco.setdefault(suf, {}).setdefault(data_aj.isoformat(), [None, None, None, None])
                reg[0] = q["prvsDayAdjstmntPric"]
                n += 1
            if data_fech:
                reg = bloco.setdefault(suf, {}).setdefault(data_fech.isoformat(), [None, None, None, None])
                reg[1] = q.get("curPrc")
                reg[2] = a.get("traddCtrctsQty")
                reg[3] = a.get("opnCtrcts")
                n += 1
            if a.get("mtrtyCode"):
                hist.setdefault("vencimentos", {})[c + suf] = a["mtrtyCode"]

    if not n:
        print("nada gravado")
        return 1
    hist["atualizado"] = agora.isoformat(timespec="minutes")
    os.makedirs(os.path.dirname(ARQ), exist_ok=True)
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    oque = f"ajuste de {data_aj}" if data_aj else f"fechamento de {data_fech}"
    print(f"{n} registros gravados ({oque})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
