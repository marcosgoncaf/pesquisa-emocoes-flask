import os
import json
import random
import string
import base64
import numpy as np
import cv2
import gspread
from flask import Flask, render_template, request, jsonify
from deepface import DeepFace
from datetime import datetime

# Bibliotecas do Google (Necessário requirements.txt atualizado)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# Aumenta limite para 30MB (Uploads de vídeo)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024 

# =============================================================================
# --- CONFIGURAÇÃO UNIFICADA (DRIVE + SHEETS) ---
# =============================================================================
# ID da pasta no Google Drive onde os arquivos serão salvos
FOLDER_ID = '1DW-GHQLfcW6za8_fGF55urbDFWugrjdX'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

drive_service = None
sh = None

try:
    # 1. Carrega as credenciais (Local ou Nuvem)
    creds = None
    if os.path.exists("credentials.json"):
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    else:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    
    if creds:
        # 2. Conecta no Google Drive
        drive_service = build('drive', 'v3', credentials=creds)
        
        # 3. Conecta no Google Sheets
        gc = gspread.authorize(creds)
        sh = gc.open("Resultados Pesquisa Emoções")
        print("✅ Conexão Google Drive e Sheets estabelecida!")
    else:
        print("⚠️ ERRO: Nenhuma credencial encontrada.")

except Exception as e:
    print(f"❌ ERRO CRÍTICO DE CONEXÃO: {e}")

# =============================================================================
# --- FUNÇÕES AUXILIARES ---
# =============================================================================
def upload_to_drive(file_storage, filename):
    """Envia arquivo binário para o Drive e retorna Link Público"""
    if not drive_service: return None
    try:
        # Metadados do arquivo
        file_metadata = {
            'name': f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}",
            'parents': [FOLDER_ID]
        }
        # Prepara o upload
        media = MediaIoBaseUpload(file_storage, mimetype=file_storage.mimetype, resumable=True)
        
        # Executa upload
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        file_id = file.get('id')
        
        # Torna público (Reader para Anyone)
        drive_service.permissions().create(
            fileId=file_id,
            body={'type': 'anyone', 'role': 'reader'},
            fields='id',
        ).execute()
        
        # Retorna link de visualização direta
        return f"https://drive.google.com/uc?export=view&id={file_id}"
        
    except Exception as e:
        print(f"Erro no Upload para o Drive: {e}")
        return None

def base64_to_image(base64_string):
    """Converte imagem da webcam (Base64) para OpenCV"""
    if ',' in base64_string:
        base64_string = base64_string.split(',')[1]
    nparr = np.frombuffer(base64.b64decode(base64_string), np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

# =============================================================================
# --- ROTAS ---
# =============================================================================

@app.route('/')
def home():
    study_id = request.args.get('study_id')
    study_config = None
    if study_id and sh:
        try:
            ws = sh.worksheet("Estudos")
            cell = ws.find(study_id, in_column=1)
            if cell:
                config_json = ws.cell(cell.row, 2).value
                study_config = json.loads(config_json)
        except Exception as e:
            print(f"Erro ao ler estudo: {e}")
    return render_template('index.html', study_id=study_id, study_config=study_config)

@app.route('/admin')
def admin_panel():
    return render_template('admin.html')

# =============================================================================
# --- API (CRIAÇÃO E SALVAMENTO) ---
# =============================================================================

@app.route('/create_study', methods=['POST'])
def create_study():
    try:
        # Recebe dados do Formulário (Multipart)
        form_data = request.form
        files = request.files
        
        study_name = form_data.get('study_name')
        welcome_message = form_data.get('welcome_message')
        
        items = []
        index = 0
        
        # Loop para processar os itens enviados pelo Javascript
        while True:
            # Verifica se existe o nome do item atual, senão para o loop
            if f'items[{index}][name]' not in form_data:
                break
            
            # Pega o arquivo (Imagem ou Vídeo)
            file_obj = files.get(f'items[{index}][file]')
            file_url = ""
            file_type = "image"
            
            if file_obj and file_obj.filename != '':
                # Detecta se é vídeo
                if file_obj.mimetype.startswith('video'):
                    file_type = 'video'
                
                # Faz Upload para o Drive
                print(f"Iniciando upload do item {index}...")
                file_url = upload_to_drive(file_obj.stream, file_obj.filename)
            
            if not file_url:
                raise Exception(f"Falha no upload do arquivo para o item {index+1}. Verifique a conexão com o Drive.")

            # Adiciona à lista
            items.append({
                "name": form_data.get(f'items[{index}][name]'),
                "file_data": file_url, # Salva o LINK do Drive
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

        # Gera ID e Salva no Sheets
        study_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        
        config = {
            "study_name": study_name,
            "welcome_message": welcome_message,
            "items": items,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        if not sh: raise Exception("Sem conexão com o Google Sheets")
        
        try: ws = sh.worksheet("Estudos")
        except: ws = sh.add_worksheet(title="Estudos", rows=100, cols=5); ws.append_row(["ID", "Config JSON"])
        
        ws.append_row([study_id, json.dumps(config)])
        
        base_url = request.host_url.rstrip('/')
        return jsonify({'status': 'success', 'link': f"{base_url}/?study_id={study_id}"})

    except Exception as e:
        print(f"Erro create_study: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
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
    data = request.json
    try:
        img = base64_to_image(data['image'])
        # Usa detector 'ssd' ou 'opencv' que são mais leves que o padrão
        analysis = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False, detector_backend='opencv')
        dominant = "não_detectado"
        if isinstance(analysis, list) and len(analysis) > 0: dominant = analysis[0]['dominant_emotion']
        elif isinstance(analysis, dict): dominant = analysis['dominant_emotion']
        return jsonify({'emotion': dominant})
    except Exception as e: return jsonify({'emotion': 'erro'})

@app.route('/save_data', methods=['POST'])
def save_data():
    data = request.json
    try:
        pid = data.get('participant_id'); sid = data.get('study_id'); results = data.get('results', [])
        rows = []
        for item in results:
            emotions_list = item.get('emotions_list', [])
            try: main_emotion = max(set(emotions_list), key=emotions_list.count)
            except: main_emotion = "N/A"
            rows.append([
                pid, sid, item.get('stimulus'), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                main_emotion, item.get('liking'), ", ".join(map(str, emotions_list)), item.get('word')
            ])
        
        if not sh: raise Exception("Sem conexão com Sheets")
        try: ws = sh.worksheet("Resultados")
        except: ws = sh.add_worksheet(title="Resultados", rows=1000, cols=10); ws.append_row(["Part.", "ID Est.", "Estímulo", "Data", "Emoção Princ.", "Nota", "Emoções Det.", "Palavra"])
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)