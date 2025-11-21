import os
import json
import random
import string
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify

# --- CONFIGURAÇÃO INICIAL LEVE ---
app = Flask(__name__)
# Limite de 30MB para uploads
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024 

# ID DA PASTA NO GOOGLE DRIVE
FOLDER_ID = '1DW-GHQLfcW6za8_fGF55urbDFWugrjdX'

# Variáveis globais para cache de conexão
_drive_service = None
_sheets_client = None

# =============================================================================
# --- GERENCIADOR DE CONEXÕES (CARREGAMENTO TARDIO) ---
# =============================================================================
def get_google_services():
    """Conecta ao Google apenas quando necessário"""
    global _drive_service, _sheets_client
    
    if _drive_service and _sheets_client:
        return _drive_service, _sheets_client

    print("🔌 Iniciando conexão com Google Services...")
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
        raise Exception("Credenciais do Google não encontradas.")

    _drive_service = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    _sheets_client = gc.open("Resultados Pesquisa Emoções")
    
    return _drive_service, _sheets_client

# =============================================================================
# --- FUNÇÕES AUXILIARES ---
# =============================================================================

def upload_file_to_drive(stream, filename, content_type):
    """
    Faz o upload para o Drive.
    FIX: Recebe 'content_type' (mimetype) explicitamente para evitar erro de atributo.
    """
    from googleapiclient.http import MediaIoBaseUpload
    
    drive, _ = get_google_services()
    
    file_metadata = {
        'name': f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}",
        'parents': [FOLDER_ID]
    }
    
    # Usa o content_type passado como argumento
    media = MediaIoBaseUpload(stream, mimetype=content_type, resumable=True)
    
    file = drive.files().create(body=file_metadata, media_body=media, fields='id').execute()
    file_id = file.get('id')
    
    # Permissão Pública
    drive.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}, fields='id').execute()
    
    return f"https://drive.google.com/uc?export=view&id={file_id}"

def process_base64_image(base64_string):
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
    if study_id:
        try:
            _, sh = get_google_services()
            ws = sh.worksheet("Estudos")
            cell = ws.find(study_id, in_column=1)
            if cell: study_config = json.loads(ws.cell(cell.row, 2).value)
        except Exception as e: print(f"Erro busca estudo: {e}")
    return render_template('index.html', study_id=study_id, study_config=study_config)

@app.route('/admin')
def admin_panel(): return render_template('admin.html')

@app.route('/create_study', methods=['POST'])
def create_study():
    try:
        form_data = request.form; files = request.files
        _, sh = get_google_services() # Garante conexão
        
        study_name = form_data.get('study_name')
        items = []
        index = 0
        
        # LOOP DE PROCESSAMENTO MISTO (UPLOAD E URL)
        while True:
            # Se não encontrar o nome do item X, assume que a lista acabou
            if f'items[{index}][name]' not in form_data: break
            
            # Pega o tipo de entrada ('upload' ou 'url')
            input_type = form_data.get(f'items[{index}][inputType]')
            direct_url = form_data.get(f'items[{index}][directUrl]')
            file_obj = files.get(f'items[{index}][file]')
            
            final_url = ""
            file_type = "image"

            # 1. CASO SEJA URL DIRETA (INTERNET)
            if input_type == 'url' and direct_url:
                final_url = direct_url
                # Tenta detectar se é vídeo pela extensão ou domínio
                if any(x in direct_url.lower() for x in ['.mp4', 'youtube', 'vimeo']): 
                    file_type = 'video'
            
            # 2. CASO SEJA UPLOAD (COMPUTADOR)
            elif input_type == 'upload' and file_obj and file_obj.filename:
                if file_obj.mimetype.startswith('video'): 
                    file_type = 'video'
                
                # CHAMADA CORRIGIDA: Passa o mimetype explicitamente
                final_url = upload_file_to_drive(file_obj.stream, file_obj.filename, file_obj.mimetype)
            
            # Se falhou nos dois (não tem link nem arquivo)
            if not final_url: 
                raise Exception(f"Erro no Item {index+1}: Selecione um arquivo ou cole um link.")

            # Adiciona item processado na lista
            items.append({
                "name": form_data.get(f'items[{index}][name]'),
                "file_data": final_url,
                "file_type": file_type,
                "caption": form_data.get(f'items[{index}][caption]'),
                "duration": int(form_data.get(f'items[{index}][duration]')),
                "fps": int(form_data.get(f'items[{index}][fps]')),
                "questions": {
                    "liking": form_data.get(f'items[{index}][q_liking]') == 'true',
                    "emotions": form_data.get(f'items[{index}][q_emotions]') == 'true',
                    "word": form_data.get(f'items[{index}][q_word]') == 'true'
                }
            })
            index += 1

        # Finaliza e Salva
        study_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        config = {"study_name": study_name, "welcome_message": form_data.get('welcome_message'), "items": items, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        try: ws = sh.worksheet("Estudos")
        except: ws = sh.add_worksheet(title="Estudos", rows=100, cols=5); ws.append_row(["ID", "Config JSON"])
        ws.append_row([study_id, json.dumps(config)])
        
        return jsonify({'status': 'success', 'link': f"{request.host_url.rstrip('/')}/?study_id={study_id}"})

    except Exception as e:
        print(f"Erro Create Study: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
    try:
        import cv2
        data = request.json
        img = process_base64_image(data['image'])
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    try:
        from deepface import DeepFace
        data = request.json
        img = process_base64_image(data['image'])
        analysis = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False, detector_backend='opencv')
        dominant = analysis[0]['dominant_emotion'] if isinstance(analysis, list) else analysis['dominant_emotion']
        return jsonify({'emotion': dominant})
    except Exception as e: return jsonify({'emotion': 'erro'})

@app.route('/save_data', methods=['POST'])
def save_data():
    try:
        _, sh = get_google_services()
        data = request.json
        pid = data.get('participant_id'); sid = data.get('study_id'); results = data.get('results', [])
        rows = []
        for item in results:
            emotions = item.get('emotions_list', [])
            main_emo = max(set(emotions), key=emotions.count) if emotions else "N/A"
            rows.append([pid, sid, item.get('stimulus'), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), main_emo, item.get('liking'), ", ".join(map(str, emotions)), item.get('word')])
        try: ws = sh.worksheet("Resultados")
        except: ws = sh.add_worksheet("Resultados", 1000, 10)
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)