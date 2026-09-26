import collections
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# ==============================================================================
# ⚙️ HARDCODED BOT CONFIGURATION
# ==============================================================================
# Telegram Bot Token
BOT_TOKEN = "7964854170:AAGYQCez6moITKJGSnEkiCFdfm5gCf45sQ0"

# Render provides these two automatically
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))

# Multiple reliable fast mirrors for Scrabble & full English dictionaries
WORDLIST_URLS = [
    "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt",
    "https://raw.githubusercontent.com/raun/Scrabble/master/words.txt",
    "https://gist.githubusercontent.com/deekayen/4148741/raw/10k-common-words.txt"
]

# In-memory dictionary storage indexed by word length for O(1) instant lookup
WORDS_BY_LEN = collections.defaultdict(set)
DICTIONARY_READY = threading.Event()

# English letter frequency scoring (common vowels/consonants rank higher)
LETTER_WEIGHTS = {
    'E': 12.02, 'T': 9.10, 'A': 8.12, 'O': 7.68, 'I': 7.31,
    'N': 6.95,  'S': 6.28, 'R': 6.02, 'H': 5.92, 'D': 4.32,
    'L': 3.98,  'U': 2.88, 'C': 2.71, 'M': 2.61, 'F': 2.30,
    'Y': 2.11,  'W': 2.09, 'G': 2.03, 'P': 1.82, 'B': 1.49,
    'V': 1.11,  'K': 0.69, 'X': 0.17, 'Q': 0.11, 'J': 0.10,
    'Z': 0.07
}

# Offline core seeds so the bot functions immediately even if network download delays
BUILTIN_SEEDS = [
    "MYTH", "WORD", "NOTE", "TOWN", "TONE", "BALD", "PORK", "GIFT",
    "WHEY", "GARD", "BASK", "CHUM", "GAME", "PLAY", "CODE", "COOL",
    "FAST", "STAR", "FIRE", "WATER", "EARTH", "LIGHT", "NIGHT", "DREAM",
    "APPLE", "CRANE", "SLATE", "ROAST", "AUDIO", "PLANT", "SHINE", "TRACE",
    "PLANET", "SYSTEM", "PYTHON", "RENDER", "SERVER", "CODING", "SEARCH"
]

def load_dictionary():
    """Fetches high-speed wordlists and organizes them in RAM by word length."""
    global WORDS_BY_LEN
    temp_dict = collections.defaultdict(set)

    # 1. Preload built-in seeds
    for seed in BUILTIN_SEEDS:
        w = seed.strip().upper()
        if w.isalpha():
            temp_dict[len(w)].add(w)

    print("[*] Downloading and populating full English dictionary in RAM...")
    
    for url in WORDLIST_URLS:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw_data = resp.read().decode("utf-8", errors="ignore")
                count = 0
                for line in raw_data.splitlines():
                    cleaned = line.strip().upper()
                    if cleaned.isalpha() and 2 <= len(cleaned) <= 12:
                        temp_dict[len(cleaned)].add(cleaned)
                        count += 1
                print(f"[✓] Loaded {count:,} words from: {url}")
                break  # Stop after first successful full download
        except Exception as exc:
            print(f"[!] Warning: Could not reach {url}: {exc}")

    # Transfer sets into fast lists
    for length, word_set in temp_dict.items():
        WORDS_BY_LEN[length] = list(word_set)

    total_words = sum(len(w) for w in WORDS_BY_LEN.values())
    print(f"[✓] RAM Dictionary active: {total_words:,} words across {len(WORDS_BY_LEN)} length buckets.")
    DICTIONARY_READY.set()

# Universal tile recognition across all Wordle/WordGrid bots
GREEN_TILES = {'🟩', '🟢', '✅', '💚', 'G'}
YELLOW_TILES = {'🟨', '🟡', '🟧', '🔸', '💛', 'Y'}
RED_TILES = {'🟥', '🔴', '⬛', '⬜', '🔲', '🔳', '❌', '🖤', '🤎', 'R', 'B', 'X'}

def parse_game_message(raw_text: str):
    """
    Robustly extracts rows containing tiles and letters.
    Supports bold mathematical unicode (𝗦𝗔𝗜𝗟 -> SAIL), tiles-first, or words-first.
    """
    normalized = unicodedata.normalize('NFKD', raw_text)
    lines = normalized.splitlines()
    parsed_rows = []

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        # Extract sequence of color tiles from the line
        tile_types = []
        for ch in line_str:
            if ch in GREEN_TILES:
                tile_types.append('G')
            elif ch in YELLOW_TILES:
                tile_types.append('Y')
            elif ch in RED_TILES:
                tile_types.append('R')

        # Extract uppercase English alphabetic characters
        letters_only = [c.upper() for c in line_str if c.isalpha()]

        # Ensure line contains equal number of feedback tiles and word letters
        if tile_types and letters_only and len(tile_types) == len(letters_only):
            word_str = "".join(letters_only)
            parsed_rows.append((word_str, tile_types))

    return parsed_rows

def satisfies_constraints(candidate: str, guesses: list) -> bool:
    """
    Enforces exact Wordle rules including duplicate letter verification:
    - Green (G): Exact positional match.
    - Yellow (Y): Letter exists elsewhere, never at this index.
    - Red (R): Letter count cannot exceed confirmed Yellow + Green instances.
    """
    cand_len = len(candidate)

    for guess_word, feedback in guesses:
        # 1. Positional checks
        for idx in range(cand_len):
            g_char = guess_word[idx]
            status = feedback[idx]

            if status == 'G':
                if candidate[idx] != g_char:
                    return False
            elif status == 'Y':
                if candidate[idx] == g_char:
                    return False
            elif status == 'R':
                if candidate[idx] == g_char:
                    return False

        # 2. Letter frequency counting (handles multiple duplicate letter cases)
        unique_guess_chars = set(guess_word)
        for char in unique_guess_chars:
            confirmed_greens = sum(1 for i in range(cand_len) if guess_word[i] == char and feedback[i] == 'G')
            confirmed_yellows = sum(1 for i in range(cand_len) if guess_word[i] == char and feedback[i] == 'Y')
            rejected_reds = sum(1 for i in range(cand_len) if guess_word[i] == char and feedback[i] == 'R')

            min_required = confirmed_greens + confirmed_yellows
            actual_count = candidate.count(char)

            if rejected_reds > 0:
                if actual_count != min_required:
                    return False
            else:
                if actual_count < min_required:
                    return False

    return True

def calculate_word_score(word: str) -> float:
    """Scores candidate words based on English letter frequencies and diversity."""
    unique_letters = set(word)
    score = sum(LETTER_WEIGHTS.get(ch, 0.5) for ch in unique_letters)
    score += len(unique_letters) * 1.5
    return score

def solve_grid(guesses: list):
    """Filters dictionary in RAM and returns sorted list of matching words."""
    if not guesses:
        return []

    word_length = len(guesses[0][0])
    candidates = WORDS_BY_LEN.get(word_length, [])

    valid_matches = []
    for word in candidates:
        if satisfies_constraints(word, guesses):
            valid_matches.append(word)

    valid_matches.sort(key=calculate_word_score, reverse=True)
    return valid_matches

def send_telegram_message(chat_id: int, text: str):
    """Sends message via Telegram HTTP API using HTML format."""
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("[!] ERROR: Please update BOT_TOKEN with your actual bot token!")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as err:
        print(f"[!] Error sending Telegram message to {chat_id}: {err}")
        return None

def build_response_text(guesses: list, matches: list, elapsed_ms: float) -> str:
    """Creates a clean HTML formatted response with click-to-copy words."""
    total_guesses = len(guesses)
    word_len = len(guesses[0][0])
    total_found = len(matches)

    if total_found == 0:
        return (
            f"❌ <b>No matching words found</b>\n\n"
            f"Analyzed <b>{total_guesses}</b> guesses ({word_len}-letter mode).\n"
            f"<i>Please make sure no letters or tile colors were missing in the forwarded text.</i>"
        )

    top_candidates = matches[:10]

    lines = [
        f"🎯 <b>Found {total_found} possible words</b> in <code>{elapsed_ms:.1f}ms</code>",
        f"Mode: <b>{word_len}-Letter</b> | Analyzed: <b>{total_guesses}</b> rows\n",
        "📋 <b>Tap any word to copy:</b>"
    ]

    for idx, word in enumerate(top_candidates, 1):
        lines.append(f"{idx}. <code>{word}</code>")

    if total_found > 10:
        remaining = total_found - 10
        lines.append(f"\n<i>...plus {remaining} more possibilities.</i>")

    lines.append("\n💡 <i>Tip: Just tap any word above to copy it directly to your clipboard!</i>")
    return "\n".join(lines)

def handle_telegram_update(update: dict):
    """Processes messages received from Telegram."""
    message = update.get("message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id or not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        welcome_text = (
            "👋 <b>Wordle / WordGrid Instant Solver</b>\n\n"
            "Forward or paste your game grid directly here, for example:\n\n"
            "<code>"
            "4-letter mode · 11/30\n\n"
            "🟥 🟥 🟥 🟥 SAIL\n"
            "🟨 🟥 🟥 🟥 TOWN\n"
            "🟨 🟥 🟥 🟥 TONE\n"
            "🟥 🟥 🟥 🟥 BALD\n"
            "🟥 🟥 🟥 🟥 PORK\n"
            "🟥 🟥 🟥 🟨 GIFT\n"
            "🟥 🟥 🟩 🟥 NOTE\n"
            "🟥 🟨 🟥 🟨 WHEY\n"
            "🟥 🟥 🟥 🟥 GARD\n"
            "🟥 🟥 🟥 🟥 BASK\n"
            "🟥 🟨 🟥 🟨 CHUM"
            "</code>\n\n"
            "⚡ <b>Capabilities:</b>\n"
            "• Works for any word length (3, 4, 5, 6, 7+ letters)\n"
            "• Full CSP filtering in memory within 5 milliseconds\n"
            "• Tap any word to copy it instantly!"
        )
        send_telegram_message(chat_id, welcome_text)
        return

    if not DICTIONARY_READY.is_set():
        DICTIONARY_READY.wait(timeout=10)

    start_time = time.perf_counter()
    guesses = parse_game_message(text)

    if not guesses:
        send_telegram_message(
            chat_id,
            "⚠️ <b>Could not recognize game grid.</b>\n"
            "Please paste rows containing color tiles (🟥/🟨/🟩) and corresponding letters."
        )
        return

    matches = solve_grid(guesses)
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    response_text = build_response_text(guesses, matches, elapsed_ms)
    send_telegram_message(chat_id, response_text)

class RenderWebhookHandler(http.server.BaseHTTPRequestHandler):
    """Lightweight HTTP server handling Render health checks and Telegram webhooks."""

    def do_GET(self):
        """Render health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        status = {
            "status": "online",
            "dictionary_ready": DICTIONARY_READY.is_set(),
            "words_in_ram": sum(len(w) for w in WORDS_BY_LEN.values())
        }
        self.wfile.write(json.dumps(status).encode("utf-8"))

    def do_POST(self):
        """Receives incoming updates from Telegram webhook."""
        if self.path.startswith("/webhook"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

            try:
                payload = json.loads(body.decode("utf-8"))
                threading.Thread(target=handle_telegram_update, args=(payload,), daemon=True).start()
            except Exception as e:
                print(f"[!] Error parsing webhook JSON: {e}")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def setup_webhook():
    """Registers the webhook URL automatically using Render's public domain."""
    if not RENDER_EXTERNAL_URL:
        print("[!] RENDER_EXTERNAL_URL not found. Run on Render or expose via public URL.")
        return

    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("[!] Cannot set webhook: BOT_TOKEN is not configured yet.")
        return

    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    params = urllib.parse.urlencode({"url": webhook_url}).encode("utf-8")

    try:
        req = urllib.request.Request(api_url, data=params)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            print(f"[✓] Telegram webhook registered: {webhook_url} -> {result.get('description')}")
    except Exception as exc:
        print(f"[!] Webhook registration failed: {exc}")

# ==============================================================================
# 🔄 KEEP-ALIVE SELF-PING FUNCTION (Every 30 seconds)
# ==============================================================================
def keep_alive_ping():
    """Pings the Render external URL every 30 seconds to prevent cold shutdowns."""
    if not RENDER_EXTERNAL_URL:
        print("[!] RENDER_EXTERNAL_URL not detected. Self-ping keep-alive is disabled.")
        return

    print(f"[✓] Keep-alive self-ping activated: Pinging {RENDER_EXTERNAL_URL} every 30 seconds.")
    # Server start hone ke liye 10 sec wait karega
    time.sleep(10)

    while True:
        try:
            req = urllib.request.Request(
                RENDER_EXTERNAL_URL,
                headers={"User-Agent": "Render-KeepAlive-Ping/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass  # Ping successful, silently keep alive
        except Exception as e:
            print(f"[!] Keep-alive ping error: {e}")

        time.sleep(30)

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded server to handle simultaneous webhook hits."""
    daemon_threads = True

def main():
    print("=" * 60)
    print("🚀 Initializing High-Speed Wordle Bot for Render...")
    print("=" * 60)

    # 1. Asynchronously load dictionary in RAM
    threading.Thread(target=load_dictionary, daemon=True).start()

    # 2. Configure Telegram Webhook using Render domain
    if RENDER_EXTERNAL_URL:
        threading.Thread(target=setup_webhook, daemon=True).start()

    # 3. Start keep-alive ping thread (Pings Render URL every 30 seconds)
    if RENDER_EXTERNAL_URL:
        threading.Thread(target=keep_alive_ping, daemon=True).start()

    # 4. Start Multi-threaded HTTP Server on Render PORT
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadedHTTPServer(server_address, RenderWebhookHandler)
    print(f"[✓] Server listening on 0.0.0.0:{PORT}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down bot.")
        httpd.server_close()

if __name__ == "__main__":
    main()
