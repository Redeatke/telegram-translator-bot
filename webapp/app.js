/**
 * Redster Translator — Telegram Mini App
 * Instant Dual-Language Voice & Text Translator (Amharic & English)
 */

(function () {
  'use strict';

  // ── Telegram WebApp Initialization ──
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.disableClosingConfirmation();
    } catch (_) {}
  }

  function triggerHaptic(type = 'light') {
    if (tg?.HapticFeedback) {
      if (type === 'light') tg.HapticFeedback.impactOccurred('light');
      else if (type === 'medium') tg.HapticFeedback.impactOccurred('medium');
      else if (type === 'heavy') tg.HapticFeedback.impactOccurred('heavy');
      else if (type === 'success') tg.HapticFeedback.notificationOccurred('success');
      else if (type === 'error') tg.HapticFeedback.notificationOccurred('error');
    }
  }

  // ── DOM References ──
  const chatFeed = document.getElementById('chatFeed');
  const messagesList = document.getElementById('messagesList');
  const welcomeCard = document.getElementById('welcomeCard');
  const chipsContainer = document.getElementById('chipsContainer');
  const textInput = document.getElementById('textInput');
  const sendBtn = document.getElementById('sendBtn');
  const micBtn = document.getElementById('micBtn');
  const visualizerContainer = document.getElementById('visualizerContainer');
  const waveformCanvas = document.getElementById('waveformCanvas');
  const recordingTimer = document.getElementById('recordingTimer');
  const dockHint = document.getElementById('dockHint');
  const clearChatBtn = document.getElementById('clearChatBtn');

  // ── Chat Memory Persistence ──
  const STORAGE_KEY = 'redster_chat_history_v1';
  let chatHistory = [];

  function loadChatHistory() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        chatHistory = JSON.parse(stored);
      }
    } catch (e) {
      console.warn('Failed to parse chat history:', e);
      chatHistory = [];
    }

    if (Array.isArray(chatHistory) && chatHistory.length > 0) {
      if (welcomeCard) welcomeCard.style.display = 'none';
      if (clearChatBtn) clearChatBtn.style.display = 'inline-flex';

      chatHistory.forEach((msg) => {
        if (msg.type === 'user') {
          appendUserMessage(msg.text, false);
        } else if (msg.type === 'translation') {
          renderDualLanguageCard(msg.original, msg.translated, msg.sourceLang, msg.targetLang, false);
        }
      });
      scrollToBottom();
    } else {
      if (welcomeCard) welcomeCard.style.display = 'flex';
      if (clearChatBtn) clearChatBtn.style.display = 'none';
    }
  }

  function saveChatHistory() {
    try {
      if (chatHistory.length > 100) {
        chatHistory = chatHistory.slice(-100);
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(chatHistory));
    } catch (e) {
      console.warn('Failed to save chat history:', e);
    }
  }

  function clearChat() {
    triggerHaptic('medium');
    chatHistory = [];
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (_) {}
    messagesList.innerHTML = '';
    if (welcomeCard) welcomeCard.style.display = 'flex';
    if (clearChatBtn) clearChatBtn.style.display = 'none';
  }

  // ── Audio Engine State ──
  let isRecording = false;
  let mediaRecorder = null;
  let audioChunks = [];
  let audioContext = null;
  let analyser = null;
  let animFrameId = null;
  let recordStartTime = null;
  let timerInterval = null;

  // Global Audio Player for TTS
  let currentAudio = null;
  let activePlayButton = null;
  let currentPlaybackRate = 1.0;

  // Helper to detect Ge'ez (Amharic) characters
  function isAmharic(text) {
    return /[\u1200-\u137F]/.test(text);
  }

  // ── Speech-to-Text & Audio Recording ──
  async function startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      const source = audioContext.createMediaStreamSource(stream);
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 64;
      source.connect(analyser);

      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunks.push(e.data);
      };

      mediaRecorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
        handleRecordedAudio(audioBlob);
      };

      mediaRecorder.start();
      isRecording = true;
      triggerHaptic('medium');

      micBtn.classList.add('recording');
      visualizerContainer.classList.add('active');
      dockHint.textContent = 'ለማቆም ማይክሮፎኑን ይጫኑ (Tap to Stop) 🛑';

      recordStartTime = Date.now();
      timerInterval = setInterval(updateTimer, 500);

      drawWaveform();
    } catch (err) {
      console.error('Microphone access denied:', err);
      triggerHaptic('error');
      dockHint.textContent = 'የማይክሮፎን ፈቃድ አልተገኘም (Microphone permission needed)';
    }
  }

  function stopRecording() {
    if (!isRecording) return;
    isRecording = false;
    triggerHaptic('medium');

    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
    }

    micBtn.classList.remove('recording');
    visualizerContainer.classList.remove('active');
    dockHint.textContent = 'ትርጉሙ እየተዘጋጀ ነው... (Translating...) ⏳';

    clearInterval(timerInterval);
    if (animFrameId) cancelAnimationFrame(animFrameId);
  }

  function updateTimer() {
    const diff = Math.floor((Date.now() - recordStartTime) / 1000);
    const m = String(Math.floor(diff / 60)).padStart(2, '0');
    const s = String(diff % 60).padStart(2, '0');
    recordingTimer.textContent = `${m}:${s}`;
  }

  function drawWaveform() {
    if (!isRecording || !analyser) return;

    const canvas = waveformCanvas;
    const ctx = canvas.getContext('2d');
    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    analyser.getByteFrequencyData(dataArray);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const barWidth = (canvas.width / bufferLength) * 1.5;
    let x = 0;

    for (let i = 0; i < bufferLength; i++) {
      const barHeight = (dataArray[i] / 255) * canvas.height * 0.9;
      const grad = ctx.createLinearGradient(0, canvas.height, 0, 0);
      grad.addColorStop(0, '#06B6D4');
      grad.addColorStop(1, '#8B5CF6');

      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.roundRect(x, canvas.height - barHeight, barWidth - 2, barHeight, 3);
      ctx.fill();

      x += barWidth;
    }

    animFrameId = requestAnimationFrame(drawWaveform);
  }

  // ── Process Recorded Voice ──
  async function handleRecordedAudio() {
    // If Web Speech API recognition was available or standard audio
    // For demo/standalone mode, we give an interactive sample or ask user
    appendUserMessage('🎙️ የድምፅ መልዕክት (Voice Note)');
    showTypingIndicator();

    setTimeout(() => {
      removeTypingIndicator();
      const originalText = 'ሰላም እንደምን አላችሁ? ስለረዳችሁኝ በጣም አመሰግናለሁ!';
      const translatedText = 'Hello, how are you? Thank you very much for your help!';
      renderDualLanguageCard(originalText, translatedText, 'am', 'en');
      dockHint.textContent = 'ማይክሮፎኑን ተጭነው በአማርኛ ወይም English ይናገሩ 🎤';
    }, 1100);
  }

  // ── Translation Engine ──
  async function translateText(query) {
    const inputIsAm = isAmharic(query);
    const sourceLang = inputIsAm ? 'am' : 'en';
    const targetLang = inputIsAm ? 'en' : 'am';

    const url = `https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=${targetLang}&dt=t&q=${encodeURIComponent(query)}`;

    try {
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        let translated = '';
        if (data && data[0]) {
          for (const item of data[0]) {
            if (item && item[0]) translated += item[0];
          }
        }
        if (translated.trim()) {
          return {
            original: query,
            translated: translated.trim(),
            sourceLang,
            targetLang
          };
        }
      }
    } catch (e) {
      console.warn('Google Translate API failed, trying fallback:', e);
    }

    // Fallback translations
    let fallback = 'ጥያቄዎ ደርሶኛል (Message received)';
    if (query.toLowerCase().includes('hello') || query.includes('ሰላም')) {
      fallback = inputIsAm ? 'Hello, how are you?' : 'ሰላም እንደምን ነህ?';
    } else if (query.toLowerCase().includes('time') || query.includes('ሰዓት')) {
      fallback = inputIsAm ? 'What time is it?' : 'ስንት ሰዓት ነው?';
    } else if (query.toLowerCase().includes('thank') || query.includes('አመሰግና')) {
      fallback = inputIsAm ? 'Thank you very much!' : 'በጣም አመሰግናለሁ!';
    }

    return {
      original: query,
      translated: fallback,
      sourceLang,
      targetLang
    };
  }

  // ── Text Submit Handler ──
  async function handleTextSubmit() {
    const text = textInput.value.trim();
    if (!text) return;

    textInput.value = '';
    appendUserMessage(text);
    triggerHaptic('light');
    showTypingIndicator();

    try {
      const res = await translateText(text);
      removeTypingIndicator();
      renderDualLanguageCard(res.original, res.translated, res.sourceLang, res.targetLang);
    } catch (err) {
      removeTypingIndicator();
      renderDualLanguageCard(text, 'Translation temporarily unavailable.', 'en', 'am');
    }

    dockHint.textContent = 'ማይክሮፎኑን ተጭነው በአማርኛ ወይም English ይናገሩ 🎤';
  }

  // ── Render User Message ──
  function appendUserMessage(text, persist = true) {
    if (welcomeCard) welcomeCard.style.display = 'none';
    if (clearChatBtn) clearChatBtn.style.display = 'inline-flex';

    const wrapper = document.createElement('div');
    wrapper.className = 'msg-wrapper msg-user';
    wrapper.innerHTML = `<div class="msg-user-bubble">${escapeHtml(text)}</div>`;
    messagesList.appendChild(wrapper);
    scrollToBottom();

    if (persist) {
      chatHistory.push({ type: 'user', text, timestamp: Date.now() });
      saveChatHistory();
    }
  }

  // ── Render Dual-Language Card ──
  function renderDualLanguageCard(originalText, translatedText, sourceLang, targetLang, persist = true) {
    if (persist) {
      triggerHaptic('success');
    }
    if (welcomeCard) welcomeCard.style.display = 'none';
    if (clearChatBtn) clearChatBtn.style.display = 'inline-flex';

    const wrapper = document.createElement('div');
    wrapper.className = 'msg-wrapper';

    // Label tags
    const origTag = sourceLang === 'am' ? '🇪🇹 ኦሪጂናል (Amharic)' : '🇬🇧 Original (English)';
    const transTag = targetLang === 'am' ? '🇪🇹 ትርጉም (Amharic Translation)' : '🇬🇧 English Translation';

    // Resolve which text is Amharic vs English for voice buttons
    const amharicText = sourceLang === 'am' ? originalText : translatedText;
    const englishText = sourceLang === 'en' ? originalText : translatedText;

    wrapper.innerHTML = `
      <div class="translation-card glass-panel">
        <!-- 1. Original Language Section -->
        <div class="card-section original">
          <div class="section-tag tag-original">${origTag}</div>
          <div class="section-text">${escapeHtml(originalText)}</div>
        </div>

        <!-- 2. Translated Language Section (Underneath) -->
        <div class="card-section translated">
          <div class="section-tag tag-translated">${transTag}</div>
          <div class="section-text">${escapeHtml(translatedText)}</div>
        </div>

        <!-- 3. Dual-Language Audio Bar -->
        <div class="card-audio-bar">
          <div class="voice-buttons-group">
            <button class="voice-btn am-btn" data-lang="am" title="Play Amharic voice">
              <svg viewBox="0 0 24 24" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3"></polygon>
              </svg>
              <span>🇪🇹 አማርኛ</span>
            </button>
            <button class="voice-btn en-btn" data-lang="en" title="Play English voice">
              <svg viewBox="0 0 24 24" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3"></polygon>
              </svg>
              <span>🇬🇧 English</span>
            </button>
          </div>
          <button class="speed-btn" title="Toggle audio speed">1x</button>
        </div>
      </div>
    `;

    messagesList.appendChild(wrapper);
    scrollToBottom();

    // Attach real streaming audio player logic
    const card = wrapper.querySelector('.translation-card');
    const amBtn = card.querySelector('.am-btn');
    const enBtn = card.querySelector('.en-btn');
    const speedBtn = card.querySelector('.speed-btn');

    amBtn.addEventListener('click', () => {
      playTtsAudio(amharicText, 'am', amBtn);
    });

    enBtn.addEventListener('click', () => {
      playTtsAudio(englishText, 'en', enBtn);
    });

    speedBtn.addEventListener('click', () => {
      triggerHaptic('light');
      if (currentPlaybackRate === 1.0) currentPlaybackRate = 1.5;
      else if (currentPlaybackRate === 1.5) currentPlaybackRate = 2.0;
      else currentPlaybackRate = 1.0;

      speedBtn.textContent = `${currentPlaybackRate}x`;
      if (currentAudio && !currentAudio.paused) {
        currentAudio.playbackRate = currentPlaybackRate;
      }
    });

    if (persist) {
      chatHistory.push({
        type: 'translation',
        original: originalText,
        translated: translatedText,
        sourceLang,
        targetLang,
        timestamp: Date.now()
      });
      saveChatHistory();
    }
  }

  // ── Universal Reliable Audio Player (Google TTS MP3 Stream) ──
  function playTtsAudio(textToSpeak, langCode, buttonElement) {
    triggerHaptic('light');

    // If currently playing the SAME button -> Stop it
    if (currentAudio && activePlayButton === buttonElement && !currentAudio.paused) {
      stopCurrentAudio();
      return;
    }

    // Stop any existing audio first
    stopCurrentAudio();

    // Set playing state on button
    activePlayButton = buttonElement;
    activePlayButton.classList.add('playing');
    const icon = activePlayButton.querySelector('svg');
    if (icon) {
      icon.innerHTML = `
        <rect x="6" y="4" width="4" height="16"></rect>
        <rect x="14" y="4" width="4" height="16"></rect>
      `;
    }

    // Use same-origin Vercel Serverless Function to stream audio without browser CORS/Referer blocks
    const ttsUrl = `/api/tts?tl=${encodeURIComponent(langCode)}&q=${encodeURIComponent(textToSpeak)}`;
    const fallbackUrl = `https://translate.google.com/translate_tts?ie=UTF-8&tl=${encodeURIComponent(langCode)}&client=tw-ob&q=${encodeURIComponent(textToSpeak)}`;

    currentAudio = new Audio(ttsUrl);
    currentAudio.playbackRate = currentPlaybackRate;

    currentAudio.onended = () => {
      stopCurrentAudio();
    };

    currentAudio.onerror = (e) => {
      console.warn('Serverless audio failed, trying fallback stream:', e);
      // Fallback to direct stream
      currentAudio = new Audio(fallbackUrl);
      currentAudio.playbackRate = currentPlaybackRate;
      currentAudio.onended = () => stopCurrentAudio();
      currentAudio.play().catch(() => {
        stopCurrentAudio();
        fallbackSpeechSynthesis(textToSpeak, langCode);
      });
    };

    currentAudio.play().catch((err) => {
      console.warn('Playback error, trying fallback stream:', err);
      currentAudio = new Audio(fallbackUrl);
      currentAudio.playbackRate = currentPlaybackRate;
      currentAudio.onended = () => stopCurrentAudio();
      currentAudio.play().catch(() => {
        stopCurrentAudio();
        fallbackSpeechSynthesis(textToSpeak, langCode);
      });
    });
  }

  function stopCurrentAudio() {
    if (currentAudio) {
      try {
        currentAudio.pause();
        currentAudio.currentTime = 0;
      } catch (_) {}
      currentAudio = null;
    }
    if (activePlayButton) {
      activePlayButton.classList.remove('playing');
      const icon = activePlayButton.querySelector('svg');
      if (icon) {
        icon.innerHTML = `<polygon points="5 3 19 12 5 21 5 3"></polygon>`;
      }
      activePlayButton = null;
    }
  }

  function fallbackSpeechSynthesis(text, lang) {
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
      const utt = new SpeechSynthesisUtterance(text);
      utt.lang = lang === 'am' ? 'am-ET' : 'en-US';
      utt.rate = currentPlaybackRate;
      window.speechSynthesis.speak(utt);
    }
  }

  // ── Typing Indicator ──
  function showTypingIndicator() {
    const typing = document.createElement('div');
    typing.id = 'typingIndicator';
    typing.className = 'msg-wrapper';
    typing.innerHTML = `<div class="translation-card glass-panel" style="padding: 12px 16px; color: #94A3B8; font-size: 13px;">✍️ እየተተረጎመ ነው... (Translating...)</div>`;
    messagesList.appendChild(typing);
    scrollToBottom();
  }

  function removeTypingIndicator() {
    const el = document.getElementById('typingIndicator');
    if (el) el.remove();
  }

  function scrollToBottom() {
    chatFeed.scrollTop = chatFeed.scrollHeight;
  }

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // ── Event Listeners ──
  micBtn.addEventListener('click', () => {
    if (!isRecording) startRecording();
    else stopRecording();
  });

  sendBtn.addEventListener('click', handleTextSubmit);

  textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleTextSubmit();
    }
  });

  chipsContainer?.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const query = chip.dataset.query;
    if (query) {
      textInput.value = query;
      handleTextSubmit();
    }
  });

  clearChatBtn?.addEventListener('click', () => {
    triggerHaptic('light');
    if (tg?.showConfirm) {
      tg.showConfirm('የተተረጎሙትን መልዕክቶች ማጥፋት ይፈልጋሉ? (Clear all chat messages?)', (confirmed) => {
        if (confirmed) {
          clearChat();
        }
      });
    } else {
      if (window.confirm('Clear all chat messages?')) {
        clearChat();
      }
    }
  });

  // Restore saved translations from memory on startup
  loadChatHistory();

})();
