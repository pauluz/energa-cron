#pZ: 10.10.2026

# Crontab codziennie o 16:00 (moje konto to "szczepan")
# 0 16 * * * /usr/bin/python3 /home/szczepan/skrypty/energa_cron.py >> /home/szczepan/skrypty/energa_cron.log 2>&1

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import sys
import re
import urllib.request
import urllib.error
from urllib.parse import urlparse
from html.parser import HTMLParser
import json
import os

# =====================================================================
# Klasa pomocnicza do czyszczenia kodu HTML (zastępuje BeautifulSoup)
# =====================================================================
class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text_data = []
    def handle_data(self, d):
        self.text_data.append(d)
    def get_data(self):
        return ' '.join(self.text_data)

def strip_tags(html_content):
    s = MLStripper()
    try:
        s.feed(html_content)
    except Exception:
        pass
    return s.get_data()

# =====================================================================
# WCZYTYWANIE KONFIGURACJI Z PLIKU JSON
# =====================================================================

# Dynamiczne ustalanie ścieżki (niezbędne dla systemów Cron)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as config_file:
        config_data = json.load(config_file)
        
    # Ustawienia poczty
    EMAIL_TO = config_data.get("EMAIL_TO")
    SMTP_SERVER = config_data.get("SMTP_SERVER")
    SMTP_PORT = config_data.get("SMTP_PORT", 465)
    SMTP_USER = config_data.get("SMTP_USER")
    SMTP_PASS = config_data.get("SMTP_PASS")
    USE_SSL = config_data.get("USE_SSL", True)
    
    # Ustawienia poszukiwań
    ENERGA_URL  = config_data.get("ENERGA_URL")
    MIEJSCOWOSC = config_data.get("CITY")
    ULICA       = config_data.get("STREET")
    
    # Weryfikacja czy wszystkie niezbędne dane zostały podane
    if not all([EMAIL_TO, SMTP_SERVER, SMTP_USER, SMTP_PASS, ENERGA_URL, MIEJSCOWOSC, ULICA]):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BŁĄD: Brakuje kluczowych danych w pliku config.json!")
        sys.exit(1)
        
except FileNotFoundError:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BŁĄD: Nie znaleziono pliku {CONFIG_PATH}!")
    sys.exit(1)
except json.JSONDecodeError:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BŁĄD: Plik config.json ma nieprawidłowy format!")
    sys.exit(1)

# =====================================================================
# LOGIKA PROGRAMU
# =====================================================================

def send_email(subject, body):
    """Wysyła wiadomość e-mail z powiadomieniem."""
    msg = MIMEMultipart()
    msg['From']    = SMTP_USER
    msg['To']      = EMAIL_TO
    msg['Subject'] = subject

    msg.attach(MIMEText(body, 'html'))

    try:
        if USE_SSL:
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Pomyślnie wysłano e-mail do {EMAIL_TO}")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BŁĄD podczas wysyłania e-maila: {e}")
        sys.exit(1)

def check_outages():
    """Pobiera główną stronę, szuka linku do API JSON i parsuje jego strukturę."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Uruchamianie skanera dla: {MIEJSCOWOSC}, ul. {ULICA}...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        # KROK 1: Pobranie głównej strony
        req = urllib.request.Request(ENERGA_URL, headers=headers)
        response = urllib.request.urlopen(req, timeout=15)
        html_content = response.read().decode('utf-8', errors='ignore')
        
        # KROK 2: Wyszukanie w HTML odnośników do systemu API (np. /__system/api/document/EXTERNAL_DOCUMENT-305647/1791450137)
        api_pattern = re.compile(r'(/__system/api/document/[A-Za-z0-9_-]+/[0-9]+)')
        api_paths = api_pattern.findall(html_content)
        
        # Usuwamy ewentualne duplikaty ścieżek
        api_paths = list(set(api_paths))
        
        if not api_paths:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BŁĄD: Nie znaleziono żadnych adresów API na głównej stronie.")
            return
            
        # Zbudowanie bazy adresu URL na podstawie ENERGA_URL podanego w configu
        parsed_url = urlparse(ENERGA_URL)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        found_matches = []
        
        # KROK 3: Funkcja rekurencyjna do przeszukiwania JSONa 
        def search_in_json(node):
            if isinstance(node, dict):
                # Zgodnie z wytycznymi - szukamy bloku, w którym występują klucze 'areas' oraz 'message'
                if 'areas' in node and 'message' in node:
                    areas_text = str(node.get('areas', '')).strip()
                    message_text = str(node.get('message', '')).strip()
                    
                    # Szukamy daty. Skoro struktura bywa zróżnicowana, sprawdzamy kilka potencjalnych kluczy
                    date_text = str(node.get('date', node.get('startDate', node.get('duration', 'Brak daty podanej bezpośrednio')))).strip()
                    date_text_end = str(node.get('endDate', 'Brak daty końca podanej bezpośrednio')).strip()
                    
                    # Logika weryfikacji: Miejscowość w "areas", Ulica w "message"
                    if MIEJSCOWOSC.lower() in areas_text.lower() and ULICA.lower() in message_text.lower():
                        clean_name = strip_tags(areas_text)
                        clean_message = strip_tags(message_text)
                        
                        clean_date = strip_tags(date_text)
                        clean_date_end = strip_tags(date_text_end)

                        miesiace = ["", "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września", "października", "listopada", "grudnia"]

                        # Wyszukiwanie daty bezpośrednio w formacie ISO (np. 2026-10-16T10:00:00Z)
                        date_match = re.search(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})', clean_date)
                        if date_match:
                            y, m, d, t = date_match.groups()
                            nazwa_miesiaca = miesiace[int(m)]
                            
                            clean_date = f"{int(d)} {nazwa_miesiaca} {y} r., o godz. {t}"

                        date_match = re.search(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})', clean_date_end)
                        if date_match:
                            y, m, d, t = date_match.groups()
                            nazwa_miesiaca = miesiace[int(m)]
                            
                            clean_date_end = f"{int(d)} {nazwa_miesiaca} {y} r., o godz. {t}"

                        match_html = f"""
                        <div style="background-color: #fff; padding: 15px; border-radius: 8px; margin-bottom: 15px; border: 1px solid #e2e8f0;">
                            <p style="margin: 0 0 5px 0; font-size: 14px; color: #64748b;">Miejscowość / Obszar:</p>
                            <p style="margin: 0 0 10px 0; font-weight: bold; font-size: 16px;">{clean_name}</p>
                            <p style="margin: 0 0 5px 0; font-size: 14px; color: #64748b;">Szczegóły (ulice):</p>
                            <p style="margin: 0 0 15px 0; line-height: 1.5;">{clean_message}</p>
                            <div style="background-color: #fef3c7; padding: 10px; border-radius: 5px; display: inline-block;">
                                <span style="color: #b45309; font-weight: bold; font-size: 15px;">🗓 Początek: {clean_date}</span><br>
                                <span style="color: #b45309; font-weight: bold; font-size: 15px;">🗓 Koniec: {clean_date_end}</span>
                            </div>
                        </div>
                        """
                        if match_html not in found_matches:
                            found_matches.append(match_html)
                
                # Niezależnie od wyniku w tym słowniku, szukamy zagnieżdżeń (wchodzimy głębiej)
                for value in node.values():
                    search_in_json(value)
                    
            elif isinstance(node, list):
                # Jeśli natrafimy na listę elementów, sprawdzamy każdy z nich
                for item in node:
                    search_in_json(item)

        # KROK 4: Odpytanie każdego zalezionego endpointu API (zazwyczaj będzie jeden główny dla planowanych wyłączeń)
        for api_path in api_paths:
            api_url = base_url + api_path
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Pobieranie danych JSON z: {api_url}")
            
            try:
                api_req = urllib.request.Request(api_url, headers=headers)
                api_response = urllib.request.urlopen(api_req, timeout=15)
                api_data = json.loads(api_response.read().decode('utf-8'))
                
                # Uruchomienie przeszukiwania
                search_in_json(api_data)
                
            except urllib.error.URLError as e:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Błąd połączenia z API {api_url}: {e}")
            except json.JSONDecodeError:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Odpowiedź z {api_url} nie była prawidłowym formatem JSON.")

        # KROK 5: Wysyłanie maila jeśli cokolwiek znaleziono
        if found_matches:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Znaleziono zaplanowane wyłączenia! Wysyłanie wiadomości e-mail...")
            
            html_body = f"""
            <html>
                <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8fafc; padding: 20px;">
                    <h1 style="color: #b45309; border-bottom: 2px solid #b45309; padding-bottom: 10px;">
                        ⚠ Uwaga! Planowane wyłączenie prądu
                    </h1>
                    <p>Wykryto informacje o planowanych przerwach w dostawie prądu dla lokalizacji: <strong>{MIEJSCOWOSC}, ul. {ULICA}</strong></p>
                    
                    <div style="margin: 25px 0;">
            """
            for match in found_matches:
                html_body += match
                
            html_body += f"""
                    </div>
                    <p style="margin-top: 20px;">Więcej szczegółów na oficjalnej stronie: <a href="{ENERGA_URL}" style="color: #2563eb;">Kliknij tutaj</a></p>
                    <p style="font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 10px; margin-top: 30px;">
                        Wiadomość wygenerowana automatycznie przez skrypt Cron (Energa Notifier).
                    </p>
                </body>
            </html>
            """
            send_email(f"Energa Alert - Planowane wyłączenie prądu ({MIEJSCOWOSC}, ul. {ULICA})", html_body)
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Brak planowanych wyłączeń dla podanej lokalizacji.")

    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Nieoczekiwany błąd głównego skryptu: {e}")

if __name__ == "__main__":
    check_outages()
