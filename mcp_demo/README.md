# Zoho Recruit Demo MCP Server (Fictional Data)

A lightweight, standalone Model Context Protocol (MCP) server providing Server-Sent Events (SSE) transport and returning **fictional mock recruitment data**.

This server is designed specifically to test connecting the **iGentic Channel Yield Analytics Agent** to an MCP SSE endpoint on Render without requiring:
- Real Zoho Recruit OAuth credentials or API access
- Database connections or migrations
- Token encryption keys (`CY_TOKEN_ENCRYPTION_KEY`)
- Tenant IDs or organization configurations

---

## 1. Safety & Data Privacy

- **Zero Real Data**: All records returned by `list_job_openings`, `list_candidates`, and `list_applications` are synthetic mock entries clearly marked `(Demo)` and `fictional-demo-data`.
- **Zero Real Secrets**: Requires no API keys, client secrets, or database URLs.
- **Strict Isolation**: Completely decoupled from the production application, database models, and existing Zoho services.

---

## 2. Supported Tools

The server implements the Model Context Protocol (MCP) JSON-RPC 2.0 specification (`2024-11-05`):

| Tool Name | Parameters | Description |
| :--- | :--- | :--- |
| `list_job_openings` | `status` (optional string), `limit` (int, 1-50) | Returns fictional demo job requisitions. |
| `list_candidates` | `limit` (int, 1-50) | Returns fictional candidate summaries (no real PII). |
| `list_applications` | `job_opening_id` (optional), `status` (optional), `limit` (int, 1-50) | Returns fictional application status tracking. |

---

## 3. Endpoints

- **`GET /health`**: Health check probe returning service status and active session count.
- **`GET /sse`**: SSE transport entrypoint. Issues an `endpoint` event containing `/messages?session_id=<uuid>` and streams JSON-RPC responses.
- **`POST /messages?session_id=<uuid>`**: JSON-RPC message endpoint. Receives client requests (`initialize`, `tools/list`, `tools/call`, `ping`) and routes responses through the active SSE stream.

---

## 4. Local Execution & Testing

### Installation

```bash
pip install -r requirements-mcp-demo.txt
```

### Running Locally

```bash
python -m mcp_demo.server --port 10000
```

### Verifying with `curl`

1. **Health Check**:
   ```bash
   curl http://localhost:10000/health
   ```
   Expected response:
   ```json
   {
     "status": "ok",
     "service": "zoho-recruit-mcp-demo",
     "version": "1.0.0",
     "mode": "fictional-demo-data",
     "active_sessions": 0
   }
   ```

2. **Connect to SSE Stream**:
   ```bash
   curl -N http://localhost:10000/sse
   ```
   Initial output:
   ```text
   event: endpoint
   data: /messages?session_id=12345678-1234-5678-1234-567812345678
   ```

3. **Send JSON-RPC Message via POST**:
   In a separate terminal, using the `session_id` returned in step 2:
   ```bash
   curl -X POST "http://localhost:10000/messages?session_id=12345678-1234-5678-1234-567812345678" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
   ```
   The response appears instantly on the SSE stream terminal:
   ```text
   event: message
   data: {"jsonrpc": "2.0", "id": 1, "result": {"tools": [...]}}
   ```

---

## 5. Connecting from iGentic

In the iGentic MCP configuration interface:
- **Server URL**: `https://<your-render-demo-url>/sse`
- **Transport**: `sse`
- **Headers / Authentication**: None required (this demo server accepts direct connections).

---

## 6. Render Deployment Steps (When Approved)

1. In the Render Dashboard, choose **Blueprints** -> **New Blueprint Instance**.
2. Select the repository: `Hariprasath2011/channel-yield-analytics-agent`.
3. Set the Blueprint spec path to: `render-mcp-demo.yaml`.
4. Render will create the web service `zoho-recruit-mcp-demo` on the free tier.
5. Once live, test `https://<app-name>.onrender.com/health` and provide `https://<app-name>.onrender.com/sse` to iGentic.
