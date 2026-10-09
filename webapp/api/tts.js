/**
 * Vercel Serverless Function: High-Quality Audio Streamer (Proxy)
 * Eliminates browser CORS, Referer blocking, and audio corruption.
 */

export default async function handler(req, res) {
  const { q, text, tl = 'am', lang = 'am' } = req.query;
  const queryText = (q || text || '').trim();
  const langCode = (tl || lang || 'am').toLowerCase();

  if (!queryText) {
    return res.status(400).json({ error: 'Text query is required' });
  }

  try {
    // Truncate to reasonable TTS length if needed
    const safeText = queryText.slice(0, 500);
    const ttsUrl = `https://translate.google.com/translate_tts?ie=UTF-8&tl=${encodeURIComponent(langCode)}&client=tw-ob&q=${encodeURIComponent(safeText)}`;

    const response = await fetch(ttsUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'audio/mpeg, audio/*; q=0.9',
        'Accept-Language': 'en-US,en;q=0.9,am;q=0.8',
      },
    });

    if (!response.ok) {
      return res.status(response.status).json({ error: 'Failed to fetch audio stream' });
    }

    const audioBuffer = await response.arrayBuffer();

    res.setHeader('Content-Type', 'audio/mpeg');
    res.setHeader('Content-Length', audioBuffer.byteLength);
    res.setHeader('Cache-Control', 'public, max-age=86400, s-maxage=86400');
    res.setHeader('Access-Control-Allow-Origin', '*');

    return res.status(200).send(Buffer.from(audioBuffer));
  } catch (error) {
    console.error('TTS handler error:', error);
    return res.status(500).json({ error: error.message });
  }
}
