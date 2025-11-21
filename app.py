import os
import json
import random
import string
from datetime import datetime
from flask import Flask, render_template, request, jsonify

# --- CONFIGURAÇÃO LEVE ---
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024  # 30MB Limite

# SEU ID DA PASTA
FOLDER_ID = '1DW-GHQLfcW6za8_fGF55urbDFWugrjdX'

# Variáveis globais vazias (Cache)
_drive_service = None
_sheets_client = None

# =============================================================================
# --- CONEXÃO TARDIA (SÓ CONECTA QUANDO PRECISA) ---
# =============================================================================
def get_google_services():
    global _drive_service, _sheets_client
    
    # Se já estiver conectado, usa a conexão salva (economiza tempo)
    if _drive_service and _sheets_client:
        return _drive_service, _sheets_client

    print("🔌 Conectando ao Google agora...")
    
    # Importações aqui dentro para não pesar na inicialização do site
    import gspread
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    
    creds = None
    if os.path.exists("credentials.json"):
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    else:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    
    if not creds:
        raise Exception("Credenciais não encontradas.")

    _drive_service = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    _sheets_client = gc.open("Resultados Pesquisa Emoções")
    
    print("✅ Conectado!")
    return _drive_service, _sheets_client

# =============================================================================
# --- FUNÇÕES AUXILIARES ---
# =============================================================================
def upload_to_drive_lazy(stream, filename, content_type):
    """Faz o upload carregando a lib do Google só agora"""
    from googleapiclient.http import MediaIoBaseUpload
    drive, _ = get_google_services()
    
    meta = {
        'name': f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}",
        'parents': [FOLDER_ID]
    }
    media = MediaIoBaseUpload(stream, mimetype=content_type, resumable=True)
    file = drive.files().create(body=meta, media_body=media, fields='id').execute()
    fid = file.get('id')
    
    drive.permissions().create(fileId=fid, body={'type': 'anyone', 'role': 'reader'}, fields='id').execute()
    return f"https://drive.google.com/uc?export=view&id={fid}"

def decode_image_lazy(base64_string):
    """Carrega CV2 e Numpy só quando for analisar rosto"""
    import base64
    import numpy as np
    import cv2
    if ',' in base64_string: base64_string = base64_string.split(',')[1]
    nparr = np.frombuffer(base64.b64decode(base64_string), np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

# =============================================================================
# --- ROTAS ---
# =============================================================================
@app.route('/')
def home():
    study_id = request.args.get('study_id')
    study_config = None
    
    # Só tenta conectar no Google se tiver um ID na URL
    if study_id:
        try:
            _, sh = get_google_services()
            ws = sh.worksheet("Estudos")
            cell = ws.find(study_id, in_column=1)
            if cell: study_config = json.loads(ws.cell(cell.row, 2).value)
        except Exception as e:
            print(f"Erro ao ler estudo: {e}")
            
    return render_template('index.html', study_id=study_id, study_config=study_config)

@app.route('/admin')
def admin_panel():
    # Rota super leve, apenas carrega o HTML
    return render_template('admin.html')

@app.route('/create_study', methods=['POST'])
def create_study():
    try:
        # Agora sim conectamos no Google (Lazy)
        _, sh = get_google_services()
        
        form = request.form
        files = request.files
        items = []
        i = 0
        
        while True:
            if f'items[{i}][name]' not in form: break
            
            itype = form.get(f'items[{i}][inputType]')
            durl = form.get(f'items[{i}][directUrl]')
            fobj = files.get(f'items[{i}][file]')
            
            final_url = ""
            ftype = "image"

            if itype == 'url' and durl:
                final_url = durl
                if any(x in durl.lower() for x in ['.mp4', 'youtube', 'vimeo']): ftype = 'video'
            
            elif itype == 'upload' and fobj and fobj.filename:
                if fobj.mimetype.startswith('video'): ftype = 'video'
                # Chama função de upload
                final_url = upload_to_drive_lazy(fobj.stream, fobj.filename, fobj.mimetype)
            
            if not final_url: raise Exception(f"Item {i+1} sem arquivo/link.")

            items.append({
                "name": form.get(f'items[{i}][name]'),
                "file_data": final_url,
                "file_type": ftype,
                "caption": form.get(f'items[{i}][caption]'),
                "duration": int(form.get(f'items[{i}][duration]')),
                "fps": int(form.get(f'items[{i}][fps]')),
                "questions": {
                    "liking": form.get(f'items[{i}][q_liking]') == 'true',
                    "emotions": form.get(f'items[{i}][q_emotions]') == 'true',
                    "word": form.get(f'items[{i}][q_word]') == 'true'
                }
            })
            i += 1

        sid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        cfg = {
            "study_name": form.get('study_name'),
            "welcome_message": form.get('welcome_message'),
            "items": items,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        try: ws = sh.worksheet("Estudos")
        except: ws = sh.add_worksheet("Estudos", 100, 5); ws.append_row(["ID", "Config"])
        ws.append_row([sid, json.dumps(cfg)])
        
        return jsonify({'status': 'success', 'link': f"{request.host_url.rstrip('/')}/?study_id={sid}"})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
    try:
        import cv2 # Carrega CV2 só agora
        data = request.json
        img = decode_image_lazy(data['image'])
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        faces = cascade.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 4)
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    try:
        from deepface import DeepFace # Carrega IA PESADA só agora
        data = request.json
        img = decode_image_lazy(data['image'])
        res = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False, detector_backend='opencv')
        dom = res[0]['dominant_emotion'] if isinstance(res, list) else res['dominant_emotion']
        return jsonify({'emotion': dom})
    except Exception as e: return jsonify({'emotion': 'erro'})

@app.route('/save_data', methods=['POST'])
def save_data():
    try:
        _, sh = get_google_services()
        data = request.json
        pid = data.get('participant_id'); sid = data.get('study_id'); results = data.get('results', [])
        rows = []
        for item in results:
            emo = item.get('emotions_list', [])
            main = max(set(emo), key=emo.count) if emo else "N/A"
            rows.append([pid, sid, item.get('stimulus'), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), main, item.get('liking'), ", ".join(map(str, emo)), item.get('word')])
        
        try: ws = sh.worksheet("Resultados")
        except: ws = sh.add_worksheet("Resultados", 1000, 10)
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)