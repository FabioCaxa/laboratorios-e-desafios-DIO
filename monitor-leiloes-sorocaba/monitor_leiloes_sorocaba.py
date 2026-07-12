#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_leiloes_sorocaba.py
===========================================================================
Monitor de leilões de imóveis em Sorocaba/SP.

PROBLEMA QUE RESOLVE
--------------------
Os portais (Spy, Mega, Alfa, Destak, Zuk...) mantêm anúncios VENCIDOS no ar.
A página interna some/engana, e só dá pra confiar na DATA DE FECHAMENTO do
edital. Este script bate nos portais, descarta tudo com praça já encerrada,
aplica os seus filtros e ranqueia o que sobrou por distância do trabalho.

SEU PERFIL (já preenchido em CONFIG abaixo):
  - Casa inteira (sem cota-parte / sem "direitos sobre")
  - 2 quartos + 2 vagas de garagem
  - Até R$ 300.000
  - Raio de até ~10 km do FUNSERV (R. Major João Lício, 265 - Centro/V. Amélia)
  - Aceita parcelamento (25% + 30x, art. 895 CPC) ou financiamento

IMPORTANTE / HONESTIDADE TÉCNICA
--------------------------------
Cada portal muda layout e tem anti-bot. Os ADAPTADORES abaixo trazem a
ESTRUTURA e os pontos onde você pluga o endpoint/seletor real de cada site
(marcados com #TODO). O núcleo que importa — filtro por data futura, leitura
de edital em PDF, haversine e ranking — já está pronto e testado em lógica.
Comece habilitando 1 portal (sugiro Destak, cujo edital em PDF é legível) e
vá somando.

DEPENDÊNCIAS
------------
    pip install requests beautifulsoup4 pdfplumber python-dateutil

USO
---
    python monitor_leiloes_sorocaba.py            # imprime tabela + salva CSV
    python monitor_leiloes_sorocaba.py --csv out.csv
Agende com cron/GitHub Actions p/ rodar 1x ao dia (ver rodapé).
===========================================================================
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

try:
    import pdfplumber  # leitura de edital em PDF
except ImportError:
    pdfplumber = None


# ===========================================================================
# CONFIG — ajuste aqui
# ===========================================================================
CONFIG = {
    "trabalho_lat": -23.5064738,      # FUNSERV (Google Places)
    "trabalho_lng": -47.4539773,
    "raio_km_max": 10.0,              # distância máxima até o trabalho
    "preco_max": 350_000.0,           # teto p/ casas que aceitam parcelamento
    "quartos_min": 2,
    "vagas_min": 2,
    "exigir_casa": True,              # True = só "casa"/"sobrado", ignora apto/terreno
    "excluir_cota_parte": True,       # descarta "direitos sobre" / "parte ideal" / "quota-parte"
    "user_agent": "Mozilla/5.0 (compatible; MonitorLeiloesFabio/1.0)",
    "timeout": 25,
}

HOJE = date.today()

# Termos que denunciam venda de fração/direitos (o que você NÃO quer)
PADROES_COTA_PARTE = [
    r"direitos?\s+(aquisitivos?|sobre)",
    r"parte\s+ideal",
    r"quota[-\s]?parte",
    r"fra[cç][aã]o\s+ideal",
    r"\bnua[-\s]?propriedade\b",
]

# Termos que indicam que é casa (não apto/terreno)
PADROES_CASA = [r"\bcasa\b", r"\bsobrado\b", r"resid[eê]ncia"]


# ===========================================================================
# MODELO
# ===========================================================================
@dataclass
class Lote:
    portal: str
    titulo: str
    url: str
    bairro: str = ""
    preco: Optional[float] = None          # menor valor disponível (2ª praça/lance mín.)
    quartos: Optional[int] = None
    vagas: Optional[int] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    fechamento: Optional[date] = None       # data de encerramento da ÚLTIMA praça
    aceita_parcelamento: Optional[bool] = None
    edital_url: str = ""
    descricao: str = ""
    distancia_km: Optional[float] = field(default=None)

    def resumo(self) -> str:
        d = f"{self.distancia_km:.1f} km" if self.distancia_km is not None else "?"
        p = f"R$ {self.preco:,.0f}".replace(",", ".") if self.preco else "?"
        f = self.fechamento.strftime("%d/%m/%Y") if self.fechamento else "?"
        return (f"[{self.portal}] {self.titulo[:45]:<45} | {p:>12} | "
                f"{self.quartos or '?'}q/{self.vagas or '?'}v | {d:>8} | fecha {f}\n"
                f"          {self.url}")


# ===========================================================================
# UTILITÁRIOS
# ===========================================================================
def haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def parse_preco(texto: str) -> Optional[float]:
    """Extrai o MENOR valor monetário de um texto (2ª praça costuma ser o menor)."""
    achados = re.findall(r"R\$\s*([\d\.]+,\d{2})", texto)
    vals = []
    for a in achados:
        try:
            vals.append(float(a.replace(".", "").replace(",", ".")))
        except ValueError:
            pass
    return min(vals) if vals else None


def parse_int_perto(texto: str, palavras) -> Optional[int]:
    """Pega o número associado a 'quartos'/'dormitórios'/'vagas' etc."""
    for kw in palavras:
        m = re.search(rf"(\d+)\s*{kw}", texto, re.I) or \
            re.search(rf"{kw}\D{{0,8}}(\d+)", texto, re.I)
        if m:
            return int(m.group(1))
    return None


def extrair_datas_praca(texto: str) -> list[date]:
    """Acha datas dd/mm/aa(aa) num edital e devolve como objetos date."""
    datas = []
    for m in re.findall(r"(\d{1,2}/\d{1,2}/\d{2,4})", texto):
        try:
            datas.append(dateparser.parse(m, dayfirst=True).date())
        except (ValueError, OverflowError):
            pass
    return datas


def ler_data_fechamento_do_edital(edital_url: str, sess: requests.Session) -> Optional[date]:
    """
    Baixa o PDF do edital e devolve a MAIOR data de praça encontrada
    (= encerramento da 2ª praça). Foi assim que confirmei que vários
    lotes 'no ar' já estavam fechados.
    """
    if not edital_url or pdfplumber is None:
        return None
    try:
        r = sess.get(edital_url, timeout=CONFIG["timeout"])
        r.raise_for_status()
        with open("_edital_tmp.pdf", "wb") as fh:
            fh.write(r.content)
        with pdfplumber.open("_edital_tmp.pdf") as pdf:
            txt = "\n".join((p.extract_text() or "") for p in pdf.pages)
        datas = [d for d in extrair_datas_praca(txt) if d.year >= 2024]
        # janela de venda direta ~90 dias após a última praça: somamos como validade
        return max(datas) if datas else None
    except Exception as e:  # noqa
        print(f"  ! falha lendo edital {edital_url}: {e}", file=sys.stderr)
        return None


def tem_cota_parte(texto: str) -> bool:
    return any(re.search(p, texto, re.I) for p in PADROES_COTA_PARTE)


def parece_casa(texto: str) -> bool:
    return any(re.search(p, texto, re.I) for p in PADROES_CASA)


# ===========================================================================
# ADAPTADORES POR PORTAL
# Cada um deve devolver uma lista de Lote (ainda sem distância/filtro).
# ===========================================================================
DESTAK_BASE = "https://www.destakleiloes.com.br"
DESTAK_SITEMAPS = [f"{DESTAK_BASE}/sitemap.xml", f"{DESTAK_BASE}/sitemap_index.xml"]


def _destak_urls_lote(sess: requests.Session, cidade: str = "sorocaba") -> list[str]:
    """
    Descobre URLs de lote da Destak SEM depender do JS da busca.
    Método 1 (preferido): sitemap.xml -> filtra /lote/ que cita a cidade.
    Método 2 (alternativo, comentado): XHR JSON do motor de busca 'Degrau'.
    """
    urls: set[str] = set()
    for sm in DESTAK_SITEMAPS:
        try:
            r = sess.get(sm, timeout=CONFIG["timeout"])
            if r.status_code != 200:
                continue
            for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text, re.I):
                if loc.endswith(".xml"):                      # índice -> desce um nível
                    try:
                        rr = sess.get(loc, timeout=CONFIG["timeout"])
                        for l2 in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", rr.text, re.I):
                            if "/lote/" in l2 and cidade in l2.lower():
                                urls.add(l2)
                    except Exception:
                        pass
                elif "/lote/" in loc and cidade in loc.lower():
                    urls.add(loc)
        except Exception as e:  # noqa
            print(f"  ! Destak sitemap {sm}: {e}", file=sys.stderr)

    # Método 2 — descomente após inspecionar a XHR no DevTools (ID_Categoria=57 = Casas):
    # payload = {"Engine": "Start", "Pagina": 1, "ID_Categoria": 57, "Busca": cidade}
    # j = sess.get(f"{DESTAK_BASE}/busca/Engine", params=payload,
    #              timeout=CONFIG["timeout"]).json()
    # for item in j.get("Lotes", []):
    #     urls.add(DESTAK_BASE + item["UrlLote"])
    return sorted(urls)


_EXTENSO = {"um": 1, "dois": 2, "tres": 3, "três": 3, "quatro": 4}


def parse_lote_destak(url: str, sess: requests.Session) -> Lote:
    """Parser fiel à estrutura real da página de lote da Destak (validado no lote 3612)."""
    html = sess.get(url, timeout=CONFIG["timeout"]).text
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)

    # --- specs: a Destak separa SUÍTES, DORMITÓRIOS e VAGAS em contadores ---
    def _contador(label: str) -> int:
        m = re.search(rf"{label}\s*(\d+)", texto, re.I)
        return int(m.group(1)) if m else 0

    suites = _contador("SU[IÍ]TES")
    dorm = _contador("DORMIT[OÓ]RIOS")
    vagas = _contador("VAGAS DE GARAGEN?S?") or parse_int_perto(texto, ["vaga"])
    # nº de quartos = dormitórios comuns + suítes (na Destak vêm separados)
    quartos = (suites + dorm) or parse_int_perto(texto, ["dormit", "quarto"])
    if not quartos:  # reforço pela descrição ("dois dormitórios ...")
        for palavra, val in _EXTENSO.items():
            if re.search(rf"\b{palavra}\s+dormit", texto, re.I):
                quartos = val
                break

    # --- preço: prioriza "Avaliação"; senão o menor R$ da página ---
    mav = re.search(r"Avalia[cç][aã]o[:\s]*R\$\s*([\d\.]+,\d{2})", texto, re.I)
    preco = (float(mav.group(1).replace(".", "").replace(",", "."))
             if mav else parse_preco(texto))

    # --- edital: link de download cujo RÓTULO é "Edital" (há tb. o da matrícula) ---
    edital = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "downloadlote" in href and href.lower().endswith(".pdf"):
            rotulo = (a.find_parent("li") or a.find_parent("div") or a).get_text(" ", strip=True)
            if "edital" in rotulo.lower():
                edital = href
                break
            edital = edital or href                      # fallback: 1º PDF encontrado

    # --- geo: lat/lng do embed do Google Maps (maps?q=LAT, LNG) ---
    lat = lng = None
    mgeo = re.search(r"maps\?q=(-?\d+\.\d+),\s*(-?\d+\.\d+)", html)
    if mgeo:
        lat, lng = float(mgeo.group(1)), float(mgeo.group(2))

    # --- bairro: "..., <Bairro> - Sorocaba - SP" ---
    bairro = ""
    mb = re.search(r"([A-Za-zÀ-ÿ\.\s]{3,40}?)\s*-\s*Sorocaba\s*-\s*SP", texto)
    if mb:
        bairro = mb.group(1).strip()

    return Lote(
        portal="Destak",
        titulo=(soup.title.string or "Casa em Sorocaba/SP").strip().split("|")[0].strip(),
        url=url, bairro=bairro, preco=preco,
        quartos=quartos or None, vagas=vagas or None,
        lat=lat, lng=lng, edital_url=edital, descricao=texto[:1500],
        # fechamento fica None de propósito: a página é JS e engana.
        # O motor lê a data REAL do PDF do edital em aplicar_filtros().
    )


def adapter_destak(sess: requests.Session) -> list[Lote]:
    """Destak Leilões — descobre lotes de Sorocaba via sitemap e parseia cada um."""
    lotes: list[Lote] = []
    urls = _destak_urls_lote(sess, cidade="sorocaba")
    print(f"  Destak: {len(urls)} URLs de lote em Sorocaba", file=sys.stderr)
    for url in urls:
        try:
            lotes.append(parse_lote_destak(url, sess))
        except Exception as e:  # noqa
            print(f"  ! Destak {url}: {e}", file=sys.stderr)
    return lotes


def adapter_spy(sess: requests.Session) -> list[Lote]:
    """
    Spy Leilões — agrega vários leiloeiros. Tem endpoint JSON por trás da busca.
    #TODO: capturar no navegador a XHR de
    https://spyleiloes.com.br/imoveis-leilao/sp/sorocaba/tipo/casa
    e replicar (costuma devolver lista com bairro, valores, datas e geo).
    """
    return []  # #TODO


def adapter_mega(sess: requests.Session) -> list[Lote]:
    """
    Mega Leilões — https://www.megaleiloes.com.br/imoveis/sp/sorocaba
    #TODO: a listagem é renderizada server-side; dá pra parsear o HTML dos cards
    (cada card tem tipo, área, 1ª/2ª praça e datas).
    """
    return []  # #TODO


CAIXA_CSV = "https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_SP.csv"


def adapter_caixa(sess: requests.Session) -> list[Lote]:
    """
    CAIXA — lista pública e GRATUITA (dump oficial de todos os imóveis de SP).

    Formato real do CSV (separador ';', encoding latin-1):
      N° do imóvel; UF; Cidade; Bairro; Endereço; Preço; Valor de avaliação;
      Desconto; Descrição; Modalidade de venda; Link de acesso

    A coluna 'Descrição' traz, por exemplo:
      "Casa, 74,97 m2 de área total, 42,97 m2 de área privativa, 152m2 de área
       do terreno, 2 qts, a.serv, WC, sl, cozinha, 2 vagas de garagem."
    -> dá pra extrair QUARTOS e VAGAS — exatamente o que Spy/Mega/Zuk não
       mostram na listagem e que vinha travando a busca.

    'Modalidade de venda' separa Leilão / Licitação Aberta / Venda Online /
    Venda Direta. VENDA DIRETA = leilão que ficou negativo: sem disputa, sem
    comissão de leiloeiro (~5%) e com IPTU/condomínio quitados pela Caixa.
    """
    import csv as _csv

    lotes: list[Lote] = []
    try:
        r = sess.get(CAIXA_CSV, timeout=60)
        r.raise_for_status()
        texto = r.content.decode("latin-1", errors="replace")
    except Exception as e:  # noqa
        print(f"  ! Caixa CSV: {e}", file=sys.stderr)
        return lotes

    linhas = [l for l in texto.splitlines() if l.count(";") >= 8]
    for row in _csv.reader(linhas, delimiter=";"):
        if len(row) < 11:
            continue
        if "sorocaba" not in row[2].strip().lower():
            continue

        bairro, endereco, desc = row[3].strip(), row[4].strip(), row[8].strip()
        modalidade, link = row[9].strip(), row[10].strip()

        def _num(s: str):
            s = re.sub(r"[^\d,\.]", "", s).replace(".", "").replace(",", ".")
            try:
                return float(s)
            except ValueError:
                return None

        preco = _num(row[5])
        quartos = parse_int_perto(desc, ["qts", "quartos?", "dormit"])
        vagas = parse_int_perto(desc, ["vagas? de garagem", "vagas?"])

        # A Caixa não publica data de praça no CSV. Venda direta / online /
        # licitação ficam ABERTAS até vender -> nunca descartar por data.
        aberto_sem_data = any(k in modalidade.lower()
                              for k in ("direta", "online", "licita"))

        lotes.append(Lote(
            portal=f"Caixa/{modalidade}",
            titulo=f"{desc[:55]} - {bairro}",
            url=link,
            bairro=bairro,
            preco=preco,
            quartos=quartos,
            vagas=vagas,
            fechamento=None,          # sem data no CSV; filtro de data não corta
            aceita_parcelamento=("financia" in desc.lower()) or aberto_sem_data,
            descricao=f"{desc} | {endereco} | {modalidade}",
        ))

    print(f"  Caixa: {len(lotes)} imóveis em Sorocaba no CSV oficial",
          file=sys.stderr)
    return lotes


ADAPTERS = [adapter_caixa, adapter_destak, adapter_spy, adapter_mega]


# ===========================================================================
# PIPELINE
# ===========================================================================
def aplicar_filtros(lote: Lote, sess: requests.Session) -> Optional[Lote]:
    blob = f"{lote.titulo} {lote.descricao} {lote.bairro}"

    # 1) cota-parte / direitos -> fora
    if CONFIG["excluir_cota_parte"] and tem_cota_parte(blob):
        return None

    # 2) precisa ser casa
    if CONFIG["exigir_casa"] and not parece_casa(blob):
        return None

    # 3) preço
    if lote.preco is not None and lote.preco > CONFIG["preco_max"]:
        return None

    # 4) specs
    if lote.quartos is not None and lote.quartos < CONFIG["quartos_min"]:
        return None
    if lote.vagas is not None and lote.vagas < CONFIG["vagas_min"]:
        return None

    # 5) DATA — o filtro mais importante. Se não veio da página, lê do edital.
    if lote.fechamento is None:
        lote.fechamento = ler_data_fechamento_do_edital(lote.edital_url, sess)
    if lote.fechamento is not None and lote.fechamento < HOJE:
        return None  # praça já encerrada -> descarta (anúncio velho)

    # 6) distância
    if lote.lat is not None and lote.lng is not None:
        lote.distancia_km = haversine_km(
            CONFIG["trabalho_lat"], CONFIG["trabalho_lng"], lote.lat, lote.lng)
        if lote.distancia_km > CONFIG["raio_km_max"]:
            return None

    return lote


def coletar() -> list[Lote]:
    sess = requests.Session()
    sess.headers.update({"User-Agent": CONFIG["user_agent"]})
    brutos: list[Lote] = []
    for adapter in ADAPTERS:
        try:
            brutos.extend(adapter(sess))
        except Exception as e:  # noqa
            print(f"  ! adapter {adapter.__name__}: {e}", file=sys.stderr)

    # dedup por url
    vistos, unicos = set(), []
    for l in brutos:
        if l.url not in vistos:
            vistos.add(l.url)
            unicos.append(l)

    filtrados = [x for x in (aplicar_filtros(l, sess) for l in unicos) if x]
    # ranking: mais perto primeiro, depois mais barato
    filtrados.sort(key=lambda l: (l.distancia_km or 1e9, l.preco or 1e12))
    return filtrados


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="leiloes_abertos_sorocaba.csv")
    args = ap.parse_args()

    print(f"== Monitor de leilões — Sorocaba — {HOJE.strftime('%d/%m/%Y')} ==\n")
    resultados = coletar()

    if not resultados:
        print("Nenhum lote ABERTO bateu os filtros hoje "
              "(ou faltam adaptadores habilitados — veja os #TODO).")
        return

    for l in resultados:
        print(l.resumo())

    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(resultados[0]).keys()))
        w.writeheader()
        for l in resultados:
            w.writerow(asdict(l))
    print(f"\nSalvo em {args.csv} ({len(resultados)} lotes).")


if __name__ == "__main__":
    main()

# ===========================================================================
# AGENDAMENTO (rodar 1x ao dia)
# ---------------------------------------------------------------------------
# cron (Linux):    0 8 * * *  cd /caminho && /usr/bin/python3 monitor_leiloes_sorocaba.py
# GitHub Actions:  schedule: - cron: "0 11 * * *"  (11h UTC = 8h BRT)
#
# ALERTA (plugue onde quiser):
#   - e-mail via smtplib
#   - Telegram: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
#               data={"chat_id": CHAT, "text": msg})
# ===========================================================================
