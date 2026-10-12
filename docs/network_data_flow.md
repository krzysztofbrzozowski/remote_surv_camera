### Network Data Flow (Production)

How data flows through the network from each component to the end user:

```mermaid
flowchart LR
    subgraph Home["🏠 Home / Remote Site"]
        RPI["📷 RPi Streamer"]
    end

    subgraph VPS["🖥️  VPS (203.0.113.10)"]
        direction TB
        NGX_P["nginx-proxy\n:443 SSL"]
        PB["proxy-bridge\n:80"]
        DAPH["daphne\n:8000"]
        REDIS2["Redis\n:6379"]
        COTURN["coturn\n:3478 UDP+TCP\nrelay :49152–49252"]

        NGX_P -->|HTTP| PB -->|HTTP/WS| DAPH
        DAPH <-->|pub/sub| REDIS2
    end

    CF["☁️  Cloudflare\nDNS + CDN + WAF\ncamera.example.com"]

    subgraph User["🌐 End User Browser"]
        BR["Browser"]
    end

    RPI == "① WSS (H.264 + metadata)\nport 443 via Cloudflare" ==> CF
    CF == "② HTTPS/WSS\nproxied" ==> NGX_P

    BR == "③ HTTPS (pages, API)\nport 443 via Cloudflare" ==> CF
    BR == "④ WSS (signaling + metadata)\nport 443 via Cloudflare" ==> CF

    DAPH -. "⑤a WebRTC RTP (H.264)\ndirect UDP to browser IP" .-> BR
    DAPH -. "⑤b WebRTC RTP via TURN\nUDP/TCP relay" .-> COTURN -. "relayed to browser" .-> BR

    style Home fill:#fef3c7,stroke:#d97706,color:#000
    style VPS fill:#dbeafe,stroke:#2563eb,color:#000
    style User fill:#dcfce7,stroke:#16a34a,color:#000
    style CF fill:#fff7ed,stroke:#ea580c,color:#000
```

| # | Path | Protocol | Ports | Goes through Cloudflare? |
|---|---|---|---|---|
| ① | RPi → VPS | **WSS** (WebSocket over TLS) | 443 | ✅ Yes |
| ② | Cloudflare → VPS | **HTTPS / WSS** | 443 → 80 → 8000 | — (CF to origin) |
| ③ | Browser → VPS | **HTTPS** (pages, login, recordings) | 443 | ✅ Yes |
| ④ | Browser → VPS | **WSS** (WebRTC signaling, live metadata) | 443 | ✅ Yes |
| ⑤a | VPS → Browser | **WebRTC RTP** (H.264 video, direct UDP) | random UDP | ❌ No — direct IP |
| ⑤b | VPS → Browser | **WebRTC RTP via TURN** (relay fallback) | 3478 + 49152–49252 UDP | ❌ No — direct IP |

> **Why TURN?** Carriers like Orange PL 5G use Symmetric NAT / CGNAT which blocks direct UDP (path ⑤a). TURN relay (path ⑤b) tunnels WebRTC through the coturn server so video still reaches the viewer.