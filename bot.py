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
# ⚙️ BOT CONFIGURATION
# ==============================================================================
BOT_TOKEN = "7964854170:AAGYQCez6moITKJGSnEkiCFdfm5gCf45sQ0"

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))

WORDLIST_URLS = [
    "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt",
    "https://raw.githubusercontent.com/raun/Scrabble/master/words.txt",
    "https://gist.githubusercontent.com/deekayen/4148741/raw/10k-common-words.txt"
]

WORDS_BY_LEN = collections.defaultdict(set)
DICTIONARY_READY = threading.Event()

LETTER_WEIGHTS = {
    'E': 12.02, 'T': 9.10, 'A': 8.12, 'O': 7.68, 'I': 7.31,
    'N': 6.95,  'S': 6.28, 'R': 6.02, 'H': 5.92, 'D': 4.32,
    'L': 3.98,  'U': 2.88, 'C': 2.71, 'M': 2.61, 'F': 2.30,
    'Y': 2.11,  'W': 2.09, 'G': 2.03, 'P': 1.82, 'B': 1.49,
    'V': 1.11,  'K': 0.69, 'X': 0.17, 'Q': 0.11, 'J': 0.10,
    'Z': 0.07
}

BUILTIN_SEEDS = [
    "MYTH", "WORD", "NOTE", "TOWN", "TONE", "BALD", "PORK", "GIFT",
    "WHEY", "GARD", "BASK", "CHUM", "GAME", "PLAY", "CODE", "COOL",
    "FAST", "STAR", "FIRE", "WATER", "EARTH", "LIGHT", "NIGHT", "DREAM",
    "APPLE", "CRANE", "SLATE", "ROAST", "AUDIO", "PLANT", "SHINE", "TRACE",
    "PLANET", "SYSTEM", "PYTHON", "RENDER", "SERVER", "CODING", "SEARCH"
]

def load_dictionary():
    """RAM me fast lookup ke liye dictionary download aur prepare karta hai."""
    global WORDS_BY_LEN
    temp_dict = collections.defaultdict(set)

    for seed in BUILTIN_SEEDS:
        w = seed.strip().upper()
        if w.isalpha():
            temp_dict[len(w)].add(w)

    print("[*] Downloading English dictionary into RAM...")
    
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
                break
        except Exception as exc:
            print(f"[!] Warning: {url} se dictionary nahi mili: {exc}")

    for length, word_set in temp_dict.items():
        WORDS_BY_LEN[length] = list(word_set)

    total_words = sum(len(w) for w in WORDS_BY_LEN.values())
    print(f"[✓] RAM Dictionary active: {total_words:,} words.")
    DICTIONARY_READY.set()

# ==============================================================================
# 🧠 SMART PARSER (WordSeek & Wordle Grid Extractor)
# ==============================================================================
TILE_MAP = {
    # Green tiles (Correct position)
    '🟩': 'G', '🟢': 'G', '✅': 'G', '💚': 'G',
    # Yellow / Orange tiles (Wrong position)
    '🟨': 'Y', '🟡': 'Y', '🟧': 'Y', '🔸': 'Y', '💛': 'Y',
    # Red / Black / Grey tiles (Not in word)
    '🟥': 'R', '🔴': 'R', '⬛': 'R', '⬜': 'R', '🔲': 'R', '🔳': 'R',
    '❌': 'R', '🖤': 'R', '🤎': 'R', '🔘': 'R', '⚪': 'R', '⚫': 'R'
}

def parse_game_message(raw_text: str):
    """
    Super-smart parser:
    - 'Forwarded from...' aur '4-letter mode' ko ignore karta hai.
    - Hidden invisible characters (\ufe0f) ko clean karta hai.
    - Letters (jaise TERA ka R, ya MYTH ka Y) ko tiles se mix nahi hone deta.
    """
    normalized = unicodedata.normalize('NFKD', raw_text).replace('\ufe0f', '').replace('\u200b', '')
    lines = normalized.splitlines()
    parsed_rows = []

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        # 1. Line me se valid emoji tiles extract karo
        tiles = [TILE_MAP[ch] for ch in line_str if ch in TILE_MAP]
        
        if tiles:
            tile_count = len(tiles)
            
            # Line me se exact 'tile_count' length ka English word dhoondho
            candidate_words = re.findall(r'\b[A-Za-z]{' + str(tile_count) + r'}\b', line_str)
            
            if candidate_words:
                first_tile_pos = min(line_str.find(ch) for ch in TILE_MAP if ch in line_str)
                first_word_pos = line_str.upper().find(candidate_words[0].upper())
                
                # Check ki tile pehle hai ya word pehle hai
                if first_tile_pos < first_word_pos:
                    word = candidate_words[-1].upper()
                else:
                    word = candidate_words[0].upper()
                
                parsed_rows.append((word, tiles))
                continue

            # Fallback: agar letters ke beech spaces hon jaise (T E R A)
            letters_only = [c.upper() for c in line_str if c.isalpha()]
            if len(letters_only) == tile_count:
                word = "".join(letters_only)
                parsed_rows.append((word, tiles))
                continue

        # 2. Text tile fallback (e.g. G Y R R TERA)
        tokens = line_str.split()
        if len(tokens) >= 2:
            tile_tokens = tokens[:-1]
            word_token = tokens[-1].upper()
            if word_token.isalpha() and len(word_token) == len(tile_tokens):
                char_tile_map = {'G': 'G', 'Y': 'Y', 'R': 'R', 'B': 'R', 'X': 'R'}
                if all(t.upper() in char_tile_map for t in tile_tokens):
                    parsed_tiles = [char_tile_map[t.upper()] for t in tile_tokens]
                    parsed_rows.append((word_token, parsed_tiles))

    return parsed_rows

def satisfies_constraints(candidate: str, guesses: list) -> bool:
    """Exact Wordle logic duplicate letter checks ke sath."""
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

        # 2. Duplicate letter frequency count
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
    unique_letters = set(word)
    score = sum(LETTER_WEIGHTS.get(ch, 0.5) for ch in unique_letters)
    score += len(unique_letters) * 1.5
    return score

def solve_grid(guesses: list):
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
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("[!] ERROR: Please update BOT_TOKEN!")
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
        print(f"[!] Telegram message send fail: {err}")
        return None

def build_response_text(guesses: list, matches: list, elapsed_ms: float) -> str:
    total_guesses = len(guesses)
    word_len = len(guesses[0][0])
    total_found = len(matches)

    if total_found == 0:
        return (
            f"❌ <b>No matching words found</b>\n\n"
            f"Analyzed <b>{total_guesses}</b> guesses ({word_len}-letter mode).\n"
            f"<i>Word dictionary me match nahi hua ya grid constraints bahut strict hain.</i>"
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

    lines.append("\n💡 <i>Tip: Kisi bhi word par tap karke copy karein!</i>")
    return "\n".join(lines)

def handle_telegram_update(update: dict):
    message = update.get("message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id or not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        welcome_text = (
            "👋 <b>Wordle / WordSeek Instant Solver</b>\n\n"
            "Apne game ka message directly yahan forward ya paste karein:\n\n"
            "<code>"
            "4-letter mode · 2/30\n\n"
            "🟥 🟥 🟨 🟨 TERA\n"
            "🟥 🟨 🟥 🟥 MYTH\n"
            "</code>\n\n"
            "⚡ <b>Features:</b>\n"
            "• Works for any word length (3 to 10 letters)\n"
            "• Tap any word to copy directly!"
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
            "⚠️ <b>Game grid recognize nahi ho payi.</b>\n"
            "Kripya tiles (🟥/🟨/🟩) aur words wali lines sahi se paste ya forward karein."
        )
        return

    matches = solve_grid(guesses)
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    response_text = build_response_text(guesses, matches, elapsed_ms)
    send_telegram_message(chat_id, response_text)

class RenderWebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
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
    if not RENDER_EXTERNAL_URL:
        return

    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    params = urllib.parse.urlencode({"url": webhook_url}).encode("utf-8")

    try:
        req = urllib.request.Request(api_url, data=params)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            print(f"[✓] Webhook setup: {webhook_url} -> {result.get('description')}")
    except Exception as exc:
        print(f"[!] Webhook registration error: {exc}")

# ==============================================================================
# 🔄 KEEP-ALIVE SELF-PING (Har 30 Seconds me Render URL ko Ping Karega)
# ==============================================================================
def keep_alive_ping():
    if not RENDER_EXTERNAL_URL:
        print("[!] RENDER_EXTERNAL_URL nahi mila, self-ping skip ho gaya.")
        return

    print(f"[✓] Keep-alive ping active: {RENDER_EXTERNAL_URL} har 30 seconds me ping hoga.")
    time.sleep(10)

    while True:
        try:
            req = urllib.request.Request(
                RENDER_EXTERNAL_URL,
                headers={"User-Agent": "Render-KeepAlive-Ping/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"[!] Keep-alive ping log: {e}")

        time.sleep(30)

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def main():
    print("=" * 60)
    print("🚀 Wordle / WordSeek Solver Bot Starting on Render...")
    print("=" * 60)

    threading.Thread(target=load_dictionary, daemon=True).start()

    if RENDER_EXTERNAL_URL:
        threading.Thread(target=setup_webhook, daemon=True).start()
        threading.Thread(target=keep_alive_ping, daemon=True).start()

    server_address = ("0.0.0.0", PORT)
    httpd = ThreadedHTTPServer(server_address, RenderWebhookHandler)
    print(f"[✓] Server listening on PORT: {PORT}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down bot.")
        httpd.server_close()

if __name__ == "__main__":
    main()
