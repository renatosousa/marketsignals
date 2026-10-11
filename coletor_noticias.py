"""Coletor de noticias (RSS BR, BCB, Fed, Finnhub, GDELT) com classificacao de sentimento -> SQLite.

Fontes gratuitas: RSS de veiculos brasileiros, feeds do BCB, Federal Reserve, Finnhub (tier gratuito
com FINNHUB_API_KEY), GDELT DOC API. Classificacao local via FinBERT-PT-BR e ProsusAI/finbert (CPU).
TypeSafe Jev opcional (--jev, TYPESAFE_API_KEY), desligado por padrao (custo ~US$ 0,05/dia).

Normaliza URLs, deduplica por hash de titulo e similaridade de Jaccard, classifica em thread separada
e agrega por minuto (noticias_1min) e 5 min (noticias_5min) para features do regime.

Uso: python coletor_noticias.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--jev] [--sem-gdelt]
                                [--sem-classificar] [--fontes infomoney,bcb_noticias,...]
"""
import argparse
import hashlib
import html
import json
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
import warnings
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import feedparser
import numpy as np
import requests

import banco
from relogio import local_br_ms

warnings.filterwarnings("ignore", category=FutureWarning)

# Schema SQLite (passado para banco.abrir)
SCHEMA = """
CREATE TABLE IF NOT EXISTS noticias (
    id TEXT PRIMARY KEY,
    ts_coleta_ms INTEGER NOT NULL,
    ts_pub_ms INTEGER,
    sessao TEXT NOT NULL,
    fonte TEXT NOT NULL,
    idioma TEXT NOT NULL,
    titulo TEXT NOT NULL,
    resumo TEXT,
    url TEXT NOT NULL,
    hash_titulo TEXT NOT NULL,
    dup_de TEXT,
    evento TEXT
);
CREATE INDEX IF NOT EXISTS ix_noticias_coleta ON noticias (ts_coleta_ms);
CREATE INDEX IF NOT EXISTS ix_noticias_hash ON noticias (hash_titulo, ts_coleta_ms);
CREATE INDEX IF NOT EXISTS ix_noticias_fonte ON noticias (fonte, ts_coleta_ms);

CREATE TABLE IF NOT EXISTS noticias_score (
    id TEXT NOT NULL,
    modelo TEXT NOT NULL,
    versao TEXT NOT NULL,
    ts_score_ms INTEGER NOT NULL,
    sentimento REAL,
    p_pos REAL, p_neg REAL, p_neu REAL,
    conf REAL,
    relevancia REAL,
    tema TEXT,
    surpresa REAL,
    extra TEXT,
    PRIMARY KEY (id, modelo, versao)
);
CREATE INDEX IF NOT EXISTS ix_noticias_score_ts ON noticias_score (ts_score_ms);

CREATE TABLE IF NOT EXISTS noticias_1min (
    ts_ms INTEGER NOT NULL,
    escopo TEXT NOT NULL,
    modelo TEXT NOT NULL,
    n INTEGER NOT NULL,
    n_relevantes INTEGER NOT NULL,
    n_neg INTEGER NOT NULL, n_pos INTEGER NOT NULL,
    sent_medio REAL,
    sent_min REAL,
    frac_neg REAL,
    surpresa_max REAL,
    evento INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts_ms, escopo, modelo)
);

CREATE TABLE IF NOT EXISTS noticias_5min (
    ts_ms INTEGER NOT NULL,
    escopo TEXT NOT NULL,
    modelo TEXT NOT NULL,
    n INTEGER NOT NULL, n_relevantes INTEGER NOT NULL,
    sent_medio REAL, sent_min REAL, frac_neg REAL, surpresa_max REAL,
    n_60 INTEGER,
    sent_60 REAL,
    z_volume REAL,
    pico INTEGER NOT NULL DEFAULT 0,
    evento INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts_ms, escopo, modelo)
);

CREATE TABLE IF NOT EXISTS calendario_macro (
    ts_ms INTEGER NOT NULL,
    tipo TEXT NOT NULL,
    pais TEXT NOT NULL,
    descricao TEXT,
    origem TEXT NOT NULL,
    PRIMARY KEY (ts_ms, tipo)
);

CREATE TABLE IF NOT EXISTS noticias_estado (
    fonte TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, ultimo_ok_ms INTEGER,
    falhas_seguidas INTEGER NOT NULL DEFAULT 0, ultimo_id TEXT
);

CREATE TABLE IF NOT EXISTS gdelt_timeline (
    ts_ms INTEGER NOT NULL,
    consulta TEXT NOT NULL,
    volume REAL,
    tom REAL,
    ts_coleta_ms INTEGER NOT NULL,
    PRIMARY KEY (ts_ms, consulta)
);
"""

# Fontes RSS e Atom
FONTES = [
    # RSS Brasil (pt-BR)
    {"id": "infomoney", "url": "https://www.infomoney.com.br/feed/", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "moneytimes", "url": "https://www.moneytimes.com.br/feed/", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "valor", "url": "https://valor.globo.com/rss/valor", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "estadao_economia", "url": "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/economia/", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "exame", "url": "https://exame.com/feed/", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "g1_economia", "url": "https://g1.globo.com/rss/g1/economia/", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "poder360", "url": "https://www.poder360.com.br/feed/", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "braziljournal", "url": "https://braziljournal.com/feed/", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "agenciabrasil_economia", "url": "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "investing_br", "url": "https://br.investing.com/rss/news.rss", "tipo": "rss", "idioma": "pt", "intervalo_s": 60, "ativo": False},
    # BCB (Atom, pt-BR)
    {"id": "bcb_noticias", "url": "https://www.bcb.gov.br/api/feed/sitebcb/sitefeeds/noticias", "tipo": "atom", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "bcb_copom_comunicados", "url": "https://www.bcb.gov.br/api/feed/sitebcb/sitefeeds/comunicadoscopom", "tipo": "atom", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "bcb_copom_atas", "url": "https://www.bcb.gov.br/api/feed/sitebcb/sitefeeds/atascopom", "tipo": "atom", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "bcb_focus", "url": "https://www.bcb.gov.br/api/feed/sitebcb/sitefeeds/focus", "tipo": "atom", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    {"id": "bcb_notastecnicas", "url": "https://www.bcb.gov.br/api/feed/sitebcb/sitefeeds/notastecnicas", "tipo": "atom", "idioma": "pt", "intervalo_s": 60, "ativo": True},
    # Federal Reserve (RSS, en)
    {"id": "fed_press", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "tipo": "rss", "idioma": "en", "intervalo_s": 60, "ativo": True},
]

UA = {"User-Agent": "Mozilla/5.0 (coletor_noticias; uso pessoal)"}
TIMEOUT_RSS = (10, 30)
TIMEOUT_GDELT = (10, 45)
TENTATIVAS = 4


def canon_url(url):
    """URL canonica: minusculas no host, sem fragmento, sem utm_*/fbclid/gclid/ref/amp, sem / final."""
    p = urlparse(url)
    query = parse_qs(p.query)
    for k in list(query):
        if k.lower() in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
                          "fbclid", "gclid", "ref", "amp"):
            del query[k]
    novo_query = urlencode(query, doseq=True)
    path = p.path.rstrip("/") if p.path != "/" else p.path
    return urlunparse((p.scheme.lower(), p.hostname.lower() if p.hostname else "",
                       path, p.params, novo_query, ""))


def norm_titulo(titulo):
    """Titulo normalizado: NFKD sem acentos, minusculas, sem pontuacao, sem prefixos."""
    t = unicodedata.normalize("NFKD", titulo)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"^(AO VIVO|URGENTE|VIDEO|ATENCAO|BREAKING):\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return " ".join(t.split())


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def jaccard(a, b):
    """Jaccard de tokens (palavras com 3+ letras)."""
    ta = {w for w in a.split() if len(w) >= 3}
    tb = {w for w in b.split() if len(w) >= 3}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def limpar_html(texto):
    """Remove HTML e trunca em 1000 chars."""
    if not texto:
        return None
    t = html.unescape(texto)
    t = re.sub(r"<[^>]+>", " ", t)
    t = " ".join(t.split())
    return t[:1000] if len(t) > 1000 else t


def ts_br_ms(struct_time_utc=None, epoch_s=None):
    """Converte struct_time UTC ou epoch UTC em milissegundos para 'hora de Brasilia como epoch UTC'."""
    if struct_time_utc:
        dt_utc = datetime(*struct_time_utc[:6], tzinfo=timezone.utc)
        dt_br = dt_utc - timedelta(hours=3)
        return int(dt_br.replace(tzinfo=timezone.utc).timestamp() * 1000)
    if epoch_s:
        dt_utc = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        dt_br = dt_utc - timedelta(hours=3)
        return int(dt_br.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return None


def parse_feed(resp, fonte_id):
    """Parse RSS/Atom com feedparser. Retorna lista de dicts {titulo, url, resumo, ts_pub_ms}."""
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        return None
    itens = []
    for e in feed.entries:
        titulo = e.get("title", "").strip()
        link = e.get("link", "").strip()
        if not titulo or not link:
            continue
        resumo = e.get("summary", "") or e.get("description", "")
        ts_pub = None
        for campo in ("published_parsed", "updated_parsed"):
            if campo in e and e[campo]:
                ts_pub = ts_br_ms(struct_time_utc=e[campo])
                break
        if fonte_id == "exame" and ts_pub is None and "published" in e:
            try:
                dt = datetime.fromisoformat(e["published"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=-3)))
                ts_pub = int(dt.astimezone(timezone.utc).timestamp() * 1000)
            except:
                pass
        itens.append({"titulo": titulo, "url": link, "resumo": limpar_html(resumo), "ts_pub_ms": ts_pub})
    return itens if itens else None


def parse_finnhub(resp, categoria):
    """Parse JSON do Finnhub. Retorna lista de dicts."""
    data = resp.json()
    itens = []
    for item in data:
        itens.append({
            "titulo": item.get("headline", "").strip(),
            "url": item.get("url", "").strip(),
            "resumo": limpar_html(item.get("summary", "")),
            "ts_pub_ms": ts_br_ms(epoch_s=item.get("datetime")),
            "finnhub_id": item.get("id"),
        })
    return itens if itens else None


def buscar(fonte, estado):
    """Busca uma fonte. Retorna (itens, novo_estado) ou (None, estado) em falha."""
    headers = UA.copy()
    if estado.get("etag"):
        headers["If-None-Match"] = estado["etag"]
    if estado.get("last_modified"):
        headers["If-Modified-Since"] = estado["last_modified"]
    
    try:
        r = requests.get(fonte["url"], headers=headers, timeout=TIMEOUT_RSS)
        if r.status_code == 304:
            return None, {**estado, "ultimo_ok_ms": int(local_br_ms()), "falhas_seguidas": 0}
        r.raise_for_status()
        itens = parse_feed(r, fonte["id"])
        if itens is None:
            return None, {**estado, "falhas_seguidas": estado.get("falhas_seguidas", 0) + 1}
        novo_estado = {
            **estado,
            "etag": r.headers.get("ETag"),
            "last_modified": r.headers.get("Last-Modified"),
            "ultimo_ok_ms": int(local_br_ms()),
            "falhas_seguidas": 0,
        }
        return itens, novo_estado
    except Exception as e:
        return None, {**estado, "falhas_seguidas": estado.get("falhas_seguidas", 0) + 1}


def buscar_finnhub(categoria, estado, api_key):
    """Busca Finnhub por categoria. Retorna (itens, novo_estado)."""
    url = f"https://finnhub.io/api/v1/news?category={categoria}&token={api_key}"
    ultimo_id = estado.get("ultimo_id")
    if ultimo_id:
        url += f"&minId={ultimo_id}"
    
    headers = UA.copy()
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT_RSS)
        r.raise_for_status()
        itens = parse_finnhub(r, categoria)
        if itens is None:
            return None, {**estado, "falhas_seguidas": estado.get("falhas_seguidas", 0) + 1}
        max_id = max((item.get("finnhub_id") for item in itens if item.get("finnhub_id")), default=ultimo_id)
        novo_estado = {
            **estado,
            "ultimo_ok_ms": int(local_br_ms()),
            "falhas_seguidas": 0,
            "ultimo_id": max_id,
        }
        return itens, novo_estado
    except Exception as e:
        return None, {**estado, "falhas_seguidas": estado.get("falhas_seguidas", 0) + 1}


def gravar_noticias(db, noticias_novas, sessao, ts_coleta_ms, dedup_recentes):
    """Grava noticias com deduplicacao. Retorna (n_novas, n_dup, originais)."""
    if not noticias_novas:
        return 0, 0, []
    
    janela_48h = ts_coleta_ms - 48 * 3600 * 1000
    janela_2h = ts_coleta_ms - 2 * 3600 * 1000
    
    cursor = db.execute("SELECT id, hash_titulo FROM noticias WHERE ts_coleta_ms >= ? ORDER BY ts_coleta_ms",
                        (janela_48h,))
    hash_para_id = {row[1]: row[0] for row in cursor}
    
    originais = []
    linhas = []
    n_dup = 0
    
    for item in noticias_novas:
        url_can = canon_url(item["url"])
        id_noticia = sha1(url_can)
        titulo = item["titulo"]
        hash_t = sha1(norm_titulo(titulo))
        
        dup_de = None
        if hash_t in hash_para_id:
            dup_de = hash_para_id[hash_t]
            n_dup += 1
        else:
            titulo_norm = norm_titulo(titulo)
            for rec in dedup_recentes:
                if rec["ts_coleta_ms"] >= janela_2h and jaccard(titulo_norm, rec["titulo_norm"]) >= 0.8:
                    dup_de = rec["id"]
                    n_dup += 1
                    break
        
        ts_pub = item.get("ts_pub_ms")
        if ts_pub and ts_pub > ts_coleta_ms + 5 * 60 * 1000:
            ts_pub = None
        
        linhas.append((
            id_noticia, ts_coleta_ms, ts_pub, sessao, item["fonte"], item["idioma"],
            titulo, item.get("resumo"), url_can, hash_t, dup_de, item.get("evento")
        ))
        
        if dup_de is None:
            originais.append({
                "id": id_noticia,
                "titulo": titulo,
                "resumo": item.get("resumo") or "",
                "idioma": item["idioma"],
                "fonte": item["fonte"],
            })
            dedup_recentes.append({
                "id": id_noticia,
                "titulo_norm": norm_titulo(titulo),
                "ts_coleta_ms": ts_coleta_ms,
            })
    
    db.executemany(
        "INSERT OR IGNORE INTO noticias VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        linhas
    )
    
    while dedup_recentes and dedup_recentes[0]["ts_coleta_ms"] < janela_2h:
        dedup_recentes.popleft()
    
    return len(originais), n_dup, originais


class Classificador:
    """Thread que consome fila de noticias e grava scores."""
    
    def __init__(self, db_path, usar_jev, parar_evento):
        self.db_path = db_path
        self.usar_jev = usar_jev
        self.parar = parar_evento
        self.fila = queue.Queue(maxsize=500)
        self.finbert_pt = None
        self.finbert_en = None
        self.tokenizer_pt = None
        self.tokenizer_en = None
        self.jev_client = None
        self.versao_pt = "local"
        self.versao_en = "local"
        self.aviso_jev = False
        
    def carregar_modelos(self):
        """Carrega FinBERT-PT-BR e FinBERT-EN. Valida com frases de teste."""
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        
        torch.set_num_threads(2)
        
        print("Carregando FinBERT-PT-BR...", flush=True)
        self.tokenizer_pt = AutoTokenizer.from_pretrained("lucas-leme/FinBERT-PT-BR")
        self.finbert_pt = AutoModelForSequenceClassification.from_pretrained("lucas-leme/FinBERT-PT-BR")
        self.finbert_pt.eval()
        
        try:
            from huggingface_hub import model_info
            info = model_info("lucas-leme/FinBERT-PT-BR")
            self.versao_pt = info.sha[:8]
        except:
            pass
        
        print("Carregando ProsusAI/finbert...", flush=True)
        self.tokenizer_en = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        self.finbert_en = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        self.finbert_en.eval()
        
        try:
            from huggingface_hub import model_info
            info = model_info("ProsusAI/finbert")
            self.versao_en = info.sha[:8]
        except:
            pass
        
        self._validar_finbert()
        
        if self.usar_jev:
            self._carregar_jev()
    
    def _validar_finbert(self):
        """Valida FinBERT-PT-BR com frases de teste."""
        import torch
        
        testes = [
            ("Hoje a bolsa caiu", "neg"),
            ("Hoje a bolsa subiu", "pos"),
        ]
        
        for frase, esperado in testes:
            inputs = self.tokenizer_pt(frase, return_tensors="pt", truncation=True, max_length=256)
            with torch.inference_mode():
                outputs = self.finbert_pt(**inputs)
                logits = outputs.logits[0]
                probs = torch.softmax(logits, dim=0).numpy()
            
            id2label = self.finbert_pt.config.id2label
            p_pos = probs[next(i for i, l in id2label.items() if "POSITIVE" in l.upper())]
            p_neg = probs[next(i for i, l in id2label.items() if "NEGATIVE" in l.upper())]
            
            if esperado == "neg" and p_neg <= p_pos:
                raise SystemExit(f"Validacao do FinBERT-PT-BR falhou: '{frase}' deveria ser negativa")
            if esperado == "pos" and p_pos <= p_neg:
                raise SystemExit(f"Validacao do FinBERT-PT-BR falhou: '{frase}' deveria ser positiva")
        
        print("FinBERT-PT-BR validado com sucesso", flush=True)
    
    def _carregar_jev(self):
        """Carrega TypeSafe Jev se a chave estiver presente."""
        api_key = os.getenv("TYPESAFE_API_KEY")
        if not api_key:
            if not self.aviso_jev:
                print("aviso: TYPESAFE_API_KEY nao definida; Jev desabilitado", flush=True)
                self.aviso_jev = True
            return
        
        try:
            from typesafe_sdk import TypeSafeClient
            self.jev_client = TypeSafeClient(api_key=api_key)
            print("TypeSafe Jev carregado (modelo jev-1.13.0)", flush=True)
        except ImportError:
            if not self.aviso_jev:
                print("aviso: typesafe-sdk nao instalado; Jev desabilitado", flush=True)
                self.aviso_jev = True
        except Exception as e:
            if not self.aviso_jev:
                print(f"aviso: erro ao carregar Jev ({e}); desabilitado", flush=True)
                self.aviso_jev = True
    
    def classificar_finbert(self, noticias, idioma):
        """Classifica lote com FinBERT. Retorna lista de scores."""
        import torch
        
        modelo = self.finbert_pt if idioma == "pt" else self.finbert_en
        tokenizer = self.tokenizer_pt if idioma == "pt" else self.tokenizer_en
        versao = self.versao_pt if idioma == "pt" else self.versao_en
        
        textos = [f"{n['titulo']}. {n['resumo'][:300]}" for n in noticias]
        inputs = tokenizer(textos, return_tensors="pt", truncation=True, max_length=256, padding=True)
        
        with torch.inference_mode():
            outputs = modelo(**inputs)
            logits = outputs.logits
            probs = torch.softmax(logits, dim=1).numpy()
        
        id2label = modelo.config.id2label
        idx_pos = next(i for i, l in id2label.items() if "POSITIVE" in l.upper())
        idx_neg = next(i for i, l in id2label.items() if "NEGATIVE" in l.upper())
        idx_neu = next(i for i, l in id2label.items() if "NEUTRAL" in l.upper())
        
        scores = []
        for i, n in enumerate(noticias):
            p = probs[i]
            p_pos, p_neg, p_neu = float(p[idx_pos]), float(p[idx_neg]), float(p[idx_neu])
            sentimento = p_pos - p_neg
            conf = max(p_pos, p_neg, p_neu)
            
            relevancia, tema = self._filtro_relevancia(n["titulo"], n["resumo"], n["fonte"])
            
            scores.append({
                "id": n["id"],
                "modelo": "finbert",
                "versao": versao,
                "sentimento": sentimento,
                "p_pos": p_pos,
                "p_neg": p_neg,
                "p_neu": p_neu,
                "conf": conf,
                "relevancia": relevancia,
                "tema": tema,
                "surpresa": None,
                "extra": json.dumps({"p_pos": p_pos, "p_neg": p_neg, "p_neu": p_neu}),
            })
        
        return scores
    
    def _filtro_relevancia(self, titulo, resumo, fonte):
        """Filtro de palavras-chave para relevancia e tema."""
        if fonte.startswith("bcb_") or fonte == "fed_press":
            return 1.0, "oficial"
        
        texto = (titulo + " " + (resumo or "")).lower()
        texto_norm = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
        
        TERMOS = {
            "copom_selic": ["copom", "selic", "banco central", "galipolo", "juros basicos", "politica monetaria"],
            "fiscal_br": ["fiscal", "arcabouco", "meta fiscal", "divida publica", "haddad", "orcamento", "tesouro nacional", "ldo", "loa"],
            "inflacao_br": ["ipca", "ipca-15", "igp-m", "inflacao", "focus", "expectativas"],
            "atividade_br": ["pib", "caged", "desemprego", "varejo", "producao industrial", "ibc-br"],
            "politica_br": ["eleicao", "2o turno", "congresso", "stf", "planalto", "lula"],
            "fed_eua": ["fed", "federal reserve", "fomc", "powell", "treasury", "treasuries", "payroll", "cpi"],
            "cambio": ["dolar", "cambio", "real", "ptax", "dxy", "dollar"],
            "commodities": ["petroleo", "brent", "minerio", "oil", "iron ore"],
        }
        
        for tema, termos in TERMOS.items():
            for termo in termos:
                if termo in texto_norm:
                    return 1.0, tema
        
        return 0.2, None
    
    def classificar_jev(self, noticias):
        """Classifica lote com Jev. Retorna lista de scores."""
        if not self.jev_client:
            return []
        
        from typesafe_sdk import Choice, Noul, Score
        
        QUESTIONS = {
            "relevant": Noul(instructions=(
                "The headline/summary is about Brazilian interest rates, the Brazilian real exchange rate, "
                "the Brazilian stock market index, Brazilian fiscal policy or inflation, the Brazilian central bank (Copom), "
                "or about the US Federal Reserve, US Treasury yields or the US dollar index.")),
            "topic": Choice(instructions="Main topic of the news item", criteria={
                "copom_selic": "Brazilian central bank, Copom, Selic rate decisions or signals",
                "fiscal_br": "Brazilian government budget, fiscal rules, public debt, spending, taxes",
                "inflacao_br": "Brazilian inflation data or expectations (IPCA, IGP-M, Focus survey)",
                "atividade_br": "Brazilian GDP, jobs, retail, industry, activity data",
                "politica_br": "Brazilian politics, elections, Congress, courts",
                "fed_eua": "US Federal Reserve, US rates, Treasuries, US macro data",
                "cambio": "Exchange rates, Brazilian real, US dollar",
                "commodities": "Oil, iron ore, agricultural commodities",
                "empresa": "A specific company",
                "outro": "Anything else",
            }),
            "br_assets": Score(
                instructions=("Likely effect of this news on Brazilian risk assets today "
                              "(Brazilian stocks up, real stronger, local rates lower = positive)"),
                criteria=["Clearly negative", "Somewhat negative", "Neutral or unclear",
                          "Somewhat positive", "Clearly positive"]),
            "surprise": Score(
                instructions="How new or unexpected this information is for financial markets",
                criteria=["Routine or already known", "Somewhat new", "Clearly new or surprising"]),
        }
        
        scores = []
        for n in noticias:
            try:
                state = {
                    "source": n["fonte"],
                    "language": n["idioma"],
                    "headline": n["titulo"],
                    "summary": n["resumo"][:600] if n["resumo"] else "",
                }
                
                response = self.jev_client.decide(
                    model="jev-1.13.0",
                    state=state,
                    questions=QUESTIONS
                )
                
                answers = response.answers
                relevancia = answers["relevant"].noul
                tema = answers["topic"].choice
                
                br_levels = answers["br_assets"].distribution
                ev_br = sum(i * p for i, p in enumerate(br_levels))
                sentimento = (ev_br - 2) / 2
                
                surp_levels = answers["surprise"].distribution
                surpresa = sum(i * p for i, p in enumerate(surp_levels)) / 2
                
                conf = answers["br_assets"].confidence
                
                scores.append({
                    "id": n["id"],
                    "modelo": "jev-1.13.0",
                    "versao": response.model,
                    "sentimento": sentimento,
                    "p_pos": None,
                    "p_neg": None,
                    "p_neu": None,
                    "conf": conf,
                    "relevancia": relevancia,
                    "tema": tema,
                    "surpresa": surpresa,
                    "extra": json.dumps({
                        "br_assets": br_levels,
                        "surprise": surp_levels,
                        "usage": response.usage._asdict() if hasattr(response, "usage") else None,
                    }),
                })
            except Exception as e:
                scores.append({
                    "id": n["id"],
                    "modelo": "jev-1.13.0",
                    "versao": "erro",
                    "sentimento": None,
                    "p_pos": None,
                    "p_neg": None,
                    "p_neu": None,
                    "conf": None,
                    "relevancia": None,
                    "tema": None,
                    "surpresa": None,
                    "extra": json.dumps({"erro": str(e)}),
                })
        
        return scores
    
    def processar_lote(self):
        """Processa um lote da fila."""
        lote = []
        try:
            while len(lote) < 16:
                item = self.fila.get(timeout=0.1)
                lote.append(item)
        except queue.Empty:
            pass
        
        if not lote:
            return
        
        pt = [n for n in lote if n["idioma"] == "pt"]
        en = [n for n in lote if n["idioma"] == "en"]
        
        scores = []
        if pt:
            scores.extend(self.classificar_finbert(pt, "pt"))
        if en:
            scores.extend(self.classificar_finbert(en, "en"))
        
        if self.usar_jev and self.jev_client:
            scores.extend(self.classificar_jev(lote))
        
        if scores:
            db = banco.abrir(self.db_path, SCHEMA)
            ts_score = int(local_br_ms())
            linhas = [(
                s["id"], s["modelo"], s["versao"], ts_score, s["sentimento"],
                s["p_pos"], s["p_neg"], s["p_neu"], s["conf"], s["relevancia"],
                s["tema"], s["surpresa"], s["extra"]
            ) for s in scores]
            db.executemany(
                "INSERT OR REPLACE INTO noticias_score VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                linhas
            )
            db.commit()
            db.close()
    
    def run(self):
        """Thread principal."""
        self.carregar_modelos()
        ultimo_processo = time.time()
        
        while not self.parar.is_set():
            if self.fila.qsize() >= 16 or (self.fila.qsize() > 0 and time.time() - ultimo_processo >= 5):
                self.processar_lote()
                ultimo_processo = time.time()
            time.sleep(0.5)
        
        while not self.fila.empty():
            self.processar_lote()


class GdeltWorker:
    """Thread que coleta GDELT a cada 5 min."""
    
    def __init__(self, db_path, parar_evento):
        self.db_path = db_path
        self.parar = parar_evento
        self.consultas = {
            "br_juros": '(Copom OR Selic OR "Banco Central" OR "interest rate") sourcelang:portuguese',
            "br_fiscal": '("fiscal" OR "arcabouco" OR "Haddad" OR "budget") sourcecountry:BR',
            "us_fed": '("Federal Reserve" OR FOMC OR Powell OR "Treasury yields")',
        }
    
    def buscar_gdelt(self, consulta, query):
        """Busca GDELT timelinevol e timelinetone."""
        resultados = {}
        for mode in ("timelinevol", "timelinetone"):
            url = "https://api.gdeltproject.org/api/v2/doc/doc"
            params = {
                "query": query,
                "mode": mode,
                "timespan": "1h",
                "format": "json",
            }
            try:
                r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT_GDELT)
                r.raise_for_status()
                data = r.json()
                if "timeline" in data and data["timeline"]:
                    timeline = data["timeline"][0].get("data", [])
                    if timeline:
                        ultimo = timeline[-1]
                        if mode == "timelinevol":
                            resultados["volume"] = ultimo.get("value")
                        else:
                            resultados["tom"] = ultimo.get("value")
                        resultados["ts"] = ultimo.get("date")
                time.sleep(6)
            except Exception as e:
                print(f"aviso: GDELT {consulta} {mode} falhou ({e})", flush=True)
        
        return resultados if "volume" in resultados or "tom" in resultados else None
    
    def run(self):
        """Thread principal."""
        db = banco.abrir(self.db_path, SCHEMA)
        
        while not self.parar.is_set():
            for consulta, query in self.consultas.items():
                if self.parar.is_set():
                    break
                
                resultado = self.buscar_gdelt(consulta, query)
                if resultado:
                    try:
                        ts_ponto = datetime.strptime(resultado["ts"], "%Y%m%d%H%M%S")
                        ts_ponto_br = ts_ponto - timedelta(hours=3)
                        ts_ms = int(ts_ponto_br.replace(tzinfo=timezone.utc).timestamp() * 1000)
                        
                        db.execute(
                            "INSERT OR REPLACE INTO gdelt_timeline VALUES (?,?,?,?,?)",
                            (ts_ms, consulta, resultado.get("volume"), resultado.get("tom"), int(local_br_ms()))
                        )
                        db.commit()
                    except Exception as e:
                        print(f"aviso: erro ao gravar GDELT {consulta} ({e})", flush=True)
            
            for _ in range(60):
                if self.parar.is_set():
                    break
                time.sleep(5)
        
        db.close()


def agregar(db, ate_ms, modelos):
    """Agrega noticias_1min e noticias_5min dos ultimos 70 minutos."""
    inicio_ms = ate_ms - 70 * 60 * 1000
    fim_ms = (ate_ms // 60000) * 60000
    
    cursor = db.execute(
        """SELECT n.ts_coleta_ms, n.fonte, n.idioma, n.dup_de, s.modelo, s.sentimento, s.conf, s.relevancia, s.surpresa
           FROM noticias n
           LEFT JOIN noticias_score s ON n.id = s.id
           WHERE n.ts_coleta_ms >= ? AND n.ts_coleta_ms < ?""",
        (inicio_ms, fim_ms)
    )
    
    linhas = cursor.fetchall()
    if not linhas:
        return
    
    eventos_cursor = db.execute(
        "SELECT ts_ms FROM calendario_macro WHERE ts_ms BETWEEN ? AND ?",
        (inicio_ms - 5 * 60 * 1000, fim_ms + 60 * 60 * 1000)
    )
    eventos = {row[0] for row in eventos_cursor}
    
    def em_janela_evento(ts_min):
        for ev_ms in eventos:
            if ev_ms - 5 * 60 * 1000 <= ts_min < ev_ms + 60 * 60 * 1000:
                return True
        return False
    
    for modelo in modelos:
        escopos = {
            "br": lambda fonte, idioma: idioma == "pt" and not fonte.startswith("bcb_") and fonte != "fed_press",
            "global": lambda fonte, idioma: idioma == "en" and fonte != "fed_press",
            "oficial": lambda fonte, idioma: fonte.startswith("bcb_") or fonte == "fed_press",
            "todas": lambda fonte, idioma: True,
        }
        
        for escopo, filtro in escopos.items():
            agg_1min = {}
            agg_5min = {}
            
            for row in linhas:
                ts_coleta, fonte, idioma, dup_de, modelo_score, sent, conf, relev, surp = row
                
                if not filtro(fonte, idioma):
                    continue
                if modelo_score != modelo and modelo_score is not None:
                    continue
                if dup_de is not None:
                    continue
                
                ts_min = (ts_coleta // 60000) * 60000
                ts_5min = (ts_coleta // (5 * 60000)) * (5 * 60000)
                
                if ts_min not in agg_1min:
                    agg_1min[ts_min] = {"n": 0, "n_rel": 0, "n_neg": 0, "n_pos": 0, "sents": [], "surps": []}
                
                agg_1min[ts_min]["n"] += 1
                
                if relev is not None and relev >= 0.5 and sent is not None:
                    agg_1min[ts_min]["n_rel"] += 1
                    peso = conf * relev if conf else relev
                    agg_1min[ts_min]["sents"].append((sent, peso))
                    if sent <= -0.3:
                        agg_1min[ts_min]["n_neg"] += 1
                    elif sent >= 0.3:
                        agg_1min[ts_min]["n_pos"] += 1
                    if surp is not None:
                        agg_1min[ts_min]["surps"].append(surp)
                
                if ts_5min not in agg_5min:
                    agg_5min[ts_5min] = {"n": 0, "n_rel": 0, "sents": [], "surps": []}
                
                agg_5min[ts_5min]["n"] += 1
                if relev is not None and relev >= 0.5 and sent is not None:
                    agg_5min[ts_5min]["n_rel"] += 1
                    agg_5min[ts_5min]["sents"].append((sent, conf * relev if conf else relev))
                    if surp is not None:
                        agg_5min[ts_5min]["surps"].append(surp)
            
            linhas_1min = []
            for ts_min, agg in agg_1min.items():
                sent_medio = None
                sent_min = None
                frac_neg = None
                surp_max = None
                
                if agg["sents"]:
                    total_peso = sum(p for _, p in agg["sents"])
                    if total_peso > 0:
                        sent_medio = sum(s * p for s, p in agg["sents"]) / total_peso
                        sent_min = min(s for s, _ in agg["sents"])
                    if agg["n_rel"] > 0:
                        frac_neg = agg["n_neg"] / agg["n_rel"]
                
                if agg["surps"]:
                    surp_max = max(agg["surps"])
                
                evento = 1 if em_janela_evento(ts_min) else 0
                
                linhas_1min.append((
                    ts_min, escopo, modelo, agg["n"], agg["n_rel"],
                    agg["n_neg"], agg["n_pos"], sent_medio, sent_min,
                    frac_neg, surp_max, evento
                ))
            
            if linhas_1min:
                db.executemany(
                    "INSERT OR REPLACE INTO noticias_1min VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    linhas_1min
                )
            
            linhas_5min = []
            for ts_5min, agg in agg_5min.items():
                sent_medio = None
                sent_min = None
                frac_neg = None
                surp_max = None
                n_60 = None
                sent_60 = None
                z_volume = None
                pico = 0
                
                if agg["sents"]:
                    total_peso = sum(p for _, p in agg["sents"])
                    if total_peso > 0:
                        sent_medio = sum(s * p for s, p in agg["sents"]) / total_peso
                        sent_min = min(s for s, _ in agg["sents"])
                    if agg["n_rel"] > 0:
                        frac_neg = sum(1 for s, _ in agg["sents"] if s <= -0.3) / agg["n_rel"]
                
                if agg["surps"]:
                    surp_max = max(agg["surps"])
                
                janela_60 = ts_5min - 60 * 60 * 1000
                cursor_60 = db.execute(
                    """SELECT n.ts_coleta_ms, s.sentimento, s.conf, s.relevancia
                       FROM noticias n
                       LEFT JOIN noticias_score s ON n.id = s.id
                       WHERE n.ts_coleta_ms >= ? AND n.ts_coleta_ms < ? AND n.dup_de IS NULL AND s.modelo = ?""",
                    (janela_60, ts_5min, modelo)
                )
                
                sents_60 = []
                for row in cursor_60:
                    ts_n, sent, conf, relev = row
                    if relev is not None and relev >= 0.5 and sent is not None:
                        idade_min = (ts_5min - ts_n) / 60000
                        peso_tempo = 0.5 ** (idade_min / 30)
                        peso_total = peso_tempo * (conf * relev if conf else relev)
                        sents_60.append((sent, peso_total))
                
                if sents_60:
                    n_60 = len(sents_60)
                    total_peso = sum(p for _, p in sents_60)
                    if total_peso > 0:
                        sent_60 = sum(s * p for s, p in sents_60) / total_peso
                
                slot_5min = ts_5min % (24 * 3600 * 1000) // (5 * 60 * 1000)
                dow = (ts_5min // (24 * 3600 * 1000) + 3) % 7
                
                if dow < 5:
                    janela_20d = ts_5min - 20 * 24 * 3600 * 1000
                    cursor_hist = db.execute(
                        """SELECT ts_ms, n_relevantes FROM noticias_5min
                           WHERE escopo = ? AND modelo = ? AND ts_ms >= ? AND ts_ms < ?""",
                        (escopo, modelo, janela_20d, ts_5min)
                    )
                    
                    hist_slot = []
                    for row in cursor_hist:
                        ts_h, n_rel_h = row
                        dow_h = (ts_h // (24 * 3600 * 1000) + 3) % 7
                        slot_h = ts_h % (24 * 3600 * 1000) // (5 * 60 * 1000)
                        if dow_h < 5 and slot_h == slot_5min:
                            hist_slot.append(n_rel_h)
                    
                    if len(hist_slot) >= 5:
                        media = np.mean(hist_slot)
                        desvio = np.std(hist_slot)
                        if desvio > 0:
                            z_volume = (agg["n_rel"] - media) / desvio
                            if z_volume >= 3:
                                pico = 1
                
                evento = 1 if em_janela_evento(ts_5min) else 0
                
                linhas_5min.append((
                    ts_5min, escopo, modelo, agg["n"], agg["n_rel"],
                    sent_medio, sent_min, frac_neg, surp_max,
                    n_60, sent_60, z_volume, pico, evento
                ))
            
            if linhas_5min:
                db.executemany(
                    "INSERT OR REPLACE INTO noticias_5min VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    linhas_5min
                )
    
    db.commit()


def carregar_calendario(db, csv_path):
    """Carrega calendario_macro.csv com INSERT OR REPLACE."""
    import csv
    
    if not csv_path.exists():
        return
    
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        linhas = []
        for row in reader:
            try:
                dt = datetime.fromisoformat(row["data_hora_brasilia"])
                ts_ms = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
                linhas.append((
                    ts_ms,
                    row["tipo"],
                    row["pais"],
                    row.get("descricao", ""),
                    "manual"
                ))
            except:
                pass
        
        if linhas:
            db.executemany(
                "INSERT OR REPLACE INTO calendario_macro VALUES (?,?,?,?,?)",
                linhas
            )
            db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="NOTICIAS")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--jev", action="store_true")
    ap.add_argument("--sem-gdelt", action="store_true")
    ap.add_argument("--sem-classificar", action="store_true")
    ap.add_argument("--fontes", help="filtro de fontes separadas por virgula")
    args = ap.parse_args()
    
    fontes = [f for f in FONTES if f["ativo"]]
    if args.fontes:
        ids = set(args.fontes.split(","))
        fontes = [f for f in fontes if f["id"] in ids]
    
    finnhub_key = os.getenv("FINNHUB_API_KEY")
    if finnhub_key:
        fontes.extend([
            {"id": "finnhub_general", "url": "finnhub", "tipo": "finnhub", "idioma": "en", "intervalo_s": 60, "ativo": True, "categoria": "general"},
            {"id": "finnhub_forex", "url": "finnhub", "tipo": "finnhub", "idioma": "en", "intervalo_s": 60, "ativo": True, "categoria": "forex"},
        ])
    else:
        print("aviso: FINNHUB_API_KEY nao definida; Finnhub desabilitado", flush=True)
    
    db = banco.abrir(args.db, SCHEMA)
    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    csv_calendario = Path(args.db).parent / "calendario_macro.csv"
    carregar_calendario(db, csv_calendario)
    
    cursor = db.execute("SELECT fonte, etag, last_modified, ultimo_ok_ms, falhas_seguidas, ultimo_id FROM noticias_estado")
    estado_fontes = {row[0]: {
        "etag": row[1],
        "last_modified": row[2],
        "ultimo_ok_ms": row[3],
        "falhas_seguidas": row[4],
        "ultimo_id": row[5],
    } for row in cursor}
    
    dedup_recentes = deque(maxlen=1000)
    janela_2h = int(local_br_ms()) - 2 * 3600 * 1000
    cursor = db.execute(
        "SELECT id, titulo, ts_coleta_ms FROM noticias WHERE ts_coleta_ms >= ? ORDER BY ts_coleta_ms",
        (janela_2h,)
    )
    for row in cursor:
        dedup_recentes.append({
            "id": row[0],
            "titulo_norm": norm_titulo(row[1]),
            "ts_coleta_ms": row[2],
        })
    
    parar_evento = threading.Event()
    threads = []
    
    if not args.sem_classificar:
        classificador = Classificador(args.db, args.jev, parar_evento)
        t = threading.Thread(target=classificador.run, daemon=True)
        t.start()
        threads.append(t)
    
    if not args.sem_gdelt:
        gdelt_worker = GdeltWorker(args.db, parar_evento)
        t = threading.Thread(target=gdelt_worker.run, daemon=True)
        t.start()
        threads.append(t)
    
    print(f"Coletando {len(fontes)} fontes -> {args.db} (sessao {sessao}). Ctrl+C para parar.", flush=True)
    
    ultimo_fonte = {f["id"]: 0 for f in fontes}
    ultimo_minuto = 0
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    
    try:
        partida = True
        while time.time() < fim:
            agora = time.time()
            ts_coleta_ms = int(local_br_ms())
            
            for i, fonte in enumerate(fontes):
                if partida:
                    escalonamento = i * 60 / len(fontes)
                    if agora - (fim - args.segundos) < escalonamento:
                        continue
                
                estado = estado_fontes.get(fonte["id"], {})
                
                falhas = estado.get("falhas_seguidas", 0)
                if falhas >= 3:
                    pausa = min(30 * 60, 60 * (2 ** (falhas - 3)))
                    ultimo_ok = estado.get("ultimo_ok_ms", 0)
                    if ts_coleta_ms - ultimo_ok < pausa * 1000:
                        continue
                
                if agora - ultimo_fonte[fonte["id"]] < fonte["intervalo_s"]:
                    continue
                
                ultimo_fonte[fonte["id"]] = agora
                
                if fonte["tipo"] == "finnhub":
                    itens, novo_estado = buscar_finnhub(fonte["categoria"], estado, finnhub_key)
                else:
                    itens, novo_estado = buscar(fonte, estado)
                
                estado_fontes[fonte["id"]] = novo_estado
                
                if itens:
                    noticias_novas = []
                    for item in itens:
                        noticias_novas.append({
                            "titulo": item["titulo"],
                            "url": item["url"],
                            "resumo": item.get("resumo"),
                            "ts_pub_ms": item.get("ts_pub_ms"),
                            "fonte": fonte["id"],
                            "idioma": fonte["idioma"],
                            "evento": None,
                        })
                    
                    n_novas, n_dup, originais = gravar_noticias(db, noticias_novas, sessao, ts_coleta_ms, dedup_recentes)
                    
                    if not args.sem_classificar and originais:
                        for orig in originais:
                            try:
                                classificador.fila.put(orig, timeout=1)
                            except queue.Full:
                                pass
                    
                    if n_novas > 0 or n_dup > 0:
                        print(f"[{datetime.now():%H:%M:%S}] {fonte['id']:24} novas={n_novas} dup={n_dup}", flush=True)
            
            if partida:
                partida = False
            
            minuto_atual = ts_coleta_ms // 60000
            if minuto_atual > ultimo_minuto:
                ultimo_minuto = minuto_atual
                
                modelos = ["finbert"]
                if args.jev:
                    modelos.append("jev-1.13.0")
                
                agregar(db, ts_coleta_ms, modelos)
                
                fila_size = classificador.fila.qsize() if not args.sem_classificar else 0
                falhas_str = ", ".join(f"{fonte}({estado_fontes[fonte]['falhas_seguidas']})"
                                        for fonte in estado_fontes
                                        if estado_fontes[fonte].get("falhas_seguidas", 0) > 0)
                
                cursor = db.execute(
                    "SELECT COUNT(*) FROM noticias WHERE ts_coleta_ms >= ?",
                    (ts_coleta_ms - 60 * 60 * 1000,)
                )
                n_60 = cursor.fetchone()[0]
                
                cursor = db.execute(
                    """SELECT AVG(sentimento) FROM noticias n
                       JOIN noticias_score s ON n.id = s.id
                       WHERE n.ts_coleta_ms >= ? AND n.dup_de IS NULL AND s.modelo = 'finbert'
                         AND s.relevancia >= 0.5""",
                    (ts_coleta_ms - 60 * 60 * 1000,)
                )
                sent_60 = cursor.fetchone()[0]
                
                print(f"[{datetime.now():%H:%M}] n60={n_60} sent60={sent_60:.2f if sent_60 else 0:.2f} fila={fila_size} | "
                      f"falhas: {falhas_str if falhas_str else 'nenhuma'}", flush=True)
            
            for fonte_id, novo_estado in estado_fontes.items():
                db.execute(
                    "INSERT OR REPLACE INTO noticias_estado VALUES (?,?,?,?,?,?)",
                    (fonte_id, novo_estado.get("etag"), novo_estado.get("last_modified"),
                     novo_estado.get("ultimo_ok_ms"), novo_estado.get("falhas_seguidas", 0),
                     novo_estado.get("ultimo_id"))
                )
            db.commit()
            
            time.sleep(1)
    
    except KeyboardInterrupt:
        print("\nParando...", flush=True)
    
    finally:
        parar_evento.set()
        for t in threads:
            t.join(timeout=10)
        
        for fonte_id, novo_estado in estado_fontes.items():
            db.execute(
                "INSERT OR REPLACE INTO noticias_estado VALUES (?,?,?,?,?,?)",
                (fonte_id, novo_estado.get("etag"), novo_estado.get("last_modified"),
                 novo_estado.get("ultimo_ok_ms"), novo_estado.get("falhas_seguidas", 0),
                 novo_estado.get("ultimo_id"))
            )
        db.commit()
        db.close()
        
        print("Coleta de noticias encerrada", flush=True)


if __name__ == "__main__":
    main()
