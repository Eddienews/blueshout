#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Blueshout backend (Flask)
- /api/login    : cria sessão Bluesky (handle + app password) e carrega follows
- /api/me       : retorna handle/did logados
- /api/logout   : encerra sessão
- /api/follows  : devolve contagem de seguidos
- /api/feed     : lê topo do timeline; query:
                  ?init=1           -> aquece cache de vistos e retorna só init_count
                  ?only_followed=1  -> filtra apenas autores seguidos
                  ?limit=40         -> itens do topo (padrão 40)
- /api/health   : ok simples
- /api/tts      : TTS via Piper (POST {text, lang?})
- /api/tts_caps : capacidades TTS do servidor (vozes Piper disponíveis)

Também serve / (index.html) e /static/*

Importante:
- Para manter o cookie leve, nada de listas grandes na sessão. Usamos um
  cache em memória do servidor (MEM) indexado pelo "sid" da sessão Flask.
"""

from __future__ import annotations
import io
import os
import time
import json
import secrets
import tempfile
import subprocess
from collections import deque
from threading import BoundedSemaphore
from typing import Dict, Any, List, Set, Tuple, Optional

import requests
from flask import (
    Flask, request, jsonify, session, abort,
    send_from_directory, send_file, make_response
)

# =============================================================================
# App
# =============================================================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    static_url_path="/static",
)

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

def _env_int(name: str, default: int, min_value: int = 1) -> int:
    try:
        return max(min_value, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default

def _secret_key() -> str:
    secret = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY")
    if secret:
        return secret
    if _env_bool("BLUESHOUT_ALLOW_INSECURE_DEV_SECRET", False):
        return secrets.token_urlsafe(32)
    raise RuntimeError("Set SECRET_KEY (or FLASK_SECRET_KEY) before starting Blueshout.")

app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.environ.get("SESSION_COOKIE_SAMESITE", "Lax"),
    SESSION_COOKIE_SECURE=_env_bool("SESSION_COOKIE_SECURE", True),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12 # 12h
)

BSKY_XRPC = "https://bsky.social/xrpc"
REQ_TIMEOUT = 15
TTS_RATE_LIMIT = _env_int("TTS_RATE_LIMIT", 30)
TTS_RATE_WINDOW = _env_int("TTS_RATE_WINDOW", 60)
TTS_MAX_CONCURRENT = _env_int("TTS_MAX_CONCURRENT", 2)
TTS_SEMAPHORE = BoundedSemaphore(TTS_MAX_CONCURRENT)

# =============================================================================
# Memória do servidor (evita cookie gigante)
# =============================================================================
MEM: Dict[str, Dict[str, Any]] = {}
# estrutura por sid:
# {
#   "auth": {...},               # tokens Bluesky ficam apenas no servidor
#   "seen": {"timeline": set(), "lists": set(), "search": set()},
#   "follows": set[str],         # DIDs que o usuário segue
#   "tts_hits": deque[float],
#   "ts_login": int,
# }
SEEN_MAX = 2000  # limite para não crescer infinito

def _get_sid() -> Optional[str]:
    return session.get("sid")

def _require_mem() -> Dict[str, Any]:
    sid = _get_sid()
    if not sid or sid not in MEM:
        abort(401, description="Sessão expirada.")
    return MEM[sid]

@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    return resp

# =============================================================================
# Helpers de sessão/autenticação (Bluesky)
# =============================================================================

def _require_auth() -> Dict[str, Any]:
    mem = _require_mem()
    auth = mem.get("auth")
    if not auth or not auth.get("accessJwt"):
        abort(401, description="Não autenticado")
    return auth

def _create_session(handle: str, app_password: str) -> Dict[str, Any]:
    r = requests.post(
        f"{BSKY_XRPC}/com.atproto.server.createSession",
        json={"identifier": handle, "password": app_password},
        timeout=REQ_TIMEOUT,
    )
    if not r.ok:
        abort(r.status_code, r.text)
    return r.json()

def _refresh_session(refresh_jwt: str) -> Dict[str, Any]:
    r = requests.post(
        f"{BSKY_XRPC}/com.atproto.server.refreshSession",
        headers={"Authorization": f"Bearer {refresh_jwt}"},
        timeout=REQ_TIMEOUT,
    )
    if not r.ok:
        abort(r.status_code, r.text)
    return r.json()

def _get_timeline(access_jwt: str, limit: int = 40) -> Dict[str, Any]:
    r = requests.get(
        f"{BSKY_XRPC}/app.bsky.feed.getTimeline",
        params={"limit": str(limit)},
        headers={"Authorization": f"Bearer {access_jwt}"},
        timeout=REQ_TIMEOUT,
    )
    return {"ok": r.ok, "status": r.status_code, "json": (r.json() if r.content else {})}

def _get_follows_page(access_jwt: str, actor: str, cursor: Optional[str] = None) -> Dict[str, Any]:
    params = {"actor": actor, "limit": "100"}
    if cursor:
        params["cursor"] = cursor
    r = requests.get(
        f"{BSKY_XRPC}/app.bsky.graph.getFollows",
        params=params,
        headers={"Authorization": f"Bearer {access_jwt}"},
        timeout=REQ_TIMEOUT,
    )
    if not r.ok:
        abort(r.status_code, r.text)
    return r.json()

def _load_all_follows(access_jwt: str, did: str) -> Set[str]:
    """Carrega todos os DIDs seguidos (paginando)."""
    dids: Set[str] = set()
    cursor = None
    # pagina no máx 30 vezes para segurança (≈ 3000 follows)
    for _ in range(30):
        js = _get_follows_page(access_jwt, did, cursor)
        for ent in js.get("follows") or []:
            d = ent.get("did")
            if d:
                dids.add(d)
        cursor = js.get("cursor")
        if not cursor:
            break
    return dids

def _make_seen_store() -> Dict[str, Set[str]]:
    return {"timeline": set(), "lists": set(), "search": set()}

def _ensure_seen(mem: Dict[str, Any], bucket: str = "timeline") -> Set[str]:
    seen = mem.get("seen")
    if isinstance(seen, set):
        seen = {"timeline": seen, "lists": set(), "search": set()}
        mem["seen"] = seen
    if not isinstance(seen, dict):
        seen = _make_seen_store()
        mem["seen"] = seen
    bucket_seen = seen.get(bucket)
    if not isinstance(bucket_seen, set):
        bucket_seen = set()
        seen[bucket] = bucket_seen
    return bucket_seen

def _trim_seen(seen: Set[str], newest_uris: List[str]) -> None:
    if len(seen) <= SEEN_MAX:
        return
    seen.clear()
    seen.update(newest_uris[-SEEN_MAX:])

def _refresh_auth(auth: Dict[str, Any]) -> Dict[str, Any]:
    ref = _refresh_session(auth["refreshJwt"])
    auth.update({
        "accessJwt": ref.get("accessJwt") or auth.get("accessJwt"),
        "refreshJwt": ref.get("refreshJwt") or auth.get("refreshJwt"),
    })
    return auth

def _check_tts_rate_limit(mem: Dict[str, Any]) -> None:
    now = time.monotonic()
    hits = mem.get("tts_hits")
    if not isinstance(hits, deque):
        hits = deque()
        mem["tts_hits"] = hits
    while hits and now - hits[0] > TTS_RATE_WINDOW:
        hits.popleft()
    if len(hits) >= TTS_RATE_LIMIT:
        abort(429, description="Limite de TTS excedido. Tente novamente em instantes.")
    hits.append(now)

def _parse_items(feed_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extrai (uri, author, did, handle, text, indexedAt) dos rows."""
    items: List[Dict[str, Any]] = []
    for row in feed_rows or []:
        post = row.get("post") or {}
        uri = post.get("uri")
        if not uri:
            continue

        author = post.get("author") or {}
        did = author.get("did")
        handle = author.get("handle")
        display = author.get("displayName") or handle or "—"

        record = post.get("record") or {}
        text = record.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            continue

        items.append({
            "uri": uri,
            "author": display,       # usado pelo front para falar/exibir
            "did": did,
            "handle": handle,
            "text": text,
            "indexedAt": post.get("indexedAt"),
        })
    return items

    # ======== NOVOS HELPERS BLUESKY (listas e busca) ========

def _get_lists(access_jwt: str, actor: str, cursor: str | None = None) -> Dict[str, Any]:
    """Listas criadas pelo ator (usuário logado)."""
    params = {"actor": actor, "limit": "50"}
    if cursor:
        params["cursor"] = cursor
    r = requests.get(
        f"{BSKY_XRPC}/app.bsky.graph.getLists",
        params=params,
        headers={"Authorization": f"Bearer {access_jwt}"},
        timeout=REQ_TIMEOUT,
    )
    return {"ok": r.ok, "status": r.status_code, "json": (r.json() if r.content else {})}

def _get_list_feed(access_jwt: str, list_uri: str, limit: int = 40) -> Dict[str, Any]:
    params = {"list": list_uri, "limit": str(limit)}
    r = requests.get(
        f"{BSKY_XRPC}/app.bsky.feed.getListFeed",
        params=params,
        headers={"Authorization": f"Bearer {access_jwt}"},
        timeout=REQ_TIMEOUT,
    )
    return {"ok": r.ok, "status": r.status_code, "json": (r.json() if r.content else {})}

def _search_posts(access_jwt: str, q: str, limit: int = 40) -> Dict[str, Any]:
    params = {"q": q, "limit": str(limit)}
    r = requests.get(
        f"{BSKY_XRPC}/app.bsky.feed.searchPosts",
        params=params,
        headers={"Authorization": f"Bearer {access_jwt}"},
        timeout=REQ_TIMEOUT,
    )
    return {"ok": r.ok, "status": r.status_code, "json": (r.json() if r.content else {})}

def _parse_items_with_embed(feed_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Como _parse_items, mas inclui embed (1 imagem => UI mostra aviso)."""
    items: List[Dict[str, Any]] = []
    for row in feed_rows or []:
        post = row.get("post") or row.get("postView") or {}
        uri = post.get("uri")
        if not uri:
            continue

        author = post.get("author") or {}
        did = author.get("did")
        handle = author.get("handle")
        display = author.get("displayName") or handle or "—"

        record = post.get("record") or {}
        text = record.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            continue

        embed = post.get("embed") or {}
        # normaliza embed de imagens para a UI (se vier no formato padrão)
        images = []
        if isinstance(embed, dict) and embed.get("$type", "").endswith("embed.images#view"):
            images = embed.get("images") or []

        items.append({
            "uri": uri,
            "author": display,
            "did": did,
            "handle": handle,
            "text": text,
            "indexedAt": post.get("indexedAt"),
            "embed": {"images": images} if images else {},
        })
    return items


# =============================================================================
# Piper TTS
# =============================================================================

TTS_STRICT_PIPER = _env_bool("TTS_STRICT_PIPER", True)  # True = não cair para EN se falta modelo do idioma pedido
PIPER_BIN = os.environ.get("PIPER_BIN", "/usr/local/bin/piper")
PIPER_MODELS_DIR = os.environ.get("PIPER_MODELS_DIR", "/opt/piper/models")

PIPER_LANG_LABELS = {
    "pt": "Portuguese",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "he": "Hebrew",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "sv": "Swedish",
    "uk": "Ukrainian",
}
PIPER_DEFAULT_MODELS = {
    "pt": "pt_BR-jeff-medium",
    "en": "en_US-amy-medium",
    "es": "es_ES-sharvard-medium",
    "fr": "fr_FR-siwis-medium",
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-paola-medium",
}


def _file_exists(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path)
    except Exception:
        return False


def _lang_base(lang: str) -> str:
    return (lang or "en-US").strip()[:2].lower() or "en"


def _env_voice_overrides() -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for raw in (os.environ.get("PIPER_VOICES") or "").split(","):
        if "=" not in raw:
            continue
        base, model_base = raw.split("=", 1)
        base = _lang_base(base)
        model_base = model_base.strip()
        if base and model_base:
            overrides[base] = model_base.removesuffix(".onnx")

    bases = set(PIPER_LANG_LABELS) | set(PIPER_DEFAULT_MODELS)
    extra_bases = os.environ.get("PIPER_LANGUAGES") or ""
    bases.update(_lang_base(part) for part in extra_bases.split(",") if part.strip())
    for base in bases:
        env_path = os.environ.get(f"PIPER_{base.upper()}")
        if env_path:
            overrides[base] = env_path.strip().removesuffix(".onnx")
    return overrides


def _installed_piper_models() -> Dict[str, List[str]]:
    models: Dict[str, List[str]] = {}
    try:
        filenames = os.listdir(PIPER_MODELS_DIR)
    except OSError:
        return models
    for filename in filenames:
        if not filename.endswith(".onnx"):
            continue
        base = filename[:2].lower()
        if not base.isalpha():
            continue
        model_base = os.path.join(PIPER_MODELS_DIR, filename.removesuffix(".onnx"))
        models.setdefault(base, []).append(model_base)
    for values in models.values():
        values.sort(key=_model_preference_key)
    return models


def _model_preference_key(model_base: str) -> Tuple[int, str]:
    name = os.path.basename(model_base).lower()
    if "-medium" in name:
        rank = 0
    elif "-high" in name:
        rank = 1
    elif "-low" in name:
        rank = 2
    elif "-x_low" in name:
        rank = 3
    else:
        rank = 4
    return rank, name


def _configured_tts_bases() -> List[str]:
    bases = set(PIPER_DEFAULT_MODELS) | set(PIPER_LANG_LABELS)
    bases.update(_env_voice_overrides())
    bases.update(_installed_piper_models())
    for raw in (os.environ.get("PIPER_LANGUAGES") or "").split(","):
        if raw.strip():
            bases.add(_lang_base(raw))
    return sorted(base for base in bases if base)


def _piper_model_base(base: str) -> str:
    base = _lang_base(base)
    overrides = _env_voice_overrides()
    if base in overrides:
        return overrides[base]
    if base in PIPER_DEFAULT_MODELS:
        return os.path.join(PIPER_MODELS_DIR, PIPER_DEFAULT_MODELS[base])
    installed = _installed_piper_models().get(base) or []
    return installed[0] if installed else ""


def _pick_piper_paths(lang: str) -> Tuple[str, str]:
    """
    Escolhe modelo Piper pelo idioma. Suporta:
      - modelos padrão em PIPER_MODELS_DIR;
      - override por PIPER_<BASE>, ex.: PIPER_NL=/opt/piper/models/nl_NL-voice-medium;
      - override em lote por PIPER_VOICES="nl=/path/model,pl=/path/model";
      - descoberta automática de arquivos *.onnx instalados em PIPER_MODELS_DIR.
    Retorna: (onnx_path, cfg_path) – cfg pode não existir (Piper aceita sem -c).
    """
    model_base = _piper_model_base(_lang_base(lang))
    if not model_base:
        return "", ""
    onnx = model_base + ".onnx"
    cfg = model_base + ".onnx.json"
    return onnx, cfg


def _is_native_for_lang(lang: str, onnx_path: str) -> bool:
    """
    Considera 'nativo' quando o nome do modelo contém o prefixo do idioma (pt_, en_, es_...).
    Isso evita dizer 'pt-BR' com voz EN.
    """
    if not onnx_path:
        return False
    base = (lang or "")[:2].lower()
    name = os.path.basename(onnx_path).lower()
    return f"{base}_" in name

def _piper_tts(text: str, lang: str) -> Tuple[bytes, str]:
    """
    Gera WAV com Piper. Retorna (wav_bytes, model_name).
    Lança abort(400/500) em caso de erro ou ausência de modelo nativo quando TTS_STRICT_PIPER=True.
    """
    if not _file_exists(PIPER_BIN):
        abort(500, description="piper não encontrado no servidor.")

    onnx, cfg = _pick_piper_paths(lang or "en-US")
    if not _file_exists(onnx):
        abort(400, description=f"modelo Piper não encontrado para lang={lang!r}")

    if TTS_STRICT_PIPER and not _is_native_for_lang(lang, onnx):
        abort(400, description=f"idioma {lang!r} não suportado nativamente pelo Piper")

    # Piper aceita sem -c se o JSON não existir (não vamos falhar por isso)
    have_cfg = _file_exists(cfg)
    model_name = os.path.basename(onnx).removesuffix(".onnx")

    # Criar WAV temporário
    with tempfile.NamedTemporaryFile(prefix="tts_", suffix=".wav", delete=False) as tf:
        tmp_path = tf.name

    try:
        # Chamando Piper: stdin = texto; saída = arquivo WAV
        cmd = [PIPER_BIN, "-m", onnx, "-f", tmp_path]
        if have_cfg:
            cmd.extend(["-c", cfg])

        proc = subprocess.run(
            cmd,
            input=text.encode("utf-8", errors="ignore"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60
        )
        if proc.returncode != 0:
            # Mensagem comum quando JSON está corrompido: "parse_error..."
            err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
            abort(500, description=f"piper error: {err or 'unknown'}")

        with open(tmp_path, "rb") as f:
            wav = f.read()
        return wav, model_name
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

# =============================================================================
# Routes - páginas estáticas
# =============================================================================

@app.get("/")
def index():
    # Procura index.html na raiz; se não existir, tenta em /templates
    root_candidate = os.path.join(BASE_DIR, "index.html")
    if os.path.isfile(root_candidate):
        return send_from_directory(BASE_DIR, "index.html")
    # fallback: templates/
    return send_from_directory(TEMPLATES_DIR, "index.html")

# =============================================================================
# API - saúde
# =============================================================================

@app.get("/api/health")
def api_health():
    return jsonify({"ok": True})

# =============================================================================
# API - login / sessão
# =============================================================================

@app.post("/api/login")
def api_login():
    data = request.get_json(force=True) or {}
    handle = (data.get("handle") or "").strip()
    app_password = (data.get("app_password") or "").strip()

    if not handle or not app_password:
        abort(400, description="Handle e app_password são obrigatórios.")

    payload = _create_session(handle, app_password)
    did = payload.get("did")

    # cria sid e estrutura em memória
    sid = secrets.token_hex(16)
    auth = {
        "handle": payload.get("handle") or handle,
        "did": did,
        "accessJwt": payload.get("accessJwt"),
        "refreshJwt": payload.get("refreshJwt"),
    }
    session.clear()
    session.permanent = True
    session["sid"] = sid
    session["user"] = {"handle": auth["handle"], "did": did}
    MEM[sid] = {
        "auth": auth,
        "seen": _make_seen_store(),
        "follows": set(),
        "tts_hits": deque(),
        "ts_login": int(time.time()),
    }

    # carrega follows para filtrar o feed
    if did:
        follows = _load_all_follows(auth["accessJwt"], did)
        MEM[sid]["follows"] = follows

    return jsonify({
        "handle": auth["handle"],
        "did": did,
        "follows_count": len(MEM[sid]["follows"]),
    })

@app.get("/api/me")
def api_me():
    sid = _get_sid()
    if not sid or sid not in MEM:
        return jsonify({"handle": None, "did": None})
    user = session.get("user") or {}
    auth = MEM[sid].get("auth") or {}
    return jsonify({
        "handle": user.get("handle") or auth.get("handle"),
        "did": user.get("did") or auth.get("did"),
    })

@app.post("/api/logout")
def api_logout():
    sid = _get_sid()
    if sid and sid in MEM:
        try:
            del MEM[sid]
        except KeyError:
            pass
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/follows")
def api_follows():
    _require_auth()
    mem = _require_mem()
    return jsonify({"count": len(mem.get("follows") or set())})

# =============================================================================
# API - feed
# =============================================================================

@app.get("/api/feed")
def api_feed():
    """
    - GET /api/feed?init=1: aquece cache 'seen' e retorna apenas init_count
    - GET /api/feed: retorna itens novos (não vistos) + atualiza cache
    Opções:
      ?only_followed=1  -> filtra só autores seguidos
      ?limit=40
    """
    auth = _require_auth()
    mem = _require_mem()

    init = request.args.get("init") == "1"
    only_followed = request.args.get("only_followed") in ("1", "true", "True")
    limit = request.args.get("limit", "40")
    try:
        limit_i = max(1, min(100, int(limit)))
    except ValueError:
        abort(400, description="limit inválido")

    # busca timeline; se 401, tenta refresh
    tl = _get_timeline(auth["accessJwt"], limit=limit_i)
    if tl["status"] == 401:
        _refresh_auth(auth)
        tl = _get_timeline(auth["accessJwt"], limit=limit_i)

    if not tl["ok"]:
        abort(tl["status"], str(tl["json"] or ""))

    feed_rows = (tl["json"] or {}).get("feed") or []

    # coleta URIs atuais para cache "seen"
    current_uris: List[str] = []
    for row in feed_rows:
        post = row.get("post") or {}
        uri = post.get("uri")
        if uri:
            current_uris.append(uri)

    init_count = len(current_uris)
    seen: Set[str] = _ensure_seen(mem, "timeline")

    # init: só aquece cache
    if init:
        seen.update(current_uris)
        _trim_seen(seen, current_uris)
        return jsonify({"init_count": init_count, "items": []})

    # fora do init, filtra novos e monta itens
    new_rows: List[Dict[str, Any]] = []
    for row in feed_rows:
        post = row.get("post") or {}
        uri = post.get("uri")
        if not uri or uri in seen:
            continue

        if only_followed:
            author = post.get("author") or {}
            did = author.get("did")
            if did and did not in (mem.get("follows") or set()):
                continue

        new_rows.append(row)

    items = _parse_items(new_rows)

    # ordem: mais antigos primeiro (para leitura natural)
    items.reverse()

    # atualiza cache de vistos
    seen.update(current_uris)
    _trim_seen(seen, current_uris)

    return jsonify({
        "init_count": init_count,
        "items": items,
    })

# =============================================================================
# API - TTS
# =============================================================================

@app.post("/api/tts")
def api_tts():
    """
    POST:
      - JSON: {"text": "...", "lang": "pt-BR"}  (lang opcional)
      - OU query string: ?lang=pt-BR
      - OU header: X-Lang: pt-BR
    Retorna WAV (audio/wav). Headers:
      X-TTS-Engine, X-TTS-Lang, X-TTS-Model
    """
    mem = _require_mem()
    _require_auth()
    _check_tts_rate_limit(mem)

    # tenta parsear JSON; se não for JSON, segue em frente com defaults
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}

    # aceita lang de 3 lugares (ordem de prioridade):
    lang = (
        request.args.get("lang")
        or data.get("lang")
        or request.headers.get("X-Lang")
        or "en-US"
    )
    lang = lang.strip()

    text = (data.get("text") or "").strip()
    if not text:
        # também aceita texto em text/plain (fallback útil p/ testes)
        if request.mimetype == "text/plain":
            text = (request.data or b"").decode("utf-8", errors="ignore").strip()
    if not text:
        abort(400, description="texto vazio")

    # Limite para não travar (ajuste à vontade)
    if len(text) > 1200:
        text = text[:1200]

    if not TTS_SEMAPHORE.acquire(blocking=False):
        abort(503, description="TTS ocupado. Tente novamente em instantes.")
    try:
        wav, model_name = _piper_tts(text, lang)
    finally:
        TTS_SEMAPHORE.release()

    resp = make_response(send_file(
        io.BytesIO(wav),
        mimetype="audio/wav",
        as_attachment=False,
        download_name="tts.wav"
    ))
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-TTS-Engine"] = "piper"
    resp.headers["X-TTS-Lang"] = lang
    resp.headers["X-TTS-Model"] = model_name
    return resp


@app.get("/api/tts_caps")
def api_tts_caps():
    """
    Informa quais idiomas o Piper realmente cobre.
    available_bases: códigos de 2 letras com modelo nativo encontrado.
    voices: lista pronta para a UI, incluindo idioma, modelo e disponibilidade.
    models: mapa base->modelo resolvido (útil para debug).
    """
    available_bases = set()
    models_map: Dict[str, str] = {}
    voices: List[Dict[str, Any]] = []

    for base in _configured_tts_bases():
        onnx, _cfg = _pick_piper_paths(base)
        model = os.path.basename(onnx).removesuffix(".onnx") if onnx else ""
        native = _is_native_for_lang(base, onnx)
        available = _file_exists(onnx) and native
        models_map[base] = model
        if available:
            available_bases.add(base)
        voices.append({
            "base": base,
            "lang": base,
            "label": PIPER_LANG_LABELS.get(base, base.upper()),
            "model": model,
            "available": available,
            "native": native,
        })

    return jsonify({
        "available_bases": sorted(available_bases),
        "default_base": "en" if "en" in available_bases else (sorted(available_bases)[0] if available_bases else ""),
        "models": models_map,
        "strict": TTS_STRICT_PIPER,
        "voices": voices,
    })


@app.get("/api/my_lists")
def api_my_lists():
    auth = _require_auth()
    _require_mem()
    did = auth.get("did")
    if not did:
        abort(400, description="Sem DID.")
    # pagina simples (até 200 listas)
    lists = []
    cursor = None
    for _ in range(4):
        res = _get_lists(auth["accessJwt"], did, cursor)
        if res["status"] == 401:
            _refresh_auth(auth)
            res = _get_lists(auth["accessJwt"], did, cursor)
        if not res["ok"]:
            abort(res["status"], str(res["json"] or ""))

        for li in (res["json"] or {}).get("lists") or []:
            lists.append({
                "uri": li.get("uri"),
                "name": li.get("name"),
                "purpose": li.get("purpose"),
                "description": li.get("description"),
            })
        cursor = (res["json"] or {}).get("cursor")
        if not cursor:
            break
    return jsonify({"lists": lists})


@app.get("/api/list_feed")
def api_list_feed():
    auth = _require_auth()
    mem = _require_mem()
    list_uri = (request.args.get("uri") or "").strip()
    if not list_uri:
        abort(400, description="uri da lista é obrigatório")
    limit = request.args.get("limit", "40")
    try:
        limit_i = max(1, min(100, int(limit)))
    except ValueError:
        abort(400, description="limit inválido")

    res = _get_list_feed(auth["accessJwt"], list_uri, limit=limit_i)
    if res["status"] == 401:
        _refresh_auth(auth)
        res = _get_list_feed(auth["accessJwt"], list_uri, limit=limit_i)
    if not res["ok"]:
        abort(res["status"], str(res["json"] or ""))

    feed_rows = (res["json"] or {}).get("feed") or []
    seen: Set[str] = _ensure_seen(mem, "lists")
    new = []
    for row in feed_rows:
        post = row.get("post") or {}
        uri = post.get("uri")
        if not uri or uri in seen:
            continue
        new.append(row)
    items = _parse_items_with_embed(new)
    current_uris = []
    for row in feed_rows:
        post = row.get("post") or {}
        uri = post.get("uri")
        if uri:
            current_uris.append(uri)
            seen.add(uri)
    _trim_seen(seen, current_uris)
    return jsonify({"items": items})


@app.get("/api/search_posts")
def api_search_posts():
    auth = _require_auth()
    mem = _require_mem()
    q = (request.args.get("q") or "").strip()
    if not q:
        abort(400, description="q é obrigatório (texto ou #hashtag)")
    limit = request.args.get("limit", "40")
    try:
        limit_i = max(1, min(100, int(limit)))
    except ValueError:
        abort(400, description="limit inválido")

    res = _search_posts(auth["accessJwt"], q, limit=limit_i)
    if res["status"] == 401:
        _refresh_auth(auth)
        res = _search_posts(auth["accessJwt"], q, limit=limit_i)
    if not res["ok"]:
        abort(res["status"], str(res["json"] or ""))

    posts = (res["json"] or {}).get("posts") or []
    rows = [{"post": p} for p in posts]

    seen: Set[str] = _ensure_seen(mem, "search")
    new = []
    for row in rows:
        uri = (row.get("post") or {}).get("uri")
        if not uri or uri in seen:
            continue
        new.append(row)
    items = _parse_items_with_embed(new)
    current_uris = []
    for row in rows:
        uri = (row.get("post") or {}).get("uri")
        if uri:
            current_uris.append(uri)
            seen.add(uri)
    _trim_seen(seen, current_uris)
    return jsonify({"items": items})


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=_env_bool("FLASK_DEBUG", False),
    )
