"""
昨的语音条 MCP Server
适配 Zeabur / 容器部署、Streamable HTTP、Claude OAuth shim 和 MCP App 资源。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route


# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
WIDGET_JS_PATH = BASE_DIR / "dist" / "widget" / "voice-view-widget.global.js"

VOICE_VIEW_URI = "ui://voice-view/mcp-app-v8.html"
VOICE_VIEW_MIME = "text/html;profile=mcp-app"
LEGACY_VIEW_URIS = [
    f"ui://voice-view/mcp-app-v{i}.html"
    for i in range(1, 8)
]

DEFAULT_CONFIG: dict[str, Any] = {
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


def deep_copy_default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def init_config_from_env() -> None:
    if CONFIG_PATH.exists():
        return

    config = deep_copy_default_config()
    config["public_base_url"] = os.getenv("PUBLIC_BASE_URL", "")
    config["elevenlabs"]["api_key"] = os.getenv("ELEVENLABS_API_KEY", "")
    config["elevenlabs"]["voice_id"] = os.getenv("ELEVENLABS_VOICE_ID", "")
    save_config(config)


def load_config() -> dict[str, Any]:
    init_config_from_env()

    try:
        user_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        user_config = {}

    merged = deep_copy_default_config()
    for key, value in user_config.items():
        if (
            isinstance(value, dict)
            and key in merged
            and isinstance(merged[key], dict)
        ):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value

    # 环境变量优先，便于 Zeabur 在不改文件的情况下更新密钥和公网地址。
    if os.getenv("PUBLIC_BASE_URL"):
        merged["public_base_url"] = os.environ["PUBLIC_BASE_URL"]
    if os.getenv("ELEVENLABS_API_KEY"):
        merged["elevenlabs"]["api_key"] = os.environ["ELEVENLABS_API_KEY"]
    if os.getenv("ELEVENLABS_VOICE_ID"):
        merged["elevenlabs"]["voice_id"] = os.environ["ELEVENLABS_VOICE_ID"]

    return merged


def get_base_url() -> str:
    config = load_config()
    return (
        os.getenv("PUBLIC_BASE_URL")
        or config.get("public_base_url")
        or "https://zuo1.zeabur.app"
    ).rstrip("/")


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "voice-mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

async def tts_elevenlabs(text: str, config: dict[str, Any]) -> bytes:
    elevenlabs = config["elevenlabs"]
    api_key = elevenlabs.get("api_key", "")
    voice_id = elevenlabs.get("voice_id", "")

    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY 未配置")
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID 未配置")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": elevenlabs.get("model_id", "eleven_v3"),
        "voice_settings": {
            "stability": float(elevenlabs.get("stability", 0.28)),
            "similarity_boost": float(
                elevenlabs.get("similarity_boost", 0.8)
            ),
            "speed": float(elevenlabs.get("speed", 1.0)),
        },
    }

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            json=payload,
            headers=headers,
        ) as response:
            if response.status != 200:
                detail = await response.text()
                raise RuntimeError(
                    f"ElevenLabs 请求失败（HTTP {response.status}）：{detail}"
                )
            return await response.read()


async def generate_speech(
    text: str,
    config: dict[str, Any],
) -> tuple[bytes, str]:
    engine = config.get("tts_engine", "elevenlabs")
    if engine != "elevenlabs":
        raise RuntimeError(f"暂不支持 TTS 引擎：{engine}")

    return await tts_elevenlabs(text, config), "audio/mpeg"


def estimate_duration(text: str, speed: float = 1.0) -> int:
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other_chars = max(0, len(text) - chinese_chars)
    seconds = (chinese_chars / 4 + other_chars / 12) / max(0.1, speed)
    return max(1, round(seconds))


def wave_bar_count(duration: int) -> int:
    return max(12, min(60, round(duration * 3.2)))


def extract_waveform(audio: bytes, bar_count: int) -> list[float]:
    try:
        process = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "pipe:1",
            ],
            input=audio,
            capture_output=True,
            timeout=20,
            check=False,
        )

        if process.returncode != 0 or not process.stdout:
            return []

        pcm = np.frombuffer(
            process.stdout,
            dtype=np.int16,
        ).astype(np.float32)

        if len(pcm) < bar_count:
            return []

        chunks = np.array_split(pcm, bar_count)
        peaks = np.array(
            [
                float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0
                for chunk in chunks
            ]
        )

        maximum = float(peaks.max()) or 1.0
        normalized = (peaks / maximum) ** 0.7
        return [round(float(value), 3) for value in normalized]
    except (OSError, subprocess.SubprocessError, ValueError):
        # 没有 ffmpeg 时仍返回可播放语音，只是不显示真实波形。
        return []


# ---------------------------------------------------------------------------
# MCP App Widget
# ---------------------------------------------------------------------------

def widget_html() -> str:
    if WIDGET_JS_PATH.exists():
        widget_js = WIDGET_JS_PATH.read_text(encoding="utf-8")
    else:
        widget_js = """
        document.getElementById("root").innerHTML =
          '<div style="padding:12px;font:13px sans-serif;color:#b8aabb">' +
          '语音组件未构建：缺少 dist/widget/voice-view-widget.global.js' +
          '</div>';
        """

    # JS 内联，避免 MCP App iframe 再请求相对路径资源而触发 404/CSP。
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1, viewport-fit=cover"
  >
  <style>
    :root {{ color-scheme: light dark; }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      min-height: 1px;
      overflow: hidden;
      background: transparent;
    }}
    #root {{
      display: block;
      width: 100%;
    }}
  </style>
</head>
<body>
  <div id="root"></div>
  <script>{widget_js}</script>
</body>
</html>
"""


def widget_csp_meta() -> dict[str, Any]:
    base_url = get_base_url()
    domains = [base_url] if base_url else []

    return {
        "ui": {
            "csp": {
                "resourceDomains": domains,
                "connectDomains": domains,
            }
        },
        "openai/widgetCSP": {
            "resource_domains": domains,
            "connect_domains": domains,
        },
    }


WIDGET_META = {
    "openai/outputTemplate": VOICE_VIEW_URI,
    "ui": {"resourceUri": VOICE_VIEW_URI},
}


@mcp.resource(
    VOICE_VIEW_URI,
    mime_type=VOICE_VIEW_MIME,
    name="voice-view",
    meta=widget_csp_meta(),
)
def voice_view() -> str:
    return widget_html()


def register_legacy_widget_aliases() -> None:
    for index, uri in enumerate(LEGACY_VIEW_URIS, start=1):

        def legacy_view() -> str:
            return widget_html()

        mcp.resource(
            uri,
            mime_type=VOICE_VIEW_MIME,
            name=f"voice-view-legacy-{index}",
            meta=widget_csp_meta(),
        )(legacy_view)


register_legacy_widget_aliases()


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

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
    bars: list[float] = Field(default_factory=list)


@mcp.tool(
    name="send_voice",
    description=(
        "发送一条语音消息。输入要说的话，会生成语音，"
        "并在聊天中渲染为可播放的语音气泡。"
    ),
    meta=WIDGET_META,
)
async def send_voice(text: str) -> VoicePayload:
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("语音文本不能为空")
    if len(clean_text) > 5000:
        raise ValueError("语音文本过长，请控制在 5000 个字符以内")

    config = load_config()
    audio, mime_type = await generate_speech(clean_text, config)

    engine = config.get("tts_engine", "elevenlabs")
    engine_config = config.get(engine, {})
    style = config["style"]

    duration = estimate_duration(
        clean_text,
        float(engine_config.get("speed", 1.0)),
    )

    return VoicePayload(
        audioUrl=(
            f"data:{mime_type};base64,"
            f"{base64.b64encode(audio).decode('ascii')}"
        ),
        duration=duration,
        bars=extract_waveform(audio, wave_bar_count(duration)),
        senderName=str(style.get("sender_name", "昨")),
        colorPrimary=str(style.get("color_primary", "#94a3b8")),
        colorSecondary=str(style.get("color_secondary", "#64748b")),
        colorBg=str(style.get("color_bg", "#0f172a")),
        colorBgEnd=str(style.get("color_bg_end", "#1e293b")),
        barCount=int(style.get("bar_count", 35)),
        bgImage=str(style.get("bg_image", "")),
        customCss=str(style.get("custom_css", "")),
    )


@mcp.tool(
    name="voice_config",
    description="查看或修改语音条配置。",
)
async def voice_config(
    tts_engine: str | None = None,
    color_primary: str | None = None,
    sender_name: str | None = None,
) -> str:
    config = load_config()
    changed = False

    if tts_engine is not None:
        if tts_engine != "elevenlabs":
            raise ValueError("当前只支持 elevenlabs")
        config["tts_engine"] = tts_engine
        changed = True

    if color_primary:
        config["style"]["color_primary"] = color_primary
        changed = True

    if sender_name:
        config["style"]["sender_name"] = sender_name
        changed = True

    if changed:
        save_config(config)
        return (
            f"已更新 | 引擎: {config['tts_engine']} | "
            f"名字: {config['style']['sender_name']}"
        )

    safe_config = json.loads(json.dumps(config))
    if safe_config.get("elevenlabs", {}).get("api_key"):
        safe_config["elevenlabs"]["api_key"] = "***"

    return json.dumps(safe_config, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Claude Connector OAuth shim
# 这是最小兼容实现，不适合作为高安全级别生产 OAuth 服务。
# ---------------------------------------------------------------------------

_oauth_codes: dict[str, dict[str, Any]] = {}
_oauth_clients: dict[str, dict[str, Any]] = {}


async def oauth_metadata(_: Request) -> JSONResponse:
    base_url = get_base_url()
    return JSONResponse(
        {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": [
                "authorization_code",
                "refresh_token",
            ],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
            ],
            "code_challenge_methods_supported": ["S256", "plain"],
        }
    )


async def oauth_protected_resource(_: Request) -> JSONResponse:
    base_url = get_base_url()
    return JSONResponse(
        {
            "resource": base_url,
            "authorization_servers": [base_url],
        }
    )


async def oauth_register(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        data = {}

    client_id = f"voice-{secrets.token_hex(8)}"
    client_secret = secrets.token_hex(16)
    redirect_uris = data.get("redirect_uris", [])

    _oauth_clients[client_id] = {
        "secret": client_secret,
        "redirect_uris": redirect_uris,
    }

    return JSONResponse(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": redirect_uris,
            "grant_types": [
                "authorization_code",
                "refresh_token",
            ],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }
    )


async def oauth_authorize(request: Request) -> RedirectResponse | JSONResponse:
    redirect_uri = request.query_params.get("redirect_uri", "")
    client_id = request.query_params.get("client_id", "")
    state = request.query_params.get("state", "")

    if not redirect_uri:
        return JSONResponse(
            {"error": "invalid_request", "detail": "缺少 redirect_uri"},
            status_code=400,
        )

    registered = _oauth_clients.get(client_id)
    if registered:
        allowed_redirects = registered.get("redirect_uris", [])
        if allowed_redirects and redirect_uri not in allowed_redirects:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "detail": "redirect_uri 未注册",
                },
                status_code=400,
            )

    code = secrets.token_hex(16)
    _oauth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "created_at": time.time(),
    }

    separator = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{separator}code={code}&state={state}"
    return RedirectResponse(location)


async def read_request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            result = await request.json()
            return result if isinstance(result, dict) else {}

        form = await request.form()
        return dict(form)
    except Exception:
        return {}


async def oauth_token(request: Request) -> JSONResponse:
    data = await read_request_data(request)
    grant_type = data.get("grant_type", "")

    if grant_type == "authorization_code":
        code = str(data.get("code", ""))
        code_data = _oauth_codes.pop(code, None)

        if code_data is None:
            return JSONResponse(
                {"error": "invalid_grant"},
                status_code=400,
            )

        # 授权码十分钟过期。
        if time.time() - float(code_data["created_at"]) > 600:
            return JSONResponse(
                {"error": "invalid_grant"},
                status_code=400,
            )

    elif grant_type != "refresh_token":
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    return JSONResponse(
        {
            "access_token": secrets.token_hex(20),
            "token_type": "Bearer",
            "expires_in": 86400,
            "refresh_token": secrets.token_hex(20),
        }
    )


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "voice-mcp",
            "mcp_endpoint": "/mcp",
            "widget_uri": VOICE_VIEW_URI,
        }
    )


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

mcp_app = mcp.streamable_http_app()

extra_routes = [
    Route("/health", health, methods=["GET"]),
    Route(
        "/.well-known/oauth-authorization-server",
        oauth_metadata,
        methods=["GET"],
    ),
    Route(
        "/.well-known/oauth-protected-resource",
        oauth_protected_resource,
        methods=["GET"],
    ),
    Route("/oauth/register", oauth_register, methods=["POST"]),
    Route("/oauth/authorize", oauth_authorize, methods=["GET"]),
    Route("/oauth/token", oauth_token, methods=["POST"]),
]

# OAuth 和 health 路由必须排在 MCP catch-all 路由之前。
for route in reversed(extra_routes):
    mcp_app.router.routes.insert(0, route)

app = mcp_app


if __name__ == "__main__":
    port = int(os.getenv("PORT", os.getenv("WEB_PORT", "8000")))
    print(f"✓ 昨的语音条 MCP 启动中，端口 {port}")
    print(f"✓ MCP endpoint: {get_base_url()}/mcp")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
