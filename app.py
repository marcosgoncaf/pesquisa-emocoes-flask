import os
import json
import random
import string
import numpy as np
import gspread
from flask import Flask, render_template, request, jsonify
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# Limite de upload (30MB)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024 

# =============================================================================
# --- CONFIGURAÇÃO GOOGLE DRIVE E SHEETS ---
# =============================================================================
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

# SEU ID DA PASTA (Já inserido conforme seu pedido)
FOLDER_ID = '1DW-GHQLfcW6za8_fGF55urbDFWugrjdX'

drive_service = None
sh = None

def init_google_services():
    """Inicializa conexão com Google apenas se ainda não existir"""
    global drive_service, sh
    if drive_service and sh: return

    try:
        creds = None
        if os.path.exists("credentials.json"):
            creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        else:
            creds_json = os.environ.get("GOOGLE_CREDENTIALS")
            if creds_json:
                creds_dict = json.loads(creds_json)
                creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        
        if creds:
            drive_service = build('drive', 'v3', credentials=creds)
            gc = gspread.authorize(creds)
            sh = gc.open("Resultados Pesquisa Emoções")
            print("✅ Conexão Google OK")
        else:
            print("⚠️ Nenhuma credencial encontrada.")
    except Exception as e:
        print(f"❌ Erro ao conectar Google: {e}")

# Inicializa na partida
init_google_services()

# =============================================================================
# --- FUNÇÕES AUXILIARES ---
# =============================================================================
def upload_to_drive(file_storage, filename):
    if not drive_service: init_google_services()
    try:
        file_metadata = {'name': f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}", 'parents': [FOLDER_ID]}
        media = MediaIoBaseUpload(file_storage, mimetype=file_storage.mimetype, resumable=True)
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')
        drive_service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}, fields='id').execute()
        return f"https://drive.google.com/uc?export=view&id={file_id}"
    except Exception as e:
        print(f"❌ ERRO DETALHADO DRIVE: {str(e)}") # Isso vai aparecer no log do Render se der erro
        return None

def base64_to_image(base64_string):
    import base64
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
        if not sh: init_google_services()
        try:
            ws = sh.worksheet("Estudos")
            cell = ws.find(study_id, in_column=1)
            if cell: study_config = json.loads(ws.cell(cell.row, 2).value)
        except: pass
    return render_template('index.html', study_id=study_id, study_config=study_config)

@app.route('/admin')
def admin_panel(): return render_template('admin.html')

@app.route('/create_study', methods=['POST'])
def create_study():
    try:
        form_data = request.form; files = request.files
        study_name = form_data.get('study_name')
        items = []
        index = 0
        
        while True:
            if f'items[{index}][name]' not in form_data: break
            
            # Lógica Híbrida: URL Externa OU Arquivo
            inputType = form_data.get(f'items[{index}][inputType]') # 'upload' ou 'url'
            directUrl = form_data.get(f'items[{index}][directUrl]')
            file_obj = files.get(f'items[{index}][file]')
            
            final_url = ""
            file_type = "image"

            if inputType == 'url' and directUrl:
                final_url = directUrl
                # Tenta adivinhar se é vídeo pela extensão ou link
                if '.mp4' in directUrl or 'youtube' in directUrl or 'vimeo' in directUrl:
                    file_type = 'video'
            
            elif inputType == 'upload' and file_obj and file_obj.filename != '':
                if file_obj.mimetype.startswith('video'): file_type = 'video'
                final_url = upload_to_drive(file_obj.stream, file_obj.filename)
            
            if not final_url: 
                raise Exception(f"Item {index+1}: É necessário enviar um arquivo ou colar um link.")

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

        study_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        config = {"study_name": study_name, "welcome_message": form_data.get('welcome_message'), "items": items, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        if not sh: init_google_services()
        try: ws = sh.worksheet("Estudos")
        except: ws = sh.add_worksheet(title="Estudos", rows=100, cols=5); ws.append_row(["ID", "Config JSON"])
        ws.append_row([study_id, json.dumps(config)])
        
        return jsonify({'status': 'success', 'link': f"{request.host_url.rstrip('/')}/?study_id={study_id}"})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
    import cv2
    data = request.json
    try:
        img = base64_to_image(data['image'])
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        face_cascade = cv2.CascadeClassifier(cascade_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    from deepface import DeepFace
    data = request.json
    try:
        img = base64_to_image(data['image'])
        analysis = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False, detector_backend='opencv')
        dominant = "não_detectado"
        if isinstance(analysis, list) and len(analysis) > 0: dominant = analysis[0]['dominant_emotion']
        elif isinstance(analysis, dict): dominant = analysis['dominant_emotion']
        return jsonify({'emotion': dominant})
    except Exception as e: return jsonify({'emotion': 'erro'})

@app.route('/save_data', methods=['POST'])
def save_data():
    if not sh: init_google_services()
    data = request.json
    try:
        pid = data.get('participant_id'); sid = data.get('study_id'); results = data.get('results', [])
        rows = []
        for item in results:
            emotions_list = item.get('emotions_list', [])
            try: main_emotion = max(set(emotions_list), key=emotions_list.count)
            except: main_emotion = "N/A"
            rows.append([pid, sid, item.get('stimulus'), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), main_emotion, item.get('liking'), ", ".join(map(str, emotions_list)), item.get('word')])
        try: ws = sh.worksheet("Resultados")
        except: ws = sh.add_worksheet(title="Resultados", rows=1000, cols=10); ws.append_row(["Part.", "ID Est.", "Estímulo", "Data", "Emoção Princ.", "Nota", "Emoções Det.", "Palavra"])
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)