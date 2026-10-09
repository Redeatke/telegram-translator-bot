/**
 * አጋዥ (Agazh) — Amharic Voice AI
 * Telegram Mini App Client Logic
 */

(function () {
  'use strict';

  // ── Telegram WebApp Initialization ──
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.enableClosingConfirmation();
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
  const userNameEl = document.getElementById('userName');
  const userAvatarEl = document.getElementById('userAvatar');
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

  // Set user profile from Telegram
  if (tg?.initDataUnsafe?.user) {
    const u = tg.initDataUnsafe.user;
    const name = u.first_name || 'ወዳጄ';
    userNameEl.textContent = name;
    userAvatarEl.textContent = name.charAt(0).toUpperCase();
  }

  // ── State ──
  let isRecording = false;
  let mediaRecorder = null;
  let audioChunks = [];
  let audioContext = null;
  let analyser = null;
  let animFrameId = null;
  let recordStartTime = null;
  let timerInterval = null;

  // ── Audio Recording & Waveform Visualizer ──
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
    dockHint.textContent = 'መልሱ እየተዘጋጀ ነው... (Processing...) ⏳';

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

  // ── Handling Audio & AI Answering ──
  async function handleRecordedAudio(blob) {
    appendUserMessage('🎙️ የድምፅ መልዕክት (Voice Note)');
    showTypingIndicator();

    // In client-side mini app, use Web Speech API or simulate instant intelligent AI
    // We synthesize answer with Amharic response
    setTimeout(() => {
      removeTypingIndicator();
      const sampleResponses = [
        'ሰላም! የላኩልኝን የድምፅ መልዕክት በጥሩ ሁኔታ ተቀብያለሁ። ስለ ኢትዮጵያ ታሪክ ወይም ባህል ማንኛውንም ጥያቄ ለመመለስ ዝግጁ ነኝ!',
        'እንኳን ደህና መጡ! ድምፅዎ ግልፅ ነው። ምን ላግዝዎ እችላለሁ?',
        'ጥያቄዎ ግሩም ነው! በአማርኛ ቋንቋ ማንኛውንም መረጃ በድምፅ እና በፅሁፍ እሰጣችኋለሁ።'
      ];
      const replyText = sampleResponses[Math.floor(Math.random() * sampleResponses.length)];
      appendBotResponse(replyText);
      dockHint.textContent = 'ማይክሮፎኑን ተጭነው በአማርኛ ይናገሩ 🎤';
    }, 1200);
  }

  // ── Handling Text Query ──
  function handleTextSubmit() {
    const text = textInput.value.trim();
    if (!text) return;

    textInput.value = '';
    appendUserMessage(text);
    triggerHaptic('light');
    showTypingIndicator();

    // Query response
    setTimeout(() => {
      removeTypingIndicator();
      generateBotAnswerForText(text);
    }, 1000);
  }

  function generateBotAnswerForText(query) {
    let reply = 'ጥያቄዎ ደርሶኛል! አጋዥ በአማርኛ ለመመለስ ሁልጊዜ ዝግጁ ነው።';

    if (query.includes('ታሪክ')) {
      reply = 'ኢትዮጵያ በዓለም ላይ ካሉ ጥንታዊ እና ረጅም ታሪክ ካላቸው ሀገራት አንዷ ናት። የላሊበላ ውቅር አብያተ ክርስቲያናት፣ የአክሱም ሐውልቶች እና የፋሲል ግቢ ታዋቂ ቅርሶቿ ናቸው።';
    } else if (query.includes('ምግብ')) {
      reply = 'ከምርጥ የኢትዮጵያ ባህላዊ ምግቦች መካከል እንጀራ በዶሮ ወጥ፣ ሽሮ፣ ክትፎ እና ጥብስ ግንባር ቀደም ተጠቃሽ ናቸው!';
    } else if (query.includes('ምን') || query.includes('ትችላለህ')) {
      reply = 'እኔ አጋዥ (Agazh) እባላለሁ። የአማርኛ ድምፅ እና ፅሁፍ ረዳትዎ ነኝ! ጥያቄዎችን መመለስ፣ ቋንቋ ማስተማር እና መረጃዎችን በድምፅ መስጠት እችላለሁ።';
    } else if (query.includes('ግጥም')) {
      reply = '«ፍቅር ያሸንፋል በሁሉም ዘንድሮ፣\nአንድነት ይስፈን በሰላም አብሮ።»\nመልካም ጊዜ ይሁንልዎ!';
    }

    appendBotResponse(reply);
    dockHint.textContent = 'ማይክሮፎኑን ተጭነው በአማርኛ ይናገሩ 🎤';
  }

  // ── Chat UI Rendering ──
  function appendUserMessage(text) {
    if (welcomeCard) welcomeCard.style.display = 'none';

    const wrapper = document.createElement('div');
    wrapper.className = 'msg-wrapper msg-user';
    wrapper.innerHTML = `<div class="msg-bubble">${escapeHtml(text)}</div>`;
    messagesList.appendChild(wrapper);
    scrollToBottom();
  }

  function appendBotResponse(text) {
    triggerHaptic('success');

    const wrapper = document.createElement('div');
    wrapper.className = 'msg-wrapper msg-bot';

    wrapper.innerHTML = `
      <div class="msg-bubble">${escapeHtml(text)}</div>
      <div class="voice-player">
        <button class="play-toggle-btn" aria-label="Play voice note">
          <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16">
            <polygon points="5 3 19 12 5 21 5 3"></polygon>
          </svg>
        </button>
        <div class="audio-track-info">
          <div class="progress-bar-bg">
            <div class="progress-fill"></div>
          </div>
          <div class="track-meta">
            <span class="track-time">0:00 / 0:04</span>
            <span class="speed-badge">1x</span>
          </div>
        </div>
      </div>
    `;

    messagesList.appendChild(wrapper);
    scrollToBottom();

    // Attach voice player logic
    const playBtn = wrapper.querySelector('.play-toggle-btn');
    const speedBadge = wrapper.querySelector('.speed-badge');
    const progressFill = wrapper.querySelector('.progress-fill');
    let isPlaying = false;
    let playbackRate = 1.0;

    playBtn.addEventListener('click', () => {
      triggerHaptic('light');
      if (!isPlaying) {
        isPlaying = true;
        playBtn.innerHTML = `
          <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16">
            <rect x="6" y="4" width="4" height="16"></rect>
            <rect x="14" y="4" width="4" height="16"></rect>
          </svg>
        `;
        speakAmharicText(text, playbackRate, () => {
          isPlaying = false;
          playBtn.innerHTML = `
            <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16">
              <polygon points="5 3 19 12 5 21 5 3"></polygon>
            </svg>
          `;
          progressFill.style.width = '0%';
        }, (prog) => {
          progressFill.style.width = `${prog * 100}%`;
        });
      } else {
        window.speechSynthesis?.cancel();
        isPlaying = false;
        playBtn.innerHTML = `
          <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16">
            <polygon points="5 3 19 12 5 21 5 3"></polygon>
          </svg>
        `;
      }
    });

    speedBadge.addEventListener('click', () => {
      triggerHaptic('light');
      if (playbackRate === 1.0) {
        playbackRate = 1.5;
        speedBadge.textContent = '1.5x';
      } else if (playbackRate === 1.5) {
        playbackRate = 2.0;
        speedBadge.textContent = '2x';
      } else {
        playbackRate = 1.0;
        speedBadge.textContent = '1x';
      }
    });
  }

  function showTypingIndicator() {
    const typing = document.createElement('div');
    typing.id = 'typingIndicator';
    typing.className = 'msg-wrapper msg-bot';
    typing.innerHTML = `<div class="msg-bubble" style="color: #94A3B8;">✍️ አጋዥ እያሰበ ነው... (Thinking...)</div>`;
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

  // ── Amharic Speech Synthesis ──
  function speakAmharicText(text, rate = 1.0, onEnd = null, onProgress = null) {
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = rate;

      // Find Amharic voice or standard voice
      const voices = window.speechSynthesis.getVoices();
      const amVoice = voices.find((v) => v.lang.startsWith('am'));
      if (amVoice) utterance.voice = amVoice;

      let timer = null;
      let start = Date.now();
      const estimatedDuration = Math.max(2500, text.length * 90) / rate;

      timer = setInterval(() => {
        const elapsed = Date.now() - start;
        const progress = Math.min(1.0, elapsed / estimatedDuration);
        if (onProgress) onProgress(progress);
        if (progress >= 1.0) clearInterval(timer);
      }, 100);

      utterance.onend = () => {
        clearInterval(timer);
        if (onProgress) onProgress(1.0);
        if (onEnd) onEnd();
      };

      utterance.onerror = () => {
        clearInterval(timer);
        if (onEnd) onEnd();
      };

      window.speechSynthesis.speak(utterance);
    } else {
      if (onEnd) onEnd();
    }
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
    if (!isRecording) {
      startRecording();
    } else {
      stopRecording();
    }
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

})();
