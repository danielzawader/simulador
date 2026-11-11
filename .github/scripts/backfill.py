#!/usr/bin/env python3
"""Preenche di-dap-futuro/ajustes.json com ajustes passados de DI1 e DAP.

Uso: python3 backfill.py AAAA-MM-DD AAAA-MM-DD   (início e fim, inclusive)
Fontes, nesta ordem:
  1. Arquivo "PR" (Price Report, BVBG.086) da pesquisa por pregão da B3: traz o ajuste em taxa (AdjstdQtTax) e em PU (AdjstdQt).
  2. Página antiga "Ajustes do pregão" (www2.bmf.com.br), que traz o ajuste em PU; a taxa é recalculada com
     PU = 100.000 / (1 + taxa)^(du/252), du do pregão (inclusive) ao vencimento (exclusive).
Dias que já têm ajuste salvo são pulados. Grava um resumo em di-dap-futuro/backfill_log.txt.
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from ajustes import ARQ, util  # noqa: E402

LOG = os.path.join(os.path.dirname(ARQ), "backfill_log.txt")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
MES = "FGHJKMNQUVXZ"
log_lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log_lines.append(s)


def vencimento(c, suf):
    m, y = MES.index(suf[0]), 2000 + int(suf[1:])
    d = dt.date(y, m + 1, 1 if c == "DI1" else 15)
    while not util(d):
        d += dt.timedelta(1)
    return d


def du(a, b):
    n, d = 0, a
    while d < b:
        if util(d):
            n += 1
        d += dt.timedelta(1)
    return n


def taxa_de_pu(c, suf, dia, pu):
    n = du(dia, vencimento(c, suf))
    if n <= 0 or pu <= 0:
        return None
    return round(((1e5 / pu) ** (252 / n) - 1) * 100, 4)


def baixar(url, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def xmls_de_zip(raw, debug=False, nivel=0):
    out = []
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        if debug:
            log("  " * nivel, "zip inválido:", e, raw[:60])
        return out
    nomes = z.namelist()
    if debug:
        log("  " * nivel, "zip com", len(nomes), "arquivos:", nomes[:6])
    for n in nomes:
        b = z.read(n)
        if b[:2] == b"PK":
            out += xmls_de_zip(b, debug, nivel + 1)
        elif b.lstrip(b"\xef\xbb\xbf \r\n\t")[:1] == b"<":
            out.append(b.decode("utf-8-sig", "replace"))
        elif debug:
            log("  " * nivel, "ignorado:", n, b[:60])
    return out


TAG = lambda t: re.compile(r"<(?:\w+:)?%s(?:\s[^>]*)?>([^<]*)</(?:\w+:)?%s>" % (t, t))
R_BLOCO = re.compile(r"<(?:\w+:)?PricRpt>(.*?)</(?:\w+:)?PricRpt>", re.S)
R_TCK, R_TAX, R_PU = TAG("TckrSymb"), TAG("AdjstdQtTax"), TAG("AdjstdQt")


def blocos(x):
    """Só os blocos PricRpt de DI1 e DAP (o XML tem ~130 MB; varrer tudo com regex é lento)."""
    for alvo in ("<TckrSymb>DI1", "<TckrSymb>DAP"):
        i = x.find(alvo)
        while i >= 0:
            a = x.rfind("<PricRpt", 0, i)
            b = x.find("</PricRpt>", i)
            if a < 0 or b < 0:
                break
            yield x[a:b]
            i = x.find(alvo, b)


def fonte_pr(dia, debug):
    nome = "PR%s.zip" % dia.strftime("%y%m%d")
    for url in ("https://www.b3.com.br/pesquisapregao/download?filelist=%s," % nome,
                "https://www.b3.com.br/pesquisapregao/download?filelist=%s" % nome):
        try:
            st, ct, raw = baixar(url)
        except Exception as e:
            if debug:
                log("  PR erro", url, repr(e)[:200])
            continue
        if debug:
            log("  PR", st, ct, len(raw), raw[:80])
        xs = xmls_de_zip(raw, debug)
        if debug and xs:
            x0 = next((x for x in xs if "DI1F" in x), xs[0])
            i = x0.find("DI1F")
            log("  xmls:", len(xs), "tam", [len(x) for x in xs[:4]])
            log("  início:", re.sub(r"\s+", " ", x0[:400]))
            log("  perto de DI1F:", re.sub(r"\s+", " ", x0[max(0, i - 700):i + 900]) if i >= 0 else "não achou DI1F")
        if not xs:
            continue
        out = {"DI1": {}, "DAP": {}}
        mostrou = False
        for x in xs:
            for b in blocos(x):
                if debug and not mostrou and "DI1F" in b:
                    mostrou = True
                    log("  bloco DI1:", re.sub(r"\s+", " ", b)[:1500])
                m = R_TCK.search(b)
                if not m:
                    continue
                sym = m.group(1).strip()
                c = sym[:3]
                if c not in out or len(sym) != 6:
                    continue
                suf = sym[3:]
                t = R_TAX.search(b)
                v = None
                if t and t.group(1).strip():
                    v = float(t.group(1))
                else:
                    p = R_PU.search(b)
                    if p and p.group(1).strip():
                        v = taxa_de_pu(c, suf, dia, float(p.group(1)))
                if v is not None:
                    out[c][suf] = v
        if out["DI1"] or out["DAP"]:
            return out
    return None


def num_br(s):
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fonte_bmf(dia, debug):
    url = "https://www2.bmf.com.br/pages/portal/bmfbovespa/lumis/lum-ajustes-do-pregao-ptBR.asp"
    data = urllib.parse.urlencode({"dData1": dia.strftime("%d/%m/%Y")}).encode()
    try:
        st, ct, raw = baixar(url, data=data)
    except Exception as e:
        if debug:
            log("  BMF erro", repr(e)[:200])
        return None
    txt = raw.decode("latin-1", "replace")
    if debug:
        log("  BMF", st, ct, len(raw), re.sub(r"\s+", " ", txt[:200]))
    out, atual = {"DI1": {}, "DAP": {}}, None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", txt, re.S | re.I):
        cel = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(cel) >= 6 and " - " in cel[0]:
            atual = cel[0].split(" - ")[0].strip()
            cel = cel[1:]
        if len(cel) < 5 or atual not in out:
            continue
        suf, pu = cel[0].strip(), num_br(cel[2])
        if re.fullmatch(r"[FGHJKMNQUVXZ]\d\d", suf) and pu:
            v = taxa_de_pu(atual, suf, dia, pu)
            if v is not None:
                out[atual][suf] = v
    return out if (out["DI1"] or out["DAP"]) else None


def main():
    ini = dt.date.fromisoformat(sys.argv[1])
    fim = dt.date.fromisoformat(sys.argv[2])
    hist = json.load(open(ARQ, encoding="utf-8")) if os.path.exists(ARQ) else {}
    hist.setdefault("campos", ["ajuste", "ultimo", "negociados", "em_aberto"])
    tem = set()
    for c in ("DI1", "DAP"):
        for o in hist.get(c, {}).values():
            for d, r in o.items():
                if r and r[0] is not None:
                    tem.add(d)
    dias, d = [], ini
    while d <= fim:
        if util(d) and d.isoformat() not in tem:
            dias.append(d)
        d += dt.timedelta(1)
    log(f"backfill {ini} a {fim}: {len(dias)} pregões a buscar ({len(tem)} já salvos)")
    ok = falha = 0
    usou = {}
    for i, dia in enumerate(dias):
        debug = i < 1
        res, fonte = fonte_pr(dia, debug), "PR"
        if not res:
            res, fonte = fonte_bmf(dia, debug), "BMF"
        if not res:
            falha += 1
            log(f"{dia}: sem dados")
            if falha >= 8 and ok == 0:
                log("parando: nenhuma fonte respondeu")
                break
            continue
        ok += 1
        usou[fonte] = usou.get(fonte, 0) + 1
        k = dia.isoformat()
        for c in ("DI1", "DAP"):
            bloco = hist.setdefault(c, {})
            for suf, v in res[c].items():
                reg = bloco.setdefault(suf, {}).setdefault(k, [None, None, None, None])
                reg[0] = v
        if debug or i % 20 == 0:
            log(f"{dia}: {fonte} DI1={len(res['DI1'])} DAP={len(res['DAP'])} ex.: {sorted(res['DAP'].items())[:2]}")
        time.sleep(1.0)
        if ok % 25 == 0:
            json.dump(hist, open(ARQ, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    log(f"fim: {ok} pregões gravados, {falha} sem dados, fontes {usou}")
    if ok:
        json.dump(hist, open(ARQ, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    main()
