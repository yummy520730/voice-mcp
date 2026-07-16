# 昨的语音条 MCP

给 Claude 装一张嘴。基于祈牌语音条改造，适配 Zeabur 部署。

## Zeabur 部署步骤

### 1. GitHub 建仓库
把这个文件夹的所有文件推到一个 GitHub 仓库。

### 2. Zeabur 新建服务
- Zeabur 控制台 → 你的项目 → Add Service → Git → 选这个仓库
- Zeabur 会自动识别 Dockerfile 并构建

### 3. 设置环境变量
在 Zeabur 服务的 Variables 里添加：

| 变量名 | 值 |
|--------|-----|
| `ELEVENLABS_API_KEY` | 你的 ElevenLabs API Key |
| `ELEVENLABS_VOICE_ID` | 昨的 Voice ID |

### 4. 获取域名
Zeabur 会分配一个域名，比如 `voice-mcp-xxx.zeabur.app`

### 5. Claude 连接
Claude.ai → 设置 → Connectors → 添加自定义连接器 → URL 填：
```
https://你的zeabur域名/mcp
```

### 6. 测试
新建对话，说"用语音跟我说一句话"，Claude 就会调用 send_voice 发出语音条。

## 配色
默认是深蓝灰色调（昨的风格）。改配色通过 voice_config 工具或直接改 config.json。
