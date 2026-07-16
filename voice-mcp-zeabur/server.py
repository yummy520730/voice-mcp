"""
昨的语音条 MCP Server (Zeabur 适配版)
基于祈牌语音条改造，适配 Zeabur 容器部署 + Claude OAuth shim。
"""

import os, json, base64, hashlib, asyncio, subprocess, uuid, time, secrets
import aiohttp, numpy as np
from pathlib import Path
from aiohttp import web
from urllib.parse import urlencode, parse_qs, urlparse

os.environ["MCP_DISABLE_TRANSPORT_SECURITY"] = "1"

from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "voice-mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CUSTOMIZE_PATH = BASE_DIR / "customize" / "index.html"
WIDGET_JS_PATH = BASE_DIR / "dist" / "widget" / "voice-view-widget.global.js"

VOICE_VIEW_URI = "ui://voice-view/mcp-app-v7.html"
VOICE_VIEW_MIME = "text/html;profile=mcp-app"
LEGACY_VIEW_URIS = [f"ui://voice-view/mcp-app-v{i}.html" for i in range(1, 7)]

DEFAULT_CONFIG = {
    "tts_engine": "elevenlabs",
    "public_base_url": "",
    "elevenlabs": {
        "api_key": "",
        "voice_id": "",
        "model_id": "eleven_v3",
        "stability": 0.28,
        "similarity_boost": 0.8,
        "speed": 0.9,
    },
    "style": {
        "theme": "dark",
        "color_primary": "#94a3b8",
        "color_secondary": "#64748b",
        "color_bg": "#0f172a",
        "color_bg_end": "#1e293b",
        "bubble_style": "waveform",
        "bar_count": 35,
        "sender_name": "昨",
        "bg_image": "",
        "custom_css": "",
    },
}


def init_config_from_env():
    if CONFIG_PATH.exists():
        return
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["elevenlabs"]["api_key"] = os.environ.get("ELEVENLABS_API_KEY", "")
    cfg["elevenlabs"]["voice_id"] = os.environ.get("ELEVENLABS_VOICE_ID", "")
    save_config(cfg)


def load_config() -> dict:
    init_config_from_env()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            merged = json.loads(json.dumps(DEFAULT_CONFIG))
            for k, v in user_cfg.items():
                if isinstance(v, dict) and k in merged and isinstance(merged[k], dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            return merged
        except Exception:
            return json.loads(json.dumps(DEFAULT_CONFIG))
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── TTS ────────────────────────────────────────────────────

async def tts_elevenlabs(text: str, cfg: dict) -> bytes:
    el = cfg["elevenlabs"]
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{el['voice_id']}"
    headers = {"xi-api-key": el["api_key"], "Content-Type": "application/json", "Accept": "audio/mpeg"}
    payload = {
        "text": text,
        "model_id": el["model_id"],
        "voice_settings": {
            "stability": el["stability"],
            "similarity_boost": el["similarity_boost"],
            "speed": el.get("speed", 1.0),
        },
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"ElevenLabs error ({r.status}): {await r.text()}")
            return await r.read()


async def generate_speech(text: str, cfg: dict) -> tuple[bytes, str]:
    return await tts_elevenlabs(text, cfg), "audio/mpeg"


def estimate_duration(text: str, speed: float = 1.0) -> int:
    cn = sum(1 for c in text if "一" <= c <= "鿿")
    en = len(text) - cn
    return max(1, round((cn / 4 + en / 12) / max(0.1, speed)))


def wave_bar_count(duration: int) -> int:
    return max(12, min(60, round(duration * 3.2)))


def extract_waveform(audio: bytes, n_bars: int) -> list:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1"],
            input=audio, capture_output=True, timeout=20,
        )
        pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
        if len(pcm) < n_bars:
            return []
        chunks = np.array_split(pcm, n_bars)
        peaks = np.array([float(np.sqrt(np.mean(c ** 2))) if len(c) else 0.0 for c in chunks])
        mx = peaks.max() or 1.0
        norm = (peaks / mx) ** 0.7
        return [round(float(x), 3) for x in norm]
    except Exception:
        return []


# ── Widget ─────────────────────────────────────────────────

def widget_html() -> str:
    if WIDGET_JS_PATH.exists():
        js = WIDGET_JS_PATH.read_text(encoding="utf-8")
    else:
        js = "document.getElementById('root').innerHTML='<div style=\"color:#b8aabb;font-size:13px\">语音组件未构建</div>';"
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<style>\n"
        "  :root { color-scheme: light dark; }\n"
        "  * { box-sizing: border-box; }\n"
        "  html, body { margin:0; padding:0; background:transparent;"
        " width:100%; height:fit-content; overflow:hidden; }\n"
        "  #root { display:block; width:100%; }\n"
        "</style></head>\n"
        '<body><div id="root"></div><script>' + js + "</script></body></html>"
    )


def csp_meta(cfg: dict) -> dict:
    base = (cfg.get("public_base_url") or "").rstrip("/")
    origins = [base] if base else []
    return {
        "ui": {"csp": {"resourceDomains": origins, "connectDomains": origins}},
        "openai/widgetCSP": {"resource_domains": origins, "connect_domains": origins},
    }


WIDGET_META = {"openai/outputTemplate": VOICE_VIEW_URI, "ui": {"resourceUri": VOICE_VIEW_URI}}


@mcp.resource(VOICE_VIEW_URI, mime_type=VOICE_VIEW_MIME, name="voice-view", meta=csp_meta(load_config()))
def voice_view() -> str:
    return widget_html()


def _register_legacy_aliases():
    _csp = csp_meta(load_config())
    for _i, _uri in enumerate(LEGACY_VIEW_URIS):
        def _alias() -> str:
            return widget_html()
        mcp.resource(_uri, mime_type=VOICE_VIEW_MIME, name=f"voice-view-legacy-{_i + 1}", meta=_csp)(_alias)

_register_legacy_aliases()


# ── Tool ───────────────────────────────────────────────────

class VoicePayload(BaseModel):
    audioUrl: str
    duration: int = 1
    senderName: str = "昨"
    colorPrimary: str = "#94a3b8"
    colorSecondary: str = "#64748b"
    colorBg: str = "#0f172a"
    colorBgEnd: str = "#1e293b"
    barCount: int = 35
    bgImage: str = ""
    customCss: str = ""
    bars: list[float] = []


@mcp.tool(
    name="send_voice",
    description="发送一条语音消息。输入要说的话，会用昨的音色生成语音，并在聊天里渲染成一条可播放的语音条气泡。",
    meta=WIDGET_META,
)
async def send_voice(text: str) -> VoicePayload:
    cfg = load_config()
    engine = cfg.get("tts_engine", "elevenlabs")
    if not cfg.get(engine, {}).get("api_key"):
        raise Exception(f"{engine} API key 未配置")

    audio, mime = await generate_speech(text, cfg)
    style = cfg["style"]
    speed = cfg.get(engine, {}).get("speed", 1.0)
    duration = estimate_duration(text, speed)
    return VoicePayload(
        audioUrl=f"data:{mime};base64,{base64.b64encode(audio).decode()}",
        duration=duration,
        bars=extract_waveform(audio, wave_bar_count(duration)),
        senderName=style["sender_name"],
        colorPrimary=style["color_primary"],
        colorSecondary=style["color_secondary"],
        colorBg=style["color_bg"],
        colorBgEnd=style["color_bg_end"],
        barCount=int(style["bar_count"]),
        bgImage=style.get("bg_image", ""),
        customCss=style.get("custom_css", ""),
    )


@mcp.tool(name="voice_config", description="查看或修改语音条配置。")
async def voice_config(
    tts_engine: str = None,
    color_primary: str = None,
    sender_name: str = None,
) -> str:
    cfg = load_config()
    changed = False
    if tts_engine in ("elevenlabs", "minimax"):
        cfg["tts_engine"] = tts_engine; changed = True
    if color_primary:
        cfg["style"]["color_primary"] = color_primary; changed = True
    if sender_name:
        cfg["style"]["sender_name"] = sender_name; changed = True
    if changed:
        save_config(cfg)
        return f"已更新 | 引擎: {cfg['tts_engine']} | 名字: {cfg['style']['sender_name']}"
    safe = json.loads(json.dumps(cfg))
    for eng in ("elevenlabs",):
        if safe.get(eng, {}).get("api_key"):
            safe[eng]["api_key"] = "***"
    return json.dumps(safe, indent=2, ensure_ascii=False)


# ── OAuth Shim for Claude Connector ───────────────────────
# Claude requires OAuth endpoints to register a custom connector.
# This implements a minimal pass-through OAuth that auto-approves.

_oauth_codes = {}   # code -> {client_id, redirect_uri, ts}
_oauth_clients = {} # client_id -> {secret, redirect_uris}

def get_base_url():
    return os.environ.get("PUBLIC_BASE_URL", "https://zuo1.zeabur.app").rstrip("/")

async def handle_oauth_metadata(request):
    base = get_base_url()
    return web.json_response({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256", "plain"],
    })

async def handle_oauth_register(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    client_id = "voice-" + secrets.token_hex(8)
    client_secret = secrets.token_hex(16)
    redirect_uris = data.get("redirect_uris", [])
    _oauth_clients[client_id] = {"secret": client_secret, "redirect_uris": redirect_uris}
    return web.json_response({
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    })

async def handle_oauth_authorize(request):
    params = request.query
    redirect_uri = params.get("redirect_uri", "")
    client_id = params.get("client_id", "")
    state = params.get("state", "")
    code = secrets.token_hex(16)
    _oauth_codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri, "ts": time.time()}
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}&state={state}"
    return web.HTTPFound(location)

async def handle_oauth_token(request):
    try:
        data = await request.post()
    except Exception:
        data = {}
    if not data:
        try:
            data = await request.json()
        except Exception:
            data = {}
    grant_type = data.get("grant_type", "")
    if grant_type == "refresh_token":
        return web.json_response({
            "access_token": secrets.token_hex(20),
            "token_type": "bearer",
            "expires_in": 86400,
            "refresh_token": secrets.token_hex(20),
        })
    code = data.get("code", "")
    if code in _oauth_codes:
        del _oauth_codes[code]
    return web.json_response({
        "access_token": secrets.token_hex(20),
        "token_type": "bearer",
        "expires_in": 86400,
        "refresh_token": secrets.token_hex(20),
    })


# ── Main with OAuth routes ─────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.responses import JSONResponse, RedirectResponse, Response
    from starlette.requests import Request as StarletteRequest

    port = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8000")))

    # Build the MCP ASGI app
    mcp_app = mcp.streamable_http_app()

    # OAuth routes as Starlette
    async def s_oauth_meta(request):
        base = get_base_url()
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256", "plain"],
        })

    async def s_oauth_register(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        client_id = "voice-" + secrets.token_hex(8)
        client_secret = secrets.token_hex(16)
        redirect_uris = data.get("redirect_uris", [])
        _oauth_clients[client_id] = {"secret": client_secret, "redirect_uris": redirect_uris}
        return JSONResponse({
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        })

    async def s_oauth_authorize(request):
        params = request.query_params
        redirect_uri = params.get("redirect_uri", "")
        client_id = params.get("client_id", "")
        state = params.get("state", "")
        code = secrets.token_hex(16)
        _oauth_codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri, "ts": time.time()}
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={code}&state={state}"
        return RedirectResponse(location)

    async def s_oauth_token(request):
        content_type = request.headers.get("content-type", "")
        if "json" in content_type:
            try:
                data = await request.json()
            except Exception:
                data = {}
        else:
            try:
                form = await request.form()
                data = dict(form)
            except Exception:
                data = {}
        grant_type = data.get("grant_type", "")
        code = data.get("code", "")
        if code in _oauth_codes:
            del _oauth_codes[code]
        return JSONResponse({
            "access_token": secrets.token_hex(20),
            "token_type": "bearer",
            "expires_in": 86400,
            "refresh_token": secrets.token_hex(20),
        })

    async def s_oauth_protected_resource(request):
        base = get_base_url()
        return JSONResponse({
            "resource": base,
            "authorization_servers": [base],
        })

oauth_routes = [
    Route("/.well-known/oauth-authorization-server", s_oauth_meta, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource", s_oauth_protected_resource, methods=["GET"]),
    Route("/oauth/register", s_oauth_register, methods=["POST"]),
    Route("/oauth/authorize", s_oauth_authorize, methods=["GET"]),
    Route("/oauth/token", s_oauth_token, methods=["POST"]),
]

for route in reversed(oauth_routes):
    mcp_app.router.routes.insert(0, route)

app = mcp_app

if __name__ == "__main__":
    print(f"✓ 昨的语音条 MCP 启动中，端口 {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
