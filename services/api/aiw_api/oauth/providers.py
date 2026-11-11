"""v3.9.27: every connector in 设置 › 连接器 and how it connects — directly, no relay.

method
  mcp_oauth   official remote MCP + OAuth (MCP authorization spec: discovery, DCR when offered, PKCE, resource).
              `provider` names a pre-registered client config used when the server has no DCR.
  native      aiw's own REST tools (google / microsoft), the user's own OAuth client from 设置 › 连接器 › 高级.
  rest_oauth  OAuth2 against the vendor + a small preset of REST tools (`tools`), Bearer from the connection.
  token       official remote MCP with an API key / PAT (Bearer header or key in the URL).
  rest_token  vendor REST API with an API key, preset tools.
  mcp_url     the vendor's OWN official MCP address generated in its open platform (not a relay).
"""
from __future__ import annotations

# OAuth clients for providers that need one the user registers (no DCR). Endpoints are the vendors' documented ones.
PROVIDERS: dict[str, dict] = {
    "google": {
        "name": "Google", "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token", "revoke": "https://oauth2.googleapis.com/revoke",
        "extra": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "console": "https://console.cloud.google.com/apis/credentials",
        "help": "Google Cloud 控制台 › API 和服务 › 凭据 › 创建 OAuth 客户端 ID（类型选「Web 应用」，已获授权的重定向 URI 填下面这个地址）。"
                "启用 Gmail API、Google Calendar API、Google Drive API。OAuth 同意屏幕保持「测试」状态时 refresh token 7 天过期，"
                "长期使用请「发布应用」到 Production（个人使用可以不过审，登录时点「高级 › 继续」）。",
        "scopes": {
            "gmail": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"],
            "gmail_send": ["https://www.googleapis.com/auth/gmail.send"],
            "gcal": ["https://www.googleapis.com/auth/calendar.events"],
            "gdrive": ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"],
        },
        "base_scopes": ["openid", "email"],
    },
    "microsoft": {
        "name": "Microsoft", "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token", "revoke": "",
        "extra": {"prompt": "select_account"},
        "console": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
        "help": "Microsoft Entra 管理中心 › 应用注册 › 新注册：受支持的账户类型选「任何组织目录中的账户和个人 Microsoft 账户」，"
                "重定向 URI 平台选「Web」填下面这个地址；证书和密码 › 新客户端密码。API 权限加 Microsoft Graph 委托权限 "
                "offline_access、User.Read、Mail.ReadWrite、Mail.Send、Calendars.ReadWrite。个人 Outlook.com 和工作账号都能登录。",
        "scopes": {"outlook": ["offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"]},
        "base_scopes": [],
    },
    "github": {
        "name": "GitHub", "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token", "revoke": "",
        "console": "https://github.com/settings/developers",
        "help": "GitHub › Settings › Developer settings › OAuth Apps › New OAuth App，Authorization callback URL 填下面这个地址。"
                "不想建 OAuth App 也可以继续用个人访问令牌。",
        "scopes": {"github": ["repo", "read:org", "read:user"]}, "no_pkce_ok": True,
    },
    "slack": {
        "name": "Slack", "authorize": "https://slack.com/oauth/v2_user/authorize",
        "token": "https://slack.com/api/oauth.v2.user.access", "revoke": "",
        "console": "https://api.slack.com/apps",
        "help": "api.slack.com/apps › Create New App，OAuth & Permissions 里加 Redirect URL（下面这个地址），"
                "User Token Scopes 加 search:read、channels:history、chat:write、users:read，并在 App 设置里打开 MCP。",
        "scopes": {"slack": ["search:read", "channels:history", "groups:history", "chat:write", "users:read"]},
    },
    "asana": {
        "name": "Asana", "authorize": "https://app.asana.com/-/oauth_authorize",
        "token": "https://app.asana.com/-/oauth_token", "revoke": "https://app.asana.com/-/oauth_revoke",
        "console": "https://app.asana.com/0/my-apps",
        "help": "Asana 的 MCP 不接受动态注册时：app.asana.com/0/my-apps 新建应用，Redirect URL 填下面这个地址。",
        "scopes": {"asana": []},
    },
    "dropbox": {
        "name": "Dropbox", "authorize": "https://www.dropbox.com/oauth2/authorize",
        "token": "https://api.dropboxapi.com/oauth2/token", "revoke": "",
        "extra": {"token_access_type": "offline"},
        "console": "https://www.dropbox.com/developers/apps",
        "help": "dropbox.com/developers/apps › Create app（Scoped access），Permissions 勾 files.metadata.read、files.content.read、"
                "sharing.write，Redirect URIs 加下面这个地址。只填 App key 即可（PKCE 公开客户端），App secret 可留空。",
        "scopes": {"dropbox": ["files.metadata.read", "files.content.read", "sharing.write"]},
    },
    "box": {
        "name": "Box", "authorize": "https://account.box.com/api/oauth2/authorize",
        "token": "https://api.box.com/oauth2/token", "revoke": "https://api.box.com/oauth2/revoke",
        "console": "https://app.box.com/developers/console",
        "help": "Box 开发者控制台 › Create New App › Custom App › User Authentication (OAuth 2.0)，Redirect URI 填下面这个地址。",
        "scopes": {"box": []},
    },
    "canva": {
        "name": "Canva", "authorize": "https://www.canva.com/api/oauth/authorize",
        "token": "https://api.canva.com/rest/v1/oauth/token", "revoke": "https://api.canva.com/rest/v1/oauth/revoke",
        "auth_method": "client_secret_basic",
        "console": "https://www.canva.com/developers/integrations",
        "help": "Canva 开发者门户 › Your integrations › Create，Authentication 里加 Redirect URL（下面这个地址），"
                "Scopes 勾 design:meta:read、design:content:read、design:content:write。私有集成只能自己团队用。",
        "scopes": {"canva": ["design:meta:read", "design:content:read", "design:content:write"]},
    },
}

# connector id -> how it connects. Kept in step with CONNECTORS in apps/web/ui.js (tests check it).
CATALOG: dict[str, dict] = {
    "gmail": {"name": "Gmail", "method": "native", "provider": "google", "services": ["gmail"]},
    "gcal": {"name": "Google 日历", "method": "native", "provider": "google", "services": ["gcal"]},
    "gdrive": {"name": "Google 云端硬盘", "method": "native", "provider": "google", "services": ["gdrive"]},
    "outlook": {"name": "Outlook", "method": "native", "provider": "microsoft", "services": ["outlook"]},
    "notion": {"name": "Notion", "method": "mcp_oauth", "mcp_url": "https://mcp.notion.com/mcp"},
    "linear": {"name": "Linear", "method": "mcp_oauth", "mcp_url": "https://mcp.linear.app/mcp",
               "token_ok": True},
    "jira": {"name": "Jira / Confluence", "method": "mcp_oauth", "mcp_url": "https://mcp.atlassian.com/v2/mcp"},
    "asana": {"name": "Asana", "method": "mcp_oauth", "mcp_url": "https://mcp.asana.com/v2/mcp", "provider": "asana"},
    "stripe": {"name": "Stripe", "method": "mcp_oauth", "mcp_url": "https://mcp.stripe.com", "token_ok": True},
    "github": {"name": "GitHub", "method": "mcp_oauth", "mcp_url": "https://api.githubcopilot.com/mcp/",
               "provider": "github", "token_ok": True},
    "slack": {"name": "Slack", "method": "mcp_oauth", "mcp_url": "https://mcp.slack.com/mcp", "provider": "slack"},
    "huggingface": {"name": "Hugging Face", "method": "token", "mcp_url": "https://huggingface.co/mcp"},
    "supabase": {"name": "Supabase", "method": "token", "mcp_url": "https://mcp.supabase.com/mcp"},
    "firecrawl": {"name": "Firecrawl", "method": "token"},
    "tavily": {"name": "Tavily", "method": "token"},
    "dropbox": {"name": "Dropbox", "method": "rest_oauth", "provider": "dropbox", "tools": [
        {"name": "dropbox_search", "description": "在 Dropbox 里按文件名/内容搜索文件", "method": "POST",
         "url": "https://api.dropboxapi.com/2/files/search_v2",
         "params": {"query": {"type": "string", "description": "搜索词", "required": True}}},
        {"name": "dropbox_list_folder", "description": "列出 Dropbox 文件夹内容（根目录 path 传空字符串）", "method": "POST",
         "url": "https://api.dropboxapi.com/2/files/list_folder",
         "params": {"path": {"type": "string", "description": "如 /Documents，根目录用 \"\"", "required": True}}},
        {"name": "dropbox_get_temporary_link", "description": "拿到文件 4 小时有效的下载链接", "method": "POST",
         "url": "https://api.dropboxapi.com/2/files/get_temporary_link",
         "params": {"path": {"type": "string", "description": "文件路径", "required": True}}},
        {"name": "dropbox_create_shared_link", "description": "给文件创建分享链接（写操作，需确认）", "method": "POST",
         "url": "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
         "params": {"path": {"type": "string", "description": "文件路径", "required": True}}}]},
    "box": {"name": "Box", "method": "rest_oauth", "provider": "box", "tools": [
        {"name": "box_search", "description": "在 Box 里搜索文件和文件夹", "method": "GET", "url": "https://api.box.com/2.0/search",
         "params": {"query": {"type": "string", "description": "搜索词", "required": True},
                    "limit": {"type": "integer", "description": "条数，默认 20"}}},
        {"name": "box_folder_items", "description": "列出文件夹内容（根目录 id 为 0）", "method": "GET",
         "url": "https://api.box.com/2.0/folders/{folder_id}/items",
         "params": {"folder_id": {"type": "string", "description": "文件夹 id", "required": True}}},
        {"name": "box_file_info", "description": "文件详情", "method": "GET", "url": "https://api.box.com/2.0/files/{file_id}",
         "params": {"file_id": {"type": "string", "description": "文件 id", "required": True}}}]},
    "canva": {"name": "Canva", "method": "rest_oauth", "provider": "canva", "tools": [
        {"name": "canva_list_designs", "description": "列出 Canva 里的设计", "method": "GET",
         "url": "https://api.canva.com/rest/v1/designs",
         "params": {"query": {"type": "string", "description": "按标题搜索（可选）"}}},
        {"name": "canva_get_design", "description": "设计详情（含编辑/查看链接）", "method": "GET",
         "url": "https://api.canva.com/rest/v1/designs/{design_id}",
         "params": {"design_id": {"type": "string", "description": "设计 id", "required": True}}},
        {"name": "canva_create_export", "description": "导出设计为 PDF/PNG（写操作，需确认），返回导出任务 id", "method": "POST",
         "url": "https://api.canva.com/rest/v1/exports",
         "params": {"design_id": {"type": "string", "description": "设计 id", "required": True},
                    "format": {"type": "object", "description": "如 {\"type\":\"pdf\"} 或 {\"type\":\"png\"}", "required": True}}},
        {"name": "canva_get_export", "description": "查询导出任务，完成后返回下载地址", "method": "GET",
         "url": "https://api.canva.com/rest/v1/exports/{export_id}",
         "params": {"export_id": {"type": "string", "description": "导出任务 id", "required": True}}}]},
    "calendly": {"name": "Calendly", "method": "rest_token", "header": "Authorization", "prefix": "Bearer ", "tools": [
        {"name": "calendly_me", "description": "当前 Calendly 用户（返回 user uri，查日程要用）", "method": "GET",
         "url": "https://api.calendly.com/users/me", "params": {}},
        {"name": "calendly_event_types", "description": "列出预约类型和预约链接", "method": "GET",
         "url": "https://api.calendly.com/event_types",
         "params": {"user": {"type": "string", "description": "calendly_me 返回的 user uri", "required": True}}},
        {"name": "calendly_scheduled_events", "description": "列出已预约的日程", "method": "GET",
         "url": "https://api.calendly.com/scheduled_events",
         "params": {"user": {"type": "string", "description": "user uri", "required": True},
                    "min_start_time": {"type": "string", "description": "ISO 时间，可选"},
                    "status": {"type": "string", "description": "active 或 canceled"}}}]},
    "todoist": {"name": "Todoist", "method": "rest_token", "header": "Authorization", "prefix": "Bearer ", "tools": [
        {"name": "todoist_tasks", "description": "列出 Todoist 待办（可用过滤语法，如 today | overdue）", "method": "GET",
         "url": "https://api.todoist.com/api/v1/tasks/filter",
         "params": {"query": {"type": "string", "description": "过滤语法，默认 today | overdue", "required": True}}},
        {"name": "todoist_create_task", "description": "新建 Todoist 待办（写操作，需确认）", "method": "POST",
         "url": "https://api.todoist.com/api/v1/tasks",
         "params": {"content": {"type": "string", "description": "待办内容", "required": True},
                    "due_string": {"type": "string", "description": "自然语言日期，如 明天下午3点"}}},
        {"name": "todoist_close_task", "description": "完成一条待办（写操作，需确认）", "method": "POST",
         "url": "https://api.todoist.com/api/v1/tasks/{task_id}/close",
         "params": {"task_id": {"type": "string", "description": "待办 id", "required": True}}}]},
    "figma": {"name": "Figma", "method": "rest_token", "header": "X-Figma-Token", "prefix": "", "tools": [
        {"name": "figma_get_file", "description": "读取 Figma 文件结构（页面、画板、图层）", "method": "GET",
         "url": "https://api.figma.com/v1/files/{file_key}",
         "params": {"file_key": {"type": "string", "description": "文件链接 figma.com/design/<file_key>/…", "required": True},
                    "depth": {"type": "integer", "description": "层级深度，建议 2"}}},
        {"name": "figma_export_images", "description": "把节点导出成图片，返回图片地址", "method": "GET",
         "url": "https://api.figma.com/v1/images/{file_key}",
         "params": {"file_key": {"type": "string", "description": "文件 key", "required": True},
                    "ids": {"type": "string", "description": "节点 id，逗号分隔", "required": True},
                    "format": {"type": "string", "description": "png / svg / pdf"}}},
        {"name": "figma_file_comments", "description": "读取文件评论", "method": "GET",
         "url": "https://api.figma.com/v1/files/{file_key}/comments",
         "params": {"file_key": {"type": "string", "description": "文件 key", "required": True}}}]},
    "trello": {"name": "Trello", "method": "rest_token", "query_auth": True, "tools": [
        {"name": "trello_boards", "description": "列出我的 Trello 看板", "method": "GET",
         "url": "https://api.trello.com/1/members/me/boards", "params": {}},
        {"name": "trello_lists", "description": "看板里的列表", "method": "GET",
         "url": "https://api.trello.com/1/boards/{board_id}/lists",
         "params": {"board_id": {"type": "string", "description": "看板 id", "required": True}}},
        {"name": "trello_cards", "description": "列表里的卡片", "method": "GET",
         "url": "https://api.trello.com/1/lists/{list_id}/cards",
         "params": {"list_id": {"type": "string", "description": "列表 id", "required": True}}},
        {"name": "trello_create_card", "description": "新建卡片（写操作，需确认）", "method": "POST",
         "url": "https://api.trello.com/1/cards",
         "params": {"idList": {"type": "string", "description": "列表 id", "required": True},
                    "name": {"type": "string", "description": "标题", "required": True},
                    "desc": {"type": "string", "description": "描述"}}}]},
    "onepassword": {"name": "1Password", "method": "rest_token", "header": "Authorization", "prefix": "Bearer ",
                    "base_url_field": True, "tools": [
        {"name": "op_vaults", "description": "列出 1Password Connect 能访问的保险库", "method": "GET",
         "url": "{base}/v1/vaults", "params": {}},
        {"name": "op_items", "description": "按标题搜索保险库里的条目", "method": "GET",
         "url": "{base}/v1/vaults/{vault_id}/items",
         "params": {"vault_id": {"type": "string", "description": "保险库 id", "required": True},
                    "filter": {"type": "string", "description": "如 title co \"GitHub\""}}},
        {"name": "op_get_item", "description": "读取一个条目（含字段值，只在需要登录时用）", "method": "GET",
         "url": "{base}/v1/vaults/{vault_id}/items/{item_id}",
         "params": {"vault_id": {"type": "string", "description": "保险库 id", "required": True},
                    "item_id": {"type": "string", "description": "条目 id", "required": True}}}]},
    # v3.9.41: Adobe's official hosted MCP (Photoshop, Lightroom, Illustrator, Firefly, Premiere, Express, InDesign, Stock)
    "adobe": {"name": "Adobe 创意工具", "method": "mcp_oauth", "mcp_url": "https://adobe-creativity.adobe.io/mcp",
              "preset": "adobe"},
    "feishu": {"name": "飞书", "method": "mcp_url"},
    "dingtalk": {"name": "钉钉", "method": "mcp_url"},
}

# v3.9.41: remote MCP presets whose server does not follow the discovery dance exactly. Keys used by
# connections._start_preset / integrations.connection_tools:
#   skip_probe      do not probe first (Adobe answers an unauthenticated initialize with 403 and no WWW-Authenticate)
#   as_meta         authorization server metadata to use when discovery is unreachable (checked 2026-11-11)
#   scopes          requested by default (registration AND authorize); base_scopes = fallback after invalid_scope
#   dcr_auth        token_endpoint_auth_method to register with (IMS lists only client_secret_basic/post)
#   dcr_scope       send `scope` in the registration (IMS only grants llm.* to a client registered with them)
#   resource        auto: send RFC 8707 `resource` on authorize + token, drop it on the token call when refused
#   retry_403       a 403 on a call with a token = refresh once and retry (no WWW-Authenticate challenge)
#   save_dir        where result files go on the computer (relative to ~/workspace)
#   gate            tools only offered to tasks that look like this (setting expose=always turns the gate off)
MCP_PRESETS: dict[str, dict] = {
    "adobe": {
        "skip_probe": True,
        "issuer": "https://ims-na1.adobelogin.com",
        "as_meta": {"issuer": "https://ims-na1.adobelogin.com",
                    "authorization_endpoint": "https://ims-na1.adobelogin.com/ims/authorize/v2",
                    "token_endpoint": "https://ims-na1.adobelogin.com/ims/token/v3",
                    "registration_endpoint": "https://ims-na1.adobelogin.com/ims/register",
                    "revocation_endpoint": "https://ims-na1.adobelogin.com/ims/revoke",
                    "scopes_supported": ["openid", "AdobeID", "offline_access", "llm.assets.manage", "llm.assets.upload"]},
        "scopes": ["openid", "AdobeID", "offline_access", "llm.assets.manage", "llm.assets.upload"],
        "base_scopes": ["openid", "AdobeID", "offline_access"],
        "dcr_auth": "client_secret_post", "dcr_scope": True, "client_name": "AI Workstation",
        "resource": "auto", "retry_403": True, "fetch_links": True, "save_dir": "connectors/adobe",
        "gate": r"(?i)adobe|photoshop|(?<![a-z])(?:ps|lr|pr)(?![a-z])|lightroom|illustrator|firefly|premiere|express|indesign|"
                r"stock|acrobat|修图|抠图|去背景|换背景|调色|滤镜|海报|配图|插画|矢量|logo|标志|设计|排版|画册|生图|生成图|"
                r"图片|照片|相片|图像|头像|封面|banner|视频|剪辑|字幕|素材|图库|创意|美化|扩图|p图",
        "help": "Adobe 官方托管 MCP（adobe-creativity.adobe.io）。点「用 Adobe 账号登录」，在 Adobe 页面登录并同意授权即可，"
                "aiw 会自动注册客户端（不用去 Adobe 开发者控制台）。从国内访问 adobe.io 可能不稳定，可以在下面填代理。",
    },
}

# old relay connectors, only read by the one-time migration (connections.migrate_relays)
RELAY_IDS = ("zapier", "composio")
RELAY_HOST_RE = r"(zapier\.com|composio\.dev|pipedream\.net)"

METHOD_LABEL = {"mcp_oauth": "官方 MCP + OAuth", "native": "原生 OAuth（自带客户端）", "rest_oauth": "官方 REST + OAuth",
                "token": "官方 MCP + API Key", "rest_token": "官方 REST + API Key", "mcp_url": "官方开放平台 MCP 地址",
                "mcp": "自定义 MCP 服务器", "openapi": "OpenAPI 导入"}


def scopes_for(provider: str, services: list[str], send: bool = False) -> list[str]:
    p = PROVIDERS[provider]
    out = list(p.get("base_scopes") or [])
    for s in services:
        out += p["scopes"].get(s, [])
        if send and s == "gmail":
            out += p["scopes"].get("gmail_send", [])
    return list(dict.fromkeys(out))
