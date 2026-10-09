# 🧠 Phase 4: Amharic Voice AI Agent & Mini App Roadmap

This document outlines the architectural blueprint, technology stack, and implementation milestones for building the **Amharic Voice AI Agent** and **Telegram Mini App**.

---

## 🎯 Project Vision

Build an intelligent, conversational **Amharic AI Agent** capable of:
1. **Understanding spoken Amharic** via Speech-to-Text (STT).
2. **Retrieving accurate domain knowledge** via local Vector RAG (educational docs, websites, cultural/technical materials).
3. **Generating natural, culturally nuanced Amharic responses** using advanced LLMs.
4. **Speaking back in natural Amharic audio** via Text-to-Speech (TTS).
5. **Instant responses on repeat queries** using an intelligent local embedding cache.
6. **Telegram Integration**: Both inside regular chat (voice notes) and as an interactive, modern Telegram Mini App.

---

## 🏗️ System Architecture

```
User Voice Message / Web Audio
             │
             ▼
   [ 1. Audio Processing ]
   Convert Opus/OGG/WAV → 16kHz Mono
             │
             ▼
   [ 2. Speech-to-Text (STT) ]
   OpenAI Whisper / faster-whisper (Amharic 'am' support)
             │
             ▼
      Transcribed Text (Ge'ez Script)
             │
      ┌──────┴────────────────────────┐
      ▼                               ▼
[ Cache Check ]               [ 3. Knowledge Retrieval (RAG) ]
Seen before?                  ChromaDB / FAISS Vector Store
├─► Return cached audio        Semantic search across uploaded
│                             documents, guides & web content
└─► Continue pipeline         
             │
             ▼
   [ 4. AI Reasoning (LLM) ]
   OpenRouter (Gemini 2.5 Flash / DeepSeek / Claude)
   System Prompt: Native Amharic specialist
             │
             ▼
       Response Text (Ge'ez Script)
             │
             ▼
   [ 5. Text-to-Speech (TTS) ]
   Meta MMS-TTS (Amharic) or Google TTS (gTTS 'am')
             │
             ▼
   [ 6. Delivery ]
   Telegram Voice Note (.ogg) / Mini App Audio Player
```

---

## 🛠️ Technology Stack & Selection

### 1. Speech-to-Text (STT)
| Engine | Type | Latency | Accuracy (Amharic) | Cost |
|--------|------|---------|-------------------|------|
| **OpenAI Whisper API** | Cloud API | ~1.5s | High | $0.006 / min (~free for testing) |
| **faster-whisper (medium/small)** | Self-hosted | ~0.8s (GPU) | High | $0 (requires GPU/VPS) |
| **HuggingFace MMS (Meta)** | Self-hosted | ~1.0s | Good | $0 |

> **Recommendation**: Start prototyping with the **Whisper API** (fastest setup, zero local GPU required). Migrate to `faster-whisper` on a GPU instance later if scaling.

---

### 2. Knowledge Base & Vector RAG
| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Vector Database** | ChromaDB / FAISS | Lightweight, free, zero-config local vector database |
| **Embeddings** | `text-embedding-3-small` or BGE-M3 | Multilingual text embedding models |
| **Chunking** | Recursive Character Splitter | Splits documents into 500-token chunks with 50-token overlap |
| **Document Ingestion** | PyPDF, BeautifulSoup, Markdown | Load PDFs, websites, FAQs, and custom knowledge |

#### Caching Strategy:
- When a user asks a question, compute query embedding.
- Check cosine similarity with previously answered questions (similarity threshold `≥ 0.94`).
- If matched: serve cached answer & audio instantly (sub-200ms latency).
- If new: run full LLM + TTS pipeline, then cache result.

---

### 3. Text-to-Speech (TTS)
| Engine | Amharic Quality | Hosting | Cost |
|--------|----------------|---------|------|
| **Meta MMS-TTS (`amh`)** | Natural, human-like Amharic | Self-hosted / HF | Free |
| **Google TTS (`gTTS am`)** | Clear, standard pronunciation | Free API | Free |
| **ElevenLabs (Multilingual v2)** | Ultra-realistic | Cloud API | Free tier (10k chars/mo) |

> **Recommendation**: Implement `gTTS` for rapid baseline testing, with an optional switch to Meta MMS-TTS for superior Amharic vocal inflection.

---

## 📱 Interface Options

### Mode A: Telegram Voice Chat Bot
- User sends a Telegram voice note.
- Bot replies with an audio message (voice note) + expandable transcript text.
- Simple, familiar, zero install needed.

### Mode B: Telegram Mini App (Web App)
- Opens full-screen inside Telegram.
- **Glassmorphism Dark UI**:
  - Live animated microphone waveform during voice input.
  - Interactive chat bubble history with Amharic typography.
  - Audio playback controls with scrubber & speed toggle (1x / 1.5x).
  - Searchable knowledge cards & suggested topics.

---

## 📅 Implementation Milestones

### Milestone 1: Audio Pipeline & STT Prototype
- [x] Add Telegram voice message handler (`filters.VOICE`).
- [x] Convert `.oga` Telegram voice notes to `.wav` via ffmpeg.
- [x] Transcribe Amharic audio using Whisper API / Gemini Flash multimodal audio.
- [x] Validate transcription accuracy on sample Amharic speech.

### Milestone 2: Amharic Knowledge Base (RAG)
- [ ] Set up local ChromaDB storage.
- [ ] Build script to ingest PDFs, text files, and FAQs into vector embeddings.
- [ ] Connect similarity search to augment LLM prompt context.
- [ ] Implement local query/audio cache for instant repeat answers.

### Milestone 3: Amharic Voice Response (TTS)
- [x] Integrate Amharic TTS engine (`gTTS` Amharic `am` voice synthesis).
- [x] Convert synthesized speech to Telegram voice note format (OGG OPUS).
- [x] Send voice response back to the user with caption transcript.

### Milestone 4: Telegram Mini App Experience
- [ ] Build responsive web frontend (Vanilla CSS, Glassmorphism aesthetic).
- [ ] Connect Telegram WebApp SDK (`window.Telegram.WebApp`).
- [ ] Live audio streaming & waveform visualizer.
- [ ] Deploy frontend to static host (Vercel / Cloudflare Pages / Railway).

---

## 💡 Cost & Resource Projection

| Resource | Development Phase | Production (Free Tier) | Scaled |
|----------|-------------------|------------------------|--------|
| **LLM (OpenRouter/Gemini)** | ~$0.50 (test queries) | Free (Gemini Flash free tier) | ~$5 / mo |
| **STT (Whisper API)** | ~$0.30 (test clips) | ~$1-2 / mo | ~$10 / mo |
| **TTS (gTTS / MMS)** | Free | Free | Free |
| **Vector DB (ChromaDB)** | Local / Free | Local file storage | Free |
| **Hosting** | Local / Free | Northflank (Container) | $0 - $5 / mo |

---

*Prepared for: telegram-translator-bot — Phase 4 Roadmap*
